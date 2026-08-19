"""
NSE stock universe + cached 5-minute intraday history, for the
"stocks in play" research track. RESEARCH ONLY -- nothing here trades.

WHY A SEPARATE CACHE
--------------------
Every existing data path in this project is index-derivatives-only
(OPTIDX / NSE_FNO / IDX_I). This is the first equity data this codebase
has ever held, so it gets its own module and its own cache rather than
being bolted onto dhan_source's index-shaped helpers.

UNIVERSE
--------
The F&O stock list (Dhan instrument master, INSTRUMENT=FUTSTK,
EXCH_ID=NSE), ~220 names, not the full 9,846-name equity list. Reasons:
these are the liquid, institutionally-traded names; they are the only
ones with a plausible derivative expression later; and a relative-volume
screen run over all 9,846 would mostly surface illiquid microcaps whose
"unusual volume" is a handful of trades. NSE's own test symbols
(*NSETEST) are filtered out.

WHY NOT NSE'S OWN GAINERS / VOLUME-SPURT PAGES
-----------------------------------------------
They are LIVE SNAPSHOTS with no history, and NSE blocks automated
fetches (see nse_source.py's docstring for the same problem). The
"stocks in play" selection must therefore be RECONSTRUCTED from price
and volume data as it would have looked that morning. That is the
better approach regardless: reconstructing from raw bars is the only
way to guarantee the selection used no information unavailable at
decision time.

Run:
    python -m research.stock_data --universe      # build/refresh the symbol list
    python -m research.stock_data --backfill      # fetch history (long, resumable)
    python -m research.stock_data --describe
"""

import argparse
import csv
import gzip
import io
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import dhan_source
import instrument_master

DATA_DIR = Path(__file__).parent.parent / "logs" / "stock_candles"
UNIVERSE_PATH = Path(__file__).parent.parent / "logs" / "stock_universe.json"

DEFAULT_START = "2023-08-01"
SESSION_START, SESSION_END = "09:15", "15:25"   # open-stamped bars, same convention as orb_candle_cache


def build_universe() -> list:
    """[{symbol, security_id}] for the F&O stock universe, cached."""
    resp = requests.get(instrument_master.MASTER_URL,
                        timeout=instrument_master.DOWNLOAD_TIMEOUT_SECONDS)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))

    fno_symbols = {
        r["UNDERLYING_SYMBOL"] for r in rows
        if r.get("INSTRUMENT") == "FUTSTK" and r.get("EXCH_ID") == "NSE"
        and "NSETEST" not in (r.get("UNDERLYING_SYMBOL") or "")
    }
    equities = {
        r["UNDERLYING_SYMBOL"]: r["SECURITY_ID"] for r in rows
        if r.get("INSTRUMENT") == "EQUITY" and r.get("EXCH_ID") == "NSE"
    }
    universe = [{"symbol": s, "security_id": int(equities[s])}
                for s in sorted(fno_symbols) if s in equities]

    UNIVERSE_PATH.parent.mkdir(exist_ok=True)
    UNIVERSE_PATH.write_text(json.dumps(universe, indent=2), encoding="utf-8")
    return universe


def universe() -> list:
    if UNIVERSE_PATH.exists():
        return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return build_universe()


def _path_for(symbol: str) -> Path:
    return DATA_DIR / f"{symbol}.json.gz"


def load(symbol: str) -> dict:
    """{day_iso: [[hhmm, o, h, l, c, v], ...]} -- {} if not cached."""
    p = _path_for(symbol)
    if not p.exists():
        return {}
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (EOFError, OSError, json.JSONDecodeError):
        return {}   # truncated/mid-write -> treat as absent, same as orb_candle_cache


