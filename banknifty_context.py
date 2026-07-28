"""
Bank Nifty context: is Bank Nifty confirming or fighting whatever NIFTY
is doing today.

Why this exists: financials (banks + NBFCs + insurance) are roughly
30-35% of NIFTY 50's weight, so a lot of what moves NIFTY is really Bank
Nifty moving underneath it. A NIFTY setup that looks bullish is a very
different thing if Bank Nifty is quietly diverging bearish underneath it
(a narrow, financials-lagging rally) versus confirming it. This module
does NOT trade Bank Nifty options -- it's a context/divergence signal
only, feeding into premarket.py's brief and (optionally) scanner.py's
conviction scoring.

Security ID note: Dhan's security ID for the Bank Nifty index is 25 on
the IDX_I segment (same segment your NIFTY fetch in dhan_source.py
already uses for its ID 13) -- confirmed against a community-maintained
Dhan SDK's published index list, not Dhan's own docs directly, since
Dhan doesn't publish a clean human-readable ID list. If this ID is ever
wrong, the fetch will either 400 or return NIFTY's own data by mistake
(worth sanity-checking that the returned close roughly matches Bank
Nifty's real level, not NIFTY's, the first time you run this live).
"""

from datetime import datetime

import requests

import config as cfg
from dhan_source import _headers, DHAN_BASE_URL
from models import Candle
import price_action

BANKNIFTY_UNDERLYING_SCRIP = 25
BANKNIFTY_UNDERLYING_SEG = "IDX_I"


def get_banknifty_candles(interval: str = None, from_date: str = None, to_date: str = None) -> list:
    """
    Same shape and endpoint as dhan_source.get_nifty_intraday_candles,
    just pointed at Bank Nifty's security ID instead. Kept as its own
    function (not a parameterized shared helper) so a change to NIFTY's
    fetch logic doesn't silently also change Bank Nifty's, and vice versa
    -- these are two independent indices, not variations of one call.
    """
    interval = interval or cfg.CANDLE_INTERVAL_MINUTES
    if from_date is None:
        from_date = datetime.now().strftime("%Y-%m-%d 09:15:00")
    if to_date is None:
        to_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    resp = requests.post(
        f"{DHAN_BASE_URL}/charts/intraday",
        headers=_headers(),
        json={
            "securityId": str(BANKNIFTY_UNDERLYING_SCRIP),
            "exchangeSegment": BANKNIFTY_UNDERLYING_SEG,
            "instrument": "INDEX",
            "interval": interval,
            "oi": False,
            "fromDate": from_date,
            "toDate": to_date,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    candles = []
    n = len(data.get("timestamp", []))
    for i in range(n):
        candles.append(
            Candle(
                timestamp=datetime.fromtimestamp(data["timestamp"][i]),
                open=data["open"][i],
                high=data["high"][i],
                low=data["low"][i],
                close=data["close"][i],
                volume=data["volume"][i] if "volume" in data else 0,
            )
        )
    return candles


def get_banknifty_context() -> dict:
    """
    Today's Bank Nifty spot (last candle close), % change from today's
    open, and a trend read via price_action.build_context (the same
    trend/RSI logic used for NIFTY itself). Returns an "error" key
    instead of raising if the fetch fails -- this is a context signal,
    not something that should take down the main scan if it's briefly
    unavailable.
    """
    try:
        candles = get_banknifty_candles()
        if not candles:
            return {"error": "No Bank Nifty candles returned"}

        spot = candles[-1].close
        day_open = candles[0].open
        pct_change = round((spot - day_open) / day_open * 100, 2) if day_open else 0.0
        context = price_action.build_context(candles)

        return {
            "spot": spot,
            "pct_change_today": pct_change,
            "trend": context.trend,
            "trend_note": context.trend_note,
        }
    except Exception as e:
        return {"error": str(e)}


def compute_divergence(nifty_pct_change: float, banknifty_ctx: dict) -> dict:
    """
    Compare NIFTY's own % move today against Bank Nifty's. Divergence
    threshold is deliberately simple: opposite signs with both moves
    non-trivial (>0.1%) counts as "diverging"; same sign counts as
    "confirming"; anything else (one or both flat) is "inconclusive".
    This is a coarse same-day read, not a statistical correlation -- it's
    meant to catch the obvious case ("NIFTY's up but that's not financials
    doing it"), not measure correlation strength.
    """
    if "error" in banknifty_ctx:
        return {"read": "unavailable", "detail": banknifty_ctx["error"]}

    bn_pct = banknifty_ctx["pct_change_today"]

    if abs(nifty_pct_change) < 0.1 or abs(bn_pct) < 0.1:
        return {"read": "inconclusive", "detail": "one or both indices roughly flat today"}

    same_direction = (nifty_pct_change > 0) == (bn_pct > 0)
    if same_direction:
        return {
            "read": "confirming",
            "detail": f"Bank Nifty ({bn_pct:+.2f}%) moving with NIFTY ({nifty_pct_change:+.2f}%)",
        }
    return {
        "read": "diverging",
        "detail": f"Bank Nifty ({bn_pct:+.2f}%) moving AGAINST NIFTY ({nifty_pct_change:+.2f}%) -- "
        f"today's move may be narrower/weaker than it looks from NIFTY alone",
    }
