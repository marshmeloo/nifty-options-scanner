"""
Is today a normal trading day, or an unusually quiet/wild one?

WHY THIS EXISTS
---------------
Reconstructing this after the fact on 2026-07-30 produced the single
most important context for every conclusion drawn so far: all seven
recorded sessions had a daily range BELOW the 6-month median (0.98%),
sitting between the 1st and 45th percentile. 48% of normal NIFTY days
move at least 1.0% and 20% move at least 1.5% -- we had recorded
literally none of either.

That means a directional/momentum strategy had been evaluated
exclusively in the calmest sliver of market conditions and never once in
the regime it's designed for, which makes every "no edge" reading
conditional on an unrepresentative sample. That's the kind of thing that
should be visible in the log while a session runs, not something dug out
of six months of history a week later.

WHAT IT REPORTS
---------------
Today's range so far, as a percentile of the trailing daily-range
distribution, plus a quiet/normal/volatile label.

IMPORTANT CAVEAT, handled explicitly rather than papered over: an
in-progress day's range is PARTIAL. At 09:30 it is necessarily near zero
and would score as the quietest day on record. So the percentile is
always reported alongside how much of the session has actually elapsed,
and `is_provisional` stays True until the session is nearly done. Early
readings are a floor on the final value, not an estimate of it.
"""

import json
import logging
import statistics
from datetime import datetime, date, time as dtime
from pathlib import Path

import config
from atomic_state import atomic_write_json

log = logging.getLogger("nifty_scanner")

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
BASELINE_PATH = STATE_DIR / "regime_baseline.json"

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
_SESSION_MINUTES = (
    (MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
)


def _load_baseline() -> dict:
    """Trailing daily-range distribution, refetched at most once a day."""
    if BASELINE_PATH.exists():
        try:
            data = json.loads(BASELINE_PATH.read_text())
            if data.get("date") == date.today().isoformat():
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def get_range_distribution(force_refresh: bool = False) -> dict:
    """
    Sorted list of trailing daily range percentages, cached per day.

    Deliberately cached: this needs ~6 months of daily candles, and
    refetching that every 30s poll would be pointless API load for a
    number that changes once a day.
    """
    if not force_refresh:
        cached = _load_baseline()
        if cached.get("ranges"):
            return cached

    try:
        import dhan_source
        candles = dhan_source.get_nifty_daily_candles(
            days_back=getattr(config, "REGIME_LOOKBACK_DAYS", 180)
        )
    except Exception as e:
        log.info(f"  (market regime: could not fetch daily history, skipping: {e})")
        return {}

    ranges = sorted(
        (c.high - c.low) / c.open * 100
        for c in candles
        if c.open and c.high >= c.low
    )
    if not ranges:
        return {}

    data = {
        "date": date.today().isoformat(),
        "ranges": ranges,
        "median": round(statistics.median(ranges), 3),
        "days": len(ranges),
    }
    atomic_write_json(BASELINE_PATH, data)
    return data


def _percentile_of(value: float, sorted_values: list) -> float:
    if not sorted_values:
        return None
    below = sum(1 for v in sorted_values if v <= value)
    return round(below / len(sorted_values) * 100, 1)


def _session_elapsed_pct(now: datetime = None) -> float:
    now = now or datetime.now()
    minutes = (now.hour * 60 + now.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
    return max(0.0, min(100.0, minutes / _SESSION_MINUTES * 100))


def classify(candles: list, now: datetime = None) -> dict:
    """
    Regime read for the current session, from the intraday candles
    main_live.py already fetches -- no extra API call for today's part.

    Returns {} when there isn't enough to say anything. Otherwise:
      range_pct        today's high-low so far, as % of today's open
      percentile       where that sits in the trailing distribution
      label            "quiet" | "normal" | "volatile"
      elapsed_pct      how much of the session has actually run
      is_provisional   True until the session is nearly over
    """
    if not candles:
        return {}

    day_open = candles[0].open
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    if not day_open or hi < lo:
        return {}

    range_pct = (hi - lo) / day_open * 100
    baseline = get_range_distribution()
    ranges = baseline.get("ranges") or []

    elapsed = _session_elapsed_pct(now)
    quiet_p = getattr(config, "REGIME_QUIET_PERCENTILE", 25)
    volatile_p = getattr(config, "REGIME_VOLATILE_PERCENTILE", 75)

    percentile = _percentile_of(range_pct, ranges) if ranges else None
    if percentile is None:
        label = "unknown"
    elif percentile < quiet_p:
        label = "quiet"
    elif percentile > volatile_p:
        label = "volatile"
    else:
        label = "normal"

    return {
        "range_pct": round(range_pct, 2),
        "range_points": round(hi - lo, 1),
        "percentile": percentile,
        "label": label,
        "elapsed_pct": round(elapsed, 0),
        "is_provisional": elapsed < 90,
        "baseline_median_pct": baseline.get("median"),
        "baseline_days": baseline.get("days"),
    }


def describe(regime: dict) -> str:
    """One log line. Says plainly when a reading is still partial."""
    if not regime:
        return "Market regime: unavailable this cycle"

    parts = [
        f"Market regime: range {regime['range_points']} pts ({regime['range_pct']}%)"
    ]
    if regime.get("percentile") is not None:
        parts.append(
            f"p{regime['percentile']:.0f} vs {regime['baseline_days']}d history "
            f"(median {regime['baseline_median_pct']}%)"
        )
    parts.append(regime["label"].upper())
    line = "  |  ".join(parts)

    if regime.get("is_provisional"):
        line += (
            f"  [PARTIAL -- {regime['elapsed_pct']:.0f}% of session elapsed, "
            f"range can only grow from here]"
        )
    return line


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dist = get_range_distribution(force_refresh=True)
    if not dist:
        raise SystemExit("Could not build the range distribution (check Dhan credentials).")
    r = dist["ranges"]
    n = len(r)
    print(f"Trailing daily-range distribution ({n} sessions):")
    for p in (0, 10, 25, 50, 75, 90, 100):
        print(f"  p{p:<4} {r[min(n - 1, int(n * p / 100))]:.2f}%")
    print(f"\n  days >= 1.0%: {sum(1 for v in r if v >= 1.0)} ({100 * sum(1 for v in r if v >= 1.0) / n:.0f}%)")
    print(f"  days >= 1.5%: {sum(1 for v in r if v >= 1.5)} ({100 * sum(1 for v in r if v >= 1.5) / n:.0f}%)")