def save(symbol: str, data: dict):
    """Atomic write -- backfill saves per month across a run of hours, so a
    reader (or a kill) must never see a half-written gzip stream."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = _path_for(symbol)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, p)


def _month_starts(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        yield cur
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)


def fetch_month(security_id: int, month_start: date, month_end: date) -> dict:
    """One API call -> {day_iso: [[hhmm,o,h,l,c,v], ...]} for that month."""
    dhan_source.dhan_rate_limiter.wait_for_slot()
    resp = requests.post(
        f"{dhan_source.DHAN_BASE_URL}/charts/intraday",
        headers=dhan_source._headers(),
        json={
            "securityId": str(security_id),
            "exchangeSegment": "NSE_EQ",
            "instrument": "EQUITY",
            "interval": "5",
            "oi": False,
            # 09:14 not 09:15 -- Dhan's fromDate is EXCLUSIVE, see
            # dhan_source.SESSION_FETCH_FROM_TIME. Getting this wrong
            # drops the opening bar, which for an opening-range or
            # opening-relative-volume study is the entire signal.
            "fromDate": f"{month_start.isoformat()} {dhan_source.SESSION_FETCH_FROM_TIME}",
            "toDate": f"{month_end.isoformat()} 15:30:00",
        },
        timeout=30,
    )
    resp.raise_for_status()
    d = resp.json()
    out = {}
    for i, ts in enumerate(d.get("timestamp", []) or []):
        dt = datetime.fromtimestamp(ts)
        hhmm = dt.strftime("%H:%M")
        if not (SESSION_START <= hhmm <= SESSION_END):
            continue
        out.setdefault(dt.date().isoformat(), []).append([
            hhmm, d["open"][i], d["high"][i], d["low"][i], d["close"][i],
            (d.get("volume") or [0] * len(d["timestamp"]))[i],
        ])
    for day in out:
        out[day].sort(key=lambda r: r[0])
    return out


def backfill(start: str = DEFAULT_START, end: str = None, symbols: list = None):
    """
    Fetch month-by-month per symbol. RESUMABLE: a symbol/month already
    present is skipped, so an interrupted run continues where it stopped
    rather than restarting an ~8-hour job.
    """
    end_date = date.fromisoformat(end) if end else date.today()
    start_date = date.fromisoformat(start)
    names = symbols or [u["symbol"] for u in universe()]
    by_symbol = {u["symbol"]: u["security_id"] for u in universe()}

    months = list(_month_starts(start_date, end_date))
    print(f"{len(names)} symbols x {len(months)} months = up to {len(names)*len(months):,} calls",
          flush=True)

    for n, sym in enumerate(names, 1):
        sid = by_symbol.get(sym)
        if not sid:
            continue
        data = load(sym)
        have_months = {d[:7] for d in data}
        fetched = 0
        for ms in months:
            key = ms.strftime("%Y-%m")
            if key in have_months:
                continue
            nxt = (date(ms.year + 1, 1, 1) if ms.month == 12 else date(ms.year, ms.month + 1, 1))
            me = min(nxt - timedelta(days=1), end_date)
            try:
                data.update(fetch_month(sid, ms, me))
                fetched += 1
            except Exception as e:
                print(f"  {sym} {key} failed: {type(e).__name__}", flush=True)
        if fetched:
            save(sym, data)
        print(f"  [{n}/{len(names)}] {sym}: {len(data)} days cached (+{fetched} months)", flush=True)


def describe() -> str:
    if not DATA_DIR.exists():
        return "no stock cache yet -- run: python -m research.stock_data --backfill"
    files = sorted(DATA_DIR.glob("*.json.gz"))
    if not files:
        return "stock cache directory exists but is empty"
    total_days = 0
    spans = []
    for f in files:
        d = load(f.name.replace(".json.gz", ""))
        if d:
            total_days += len(d)
            spans.append((min(d), max(d)))
    size_mb = sum(f.stat().st_size for f in files) / 1e6
    return "\n".join([
        f"{len(files)} symbols cached, {total_days:,} symbol-days, {size_mb:.0f} MB on disk",
        f"date span: {min(s[0] for s in spans)} .. {max(s[1] for s in spans)}" if spans else "",
        f"universe size: {len(universe())}",
    ])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--universe", action="store_true", help="rebuild the symbol list")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--describe", action="store_true")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=None)
    args = p.parse_args()

    if args.universe:
        u = build_universe()
        print(f"{len(u)} F&O stocks -> {UNIVERSE_PATH}")
        print("sample:", [x["symbol"] for x in u[:10]])
    elif args.backfill:
        backfill(start=args.start, end=args.end)
        print()
        print(describe())
    else:
        print(describe())
