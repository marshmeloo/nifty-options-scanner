"""
Tiered data source with automatic fallback: Dhan -> NSE -> TradingView.

This is the module main_live.py should import from instead of talking to
dhan_source directly. It tries the primary source first every cycle, and
only drops down a tier when a call actually fails -- it doesn't
permanently "switch" sources, since Dhan (or NSE) can recover mid-session
(token refreshed, rate limit window rolled over, etc).

Tiers, in order:
  1. dhan_source   -- full chain + Greeks. Primary; use this whenever it works.
  2. nse_source     -- full chain, no Greeks. Free public fallback.
  3. tradingview_source -- spot + candles ONLY, no option chain at all.
     get_nifty_snapshot() cannot be served from this tier; if 1 and 2 both
     fail, get_nifty_snapshot() re-raises the tier-2 error rather than
     pretending to return a chain. get_nifty_intraday_candles() CAN fall
     back this far, so price-action analysis can keep running even when
     no chain source is reachable.

Each tier failure is logged with which tier failed and why, so a bad
session is diagnosable from the log file instead of a silent swap.
"""

import logging
import time

import dhan_source
import nse_source
import tradingview_source

log = logging.getLogger("nifty_scanner")

# Per-tier cooldown bookkeeping: once a tier fails, don't retry it on
# every single poll cycle (30s) -- wait out a cooldown so a genuinely
# down source doesn't add latency (and log noise) to every cycle.
_last_failure = {"dhan": 0.0, "nse": 0.0, "tradingview": 0.0}


def _cooldown_seconds():
    import config as cfg
    return getattr(cfg, "FALLBACK_RETRY_COOLDOWN_SECONDS", 60)


def _tier_available(name: str) -> bool:
    return (time.monotonic() - _last_failure[name]) >= _cooldown_seconds()


def _mark_failed(name: str, err: Exception):
    _last_failure[name] = time.monotonic()
    log.info(f"  [data source] {name} failed, falling back: {err}")


def get_nearest_expiry() -> str:
    """Expiry list only exists on Dhan/NSE, not TradingView (no chain there)."""
    if _tier_available("dhan"):
        try:
            return dhan_source.get_nearest_expiry()
        except Exception as e:
            _mark_failed("dhan", e)
    # NSE returns expiries embedded in the chain response itself; grab the
    # nearest one from a live fetch rather than a separate endpoint.
    raw = nse_source._fetch_raw_chain()
    return raw["records"]["expiryDates"][0]


def get_nifty_snapshot(expiry: str = None):
    """
    Try Dhan, then NSE. There is no tier-3 chain source (TradingView has
    no OI data), so if both fail this re-raises the NSE failure so the
    caller's existing except-and-retry-next-cycle handling still works.
    """
    if _tier_available("dhan"):
        try:
            snap = dhan_source.get_nifty_snapshot(expiry=expiry)
            return snap
        except Exception as e:
            _mark_failed("dhan", e)

    if _tier_available("nse"):
        try:
            snap = nse_source.get_nifty_snapshot(expiry=expiry)
            return snap
        except Exception as e:
            _mark_failed("nse", e)
            raise
    else:
        raise RuntimeError(
            "Both dhan and nse sources are in cooldown after recent failures; "
            "no chain source available this cycle."
        )


def get_nifty_intraday_candles(interval: str = None, from_date: str = None, to_date: str = None) -> list:
    """
    Try Dhan, then TradingView as a last resort for spot/candles only
    (NSE has no separate candle endpoint wired here). Can return an empty
    list if all sources fail; callers already treat "no candles this
    cycle" as non-fatal.
    """
    if _tier_available("dhan"):
        try:
            return dhan_source.get_nifty_intraday_candles(interval=interval, from_date=from_date, to_date=to_date)
        except Exception as e:
            _mark_failed("dhan", e)

    if _tier_available("tradingview"):
        try:
            return tradingview_source.get_nifty_intraday_candles(interval=interval)
        except Exception as e:
            _mark_failed("tradingview", e)
            raise

    raise RuntimeError("No candle source available this cycle (dhan and tradingview both in cooldown).")
