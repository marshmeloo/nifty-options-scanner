"""
Cached 5-minute NIFTY index candles for the ORB study, fetched with the
CORRECT session start.

WHY THIS EXISTS RATHER THAN REUSING THE RECORDED SNAPSHOTS' CANDLES
--------------------------------------------------------------------
snapshot_recorder's days already carry index candles, but every one of
them is MISSING THE 09:15 BAR -- the single most important bar for an
opening-range study, since it IS the 5-minute opening range.

Cause, confirmed by direct probe 2026-08-19: Dhan's /charts/intraday
`fromDate` is EXCLUSIVE, and dhan_source.get_nifty_intraday_candles
defaults it to "YYYY-MM-DD 09:15:00". Requesting from 09:15 returns 74
bars starting 09:20; requesting from 09:00 returns 75 bars starting
09:15. Every historical reconstruction (and live main_live.py, which
uses the same default) therefore sees the session starting one bar
late. That is a small distortion for ATR or price-action structure, but
a fatal one for ORB -- so this module fetches its own candles from
09:00 instead of inheriting the gap.

Deliberately does NOT change dhan_source's default: that would alter
what Anchor sees live, which this project only does on live-data
evidence (see STRATEGY_VERSIONS.md's promotion policy). The off-by-one
is reported separately as its own finding.

Fetches MONTH-AT-A-TIME (Dhan's intraday endpoint accepts multi-day
ranges -- verified, an 18-day request returned 12 distinct trading
days), so the full ~6-year history costs ~73 API calls rather than
~1,485 one per day.

Run:  python -m research.orb_candle_cache            # fill/refresh the cache
      python -m research.orb_candle_cache --describe # what's in it
"""

import argparse
import gzip
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

import dhan_source

log = logging.getLogger("nifty_scanner")

# parent.parent -- this module lives in research/, the cache belongs in the
# project-root logs/ directory alongside every other study output.
CACHE_PATH = Path(__file__).parent.parent / "logs" / "orb_candles.json.gz"

# NSE regular session, as OPEN-STAMPED bar labels: 09:15 (covering
# 09:15->09:20) through 15:25 (covering 15:25->15:30).
#
# SESSION_END is 15:25, NOT 15:30, and that matters. Raw fetches come
# back with a 15:30-stamped bar on some days but not others (545 of 1506
# cached days had 76 bars, the rest 75). A 15:30 bar covers 15:30->15:35,
# i.e. AFTER the close, and letting it through would mean the study's
# end-of-day exit price came from a post-close bar on a third of the
# sample and a real one on the rest -- an inconsistency in the exit
# definition itself, not just noise. This project has already documented
# that NIFTY prices in the final stretch are not continuously tradable
# (see main_live.py's MARKET_CLOSE comment), which is a second reason not
# to price an exit off a bar past 15:30.
SESSION_START = "09:15"
SESSION_END = "15:25"

DEFAULT_START = "2020-08-01"


def _month_starts(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        yield cur
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)


def load() -> dict:
    """{day_iso: [{t,o,h,l,c,v}, ...]} -- empty dict if no cache yet."""
    if not CACHE_PATH.exists():
        return {}
    try:
        with gzip.open(CACHE_PATH, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (EOFError, OSError, json.JSONDecodeError):
        # Truncated or mid-write. Callers treat "no cache" as "fetch it",
        # which is the right recovery for a pure cache -- see save().
        return {}
    # Re-apply the session window on READ, not only on fetch, so an older
    # cache written under a wider window (e.g. one that let the 15:30 bar
    # through) can't silently feed inconsistent bars to a study. Cheap
    # enough at this size to be worth the guarantee.
    return {
        day: [b for b in rows if SESSION_START <= b["t"] <= SESSION_END]
        for day, rows in data.items()
    }


def save(data: dict):
    """
    Atomic write: build a temp file, then os.replace() it into place.

    refresh() saves after EVERY month so a long fetch can be resumed,
    which means the file is rewritten ~73 times while anything else
    (another shell, a --describe run) might be reading it. A plain
    in-place write leaves a truncated gzip stream visible in that
    window -- hit while building this, reading the cache mid-fetch
    raised EOFError. Same temp-then-replace pattern as atomic_state.py,
    with the same process-unique temp name so two writers can't collide.
    """
    CACHE_PATH.parent.mkdir(exist_ok=True)
    tmp = CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, CACHE_PATH)


def candles_for(day: str, data: dict = None) -> list:
    """
    One day's bars as a list of dicts with t (HH:MM), o/h/l/c/v.
    Empty list for a holiday or an uncached day.
    """
    data = data if data is not None else load()
    return data.get(day, [])


def refresh(start: str = DEFAULT_START, end: str = None, interval: str = "5") -> dict:
    """
    Fill the cache month by month. Already-cached months are skipped, so
    re-running only fetches what's new.
    """
    end_date = date.fromisoformat(end) if end else date.today()
    start_date = date.fromisoformat(start)
    data = load()

    for month_start in _month_starts(start_date, end_date):
        next_month = (date(month_start.year + 1, 1, 1) if month_start.month == 12
                      else date(month_start.year, month_start.month + 1, 1))
        month_end = min(next_month - timedelta(days=1), end_date)

        # Skip a month already fully present. A month is "present" if any
        # of its days are cached AND its last cached day is at/after the
        # last weekday of the range -- cheap heuristic, and the final
        # (current) month is always refetched since it's still growing.
        month_days = [d for d in data if d.startswith(month_start.strftime("%Y-%m"))]
        is_current_month = (month_start.year, month_start.month) == (end_date.year, end_date.month)
        if month_days and not is_current_month:
            continue

        try:
            candles = dhan_source.get_nifty_intraday_candles(
                interval=interval,
                # 09:00, NOT 09:15 -- fromDate is exclusive, see module docstring.
                from_date=f"{month_start.isoformat()} 09:00:00",
                to_date=f"{month_end.isoformat()} 15:30:00",
            )
        except Exception as e:
            print(f"  {month_start.strftime('%Y-%m')} failed: {e}", flush=True)
            continue

        by_day = {}
        for c in candles:
            hhmm = c.timestamp.strftime("%H:%M")
            if not (SESSION_START <= hhmm <= SESSION_END):
                continue
            by_day.setdefault(c.timestamp.date().isoformat(), []).append({
                "t": hhmm,
                "o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume,
            })
        for day, rows in by_day.items():
            data[day] = sorted(rows, key=lambda r: r["t"])

        print(f"  {month_start.strftime('%Y-%m')}: {len(by_day)} trading days", flush=True)
        save(data)

    return data


def describe() -> str:
    data = load()
    if not data:
        return "cache empty -- run: python -m research.orb_candle_cache"
    days = sorted(data)
    bar_counts = {}
    starts = {}
    for d in days:
        bar_counts[len(data[d])] = bar_counts.get(len(data[d]), 0) + 1
        if data[d]:
            starts[data[d][0]["t"]] = starts.get(data[d][0]["t"], 0) + 1
    lines = [
        f"{len(days)} trading days cached: {days[0]} .. {days[-1]}",
        f"bars/day distribution: {dict(sorted(bar_counts.items()))}",
        f"first-bar-of-day distribution: {dict(sorted(starts.items()))}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--describe", action="store_true")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=None)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.describe:
        print(describe())
    else:
        refresh(start=args.start, end=args.end)
        print()
        print(describe())
