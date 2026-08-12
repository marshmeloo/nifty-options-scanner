"""
Historical India VIX daily closes, and the two standard volatility-regime
measures built from them: IV RANK and IV PERCENTILE.

WHY THIS EXISTS
---------------
global_cues.py already fetches India VIX, but only a single live point
(today's close vs. yesterday's) for the pre-market brief -- there was no
persisted HISTORY, so no way to ask "is today's IV rich or cheap
relative to its own recent past," which is exactly what real
condor-selling research (tastytrade et al.) says is the dominant entry
filter for this strategy: iron condors entered when IV rank AND IV
percentile both exceed 50 measured 56.8% win rate vs 48.2% without any
IV filter, on 595 tracked symbols. Our condor's entry (condor_scanner.py)
has no volatility-regime check at all -- this module exists to test
whether adding one would help, on OUR real data, before ever adopting it.

SAME FREE, UNOFFICIAL YAHOO ENDPOINT as global_cues.py, just requesting
a date range instead of the single-point `meta` block. Confirmed
2026-08-11 by direct probe: 1,239 daily bars, 2021-08-11 to today --
five years of real history, comfortably covering the condor backtest's
2024-08..2026-07 window with a long lookback to spare.

DEFINITIONS (standard, not invented here):
  IV RANK       = (today's VIX - trailing low) / (trailing high - trailing low) * 100
                   Sensitive to a single extreme spike/trough anchoring
                   the range for a long time afterward.
  IV PERCENTILE = % of trailing days that closed BELOW today's VIX
                   Less distorted by one outlier day; the metric the
                   dual-filter research above actually used.
Both use a trailing LOOKBACK_DAYS window (default 252, ~1 trading year,
the standard convention), computed only from days STRICTLY BEFORE the
one being scored -- using today's own close in its own trailing window
would leak information a live decision could not have had at the time.
"""

import json
import logging
import statistics
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger("nifty_scanner")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

CACHE_DIR = Path(__file__).parent / "state"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_PATH = CACHE_DIR / "india_vix_history.json"

LOOKBACK_DAYS = 252


def fetch_history(range_="5y") -> list:
    """[(date_iso, close), ...] sorted oldest-first. Raises on failure --
    same "don't fabricate a fallback" discipline as every other real
    data source in this project."""
    resp = requests.get(YAHOO_CHART_URL, params={"range": range_, "interval": "1d"},
                        headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()["chart"]["result"][0]
    timestamps = data["timestamp"]
    closes = data["indicators"]["quote"][0]["close"]
    out = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue  # a genuinely missing bar, not a zero
        out.append((datetime.fromtimestamp(ts).date().isoformat(), round(close, 2)))
    out.sort(key=lambda p: p[0])
    return out


def save_history(history: list, path: Path = None) -> Path:
    path = path or CACHE_PATH
    payload = {"fetched_at": datetime.now().isoformat(), "count": len(history), "history": history}
    path.write_text(json.dumps(payload, indent=1))
    return path


def load_history(path: Path = None) -> list:
    path = path or CACHE_PATH
    data = json.loads(path.read_text())
    return [tuple(p) for p in data["history"]]


def iv_rank_and_percentile(history: list, as_of_date: str, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """
    IV rank/percentile for `as_of_date`, using only closes STRICTLY
    BEFORE that date from `history` ([(date_iso, close), ...],
    oldest-first). Returns {"vix", "iv_rank", "iv_percentile"}, or all
    None if `as_of_date` isn't in history or there isn't enough trailing
    data yet (fewer than lookback_days prior closes).
    """
    dates = [d for d, _ in history]
    if as_of_date not in dates:
        return {"vix": None, "iv_rank": None, "iv_percentile": None}
    idx = dates.index(as_of_date)
    today_vix = history[idx][1]

    window = history[max(0, idx - lookback_days):idx]
    if len(window) < lookback_days:
        return {"vix": today_vix, "iv_rank": None, "iv_percentile": None}

    window_closes = [c for _, c in window]
    lo, hi = min(window_closes), max(window_closes)
    # Clamped to [0, 100] -- today's value CAN exceed the trailing
    # window's own high/low (a fresh multi-year extreme), and the raw
    # formula would otherwise produce a "rank" like -262% or 2327%,
    # which is meaningless as a rank and not how any real platform
    # displays this (tastytrade/thinkorswim both clamp).
    iv_rank = round(max(0.0, min(100.0, (today_vix - lo) / (hi - lo) * 100)), 1) if hi > lo else None
    below = sum(1 for c in window_closes if c < today_vix)
    iv_percentile = round(below / len(window_closes) * 100, 1)

    return {"vix": today_vix, "iv_rank": iv_rank, "iv_percentile": iv_percentile}


def fetch_live_vix() -> float:
    """
    Current intraday India VIX reading (Yahoo chart `meta.regularMarketPrice`),
    for live entry gating -- cheaper than pulling the full daily-close
    history for a single-point read. Raises on failure, same
    don't-fabricate-a-fallback discipline as fetch_history().
    """
    resp = requests.get(YAHOO_CHART_URL, params={"range": "1d", "interval": "1d"},
                        headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    if price is None:
        raise ValueError("Missing regularMarketPrice in India VIX response")
    return round(price, 2)


def live_iv_rank_and_percentile(history: list, current_vix: float, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """
    Same measures as iv_rank_and_percentile(), but for a LIVE intraday
    reading rather than a completed day already sitting in `history`:
    ranks `current_vix` against the most recent `lookback_days` closes in
    `history` (all real, completed prior days -- today's live reading is
    never added to the window it's being ranked against, same
    no-lookahead principle as the backtest version). Returns
    {"vix", "iv_rank", "iv_percentile"}, iv_rank/iv_percentile None if
    there isn't enough trailing history yet.
    """
    if len(history) < lookback_days:
        return {"vix": current_vix, "iv_rank": None, "iv_percentile": None}
    window_closes = [c for _, c in history[-lookback_days:]]
    lo, hi = min(window_closes), max(window_closes)
    iv_rank = round(max(0.0, min(100.0, (current_vix - lo) / (hi - lo) * 100)), 1) if hi > lo else None
    below = sum(1 for c in window_closes if c < current_vix)
    iv_percentile = round(below / len(window_closes) * 100, 1)
    return {"vix": current_vix, "iv_rank": iv_rank, "iv_percentile": iv_percentile}


def load_or_refresh_history(max_age_hours: float = 20.0, path: Path = None) -> list:
    """
    Loads the cached history, refreshing it from Yahoo first if the cache
    is missing or older than max_age_hours. Called every main_condor.py
    poll cycle but only actually hits the network roughly once a day --
    no reason to re-pull 5 years of daily closes every 5-minute cycle.
    """
    path = path or CACHE_PATH
    if path.exists():
        data = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if (datetime.now() - fetched_at).total_seconds() < max_age_hours * 3600:
            return [tuple(p) for p in data["history"]]
    history = fetch_history()
    save_history(history, path)
    return history


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    history = fetch_history()
    path = save_history(history)
    print(f"{len(history)} daily bars, {history[0][0]} .. {history[-1][0]} -> {path}")
