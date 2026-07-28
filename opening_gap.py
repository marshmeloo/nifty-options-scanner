"""
Captures today's opening gap for NIFTY and Bank Nifty -- the difference
between today's actual opening print and the previous session's close,
in both points and %. This is a market-open sentiment signal (gap up /
gap down / flat open) that nothing in the pipeline currently sees, since
the scanner only ever looks at intraday OI/price action from whenever it
starts polling that day.

Captured once per day and cached (state/opening_gap.json) -- the gap is
a fixed fact about how today started, not something needing a 30s
refresh. Safe to call every cycle; it's a no-op read from cache once
successfully captured.
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

import banknifty_context
from resilient_source import get_nifty_intraday_candles
from models import Candle
from atomic_state import atomic_write_json

STATE_PATH = Path(__file__).parent / "state" / "opening_gap.json"
STATE_PATH.parent.mkdir(exist_ok=True)


def _window(lookback_days=10):
    to_date = datetime.now()
    from_date = to_date - timedelta(days=int(lookback_days * 1.6) + 3)
    return from_date.strftime("%Y-%m-%d 09:15:00"), to_date.strftime("%Y-%m-%d %H:%M:%S")


def _aggregate_to_daily(candles: list) -> list:
    by_date = {}
    for c in candles:
        by_date.setdefault(c.timestamp.date(), []).append(c)
    daily = []
    for d, day_candles in sorted(by_date.items()):
        day_candles.sort(key=lambda c: c.timestamp)
        daily.append(Candle(
            timestamp=datetime.combine(d, datetime.min.time()),
            open=day_candles[0].open,
            high=max(c.high for c in day_candles),
            low=min(c.low for c in day_candles),
            close=day_candles[-1].close,
            volume=sum(c.volume for c in day_candles),
        ))
    return daily


def _gap_for(fetch_fn) -> dict:
    """
    Fetches a wide recent window, aggregates to daily, and compares
    TODAY's own daily candle's open against the most recent PRIOR day's
    close. Explicitly separates "today" from "prior days" rather than
    just taking the last two daily candles -- if today's own candle
    isn't in the aggregated list yet (e.g. called before the first
    candle of the day exists), this correctly returns None instead of
    comparing yesterday's open to the day-before's close and mislabeling
    it as "today's gap."
    """
    today = datetime.now().date()
    from_date, to_date = _window()
    raw = fetch_fn(interval="5", from_date=from_date, to_date=to_date)
    daily = _aggregate_to_daily(raw)

    today_candles = [c for c in daily if c.timestamp.date() == today]
    prior_candles = [c for c in daily if c.timestamp.date() < today]
    if not today_candles or not prior_candles:
        return None

    today_open = today_candles[0].open
    prev_close = prior_candles[-1].close
    gap_points = round(today_open - prev_close, 2)
    gap_pct = round(gap_points / prev_close * 100, 2) if prev_close else None

    return {"today_open": today_open, "prev_close": prev_close, "gap_points": gap_points, "gap_pct": gap_pct}


def capture_opening_gap(force: bool = False) -> dict:
    """
    Computes and caches today's NIFTY + Bank Nifty opening gap.
    Idempotent per calendar day: returns the cached value if already
    captured today, unless force=True. Either index's gap can come back
    None (e.g. called too early, or a fetch failed) without blocking the
    other -- this is a context signal, not something that should ever
    take down the main cycle.
    """
    if STATE_PATH.exists() and not force:
        cached = json.loads(STATE_PATH.read_text())
        if cached.get("date") == datetime.now().date().isoformat():
            return cached

    try:
        nifty_gap = _gap_for(get_nifty_intraday_candles)
    except Exception:
        nifty_gap = None
    try:
        bn_gap = _gap_for(banknifty_context.get_banknifty_candles)
    except Exception:
        bn_gap = None

    result = {"date": datetime.now().date().isoformat(), "nifty": nifty_gap, "banknifty": bn_gap}
    atomic_write_json(STATE_PATH, result, indent=2)
    return result


def get_todays_gap() -> dict:
    """Read-only accessor for whatever's cached for today (may have None legs if not yet captured)."""
    if STATE_PATH.exists():
        cached = json.loads(STATE_PATH.read_text())
        if cached.get("date") == datetime.now().date().isoformat():
            return cached
    return {"date": None, "nifty": None, "banknifty": None}
