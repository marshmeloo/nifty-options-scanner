"""
Last-resort fallback: TradingView, for SPOT PRICE AND CANDLES ONLY.

Important honesty note, read before wiring this in: TradingView does not
publish a public option chain or OI API -- it's a charting platform, not
an options data vendor. There is no CE/PE OI, IV, or Greeks available
from it at any tier. So this module can only ever backstop the parts of
the pipeline that need spot price and OHLC candles (price_action.py,
VWAP-proxy, trend/momentum), NOT the option-chain-driven parts of
scanner.py (OI buildup, IV percentile, PCR, Max Pain / OI walls).

Practically: if both Dhan and NSE are down, resilient_source.py falls
back to this for spot/candles so price-action analysis keeps running,
and returns an option chain of length 0. main_live.py already treats an
empty/failed chain fetch as "scan with price-action only this cycle" (see
its try/except around get_nifty_intraday_candles), so this degrades
gracefully rather than crashing -- just know that OI-based setups won't
fire in this tier.

Implementation: TradingView's unofficial internal websocket/HTTP feed
(the same one their own charts use) is what community libraries like
`tvdatafeed` reverse-engineer. Endpoints and exact fields have changed
before without notice and may need adjustment; treat this file as the
integration point to keep up to date, not a guaranteed-stable API client.
"""

from datetime import datetime, timedelta

import requests

import config as cfg
from models import Candle

TV_SYMBOL = "NSE:NIFTY"
# TradingView's own history endpoint (used by their embeddable widgets).
# Interval must be one of TradingView's supported resolutions.
TV_HISTORY_URL = "https://symbol-search.tradingview.com/symbol_search/"
TV_CHART_DATA_URL = "https://scanner.tradingview.com/symbol"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

_RESOLUTION_MAP = {"1": "1", "5": "5", "15": "15", "25": "25", "60": "60"}


def get_nifty_spot() -> float:
    """
    Best-effort last-traded spot price for the Nifty 50 index via
    TradingView's public quote endpoint. Raises if the endpoint shape has
    changed or the request fails -- resilient_source.py treats that as
    "this tier is also down" and surfaces the original chain-source error.
    """
    resp = requests.get(
        TV_CHART_DATA_URL,
        params={"symbol": TV_SYMBOL, "fields": "lp"},
        headers=_HEADERS,
        timeout=getattr(cfg, "TRADINGVIEW_REQUEST_TIMEOUT", 10),
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data["lp"])


def get_nifty_intraday_candles(interval: str = None) -> list:
    """
    Best-effort intraday OHLC candles for the Nifty 50 index via
    TradingView, for use as a last-resort input to price_action.analyze()
    when both Dhan and NSE candle endpoints are unavailable.
    """
    interval = _RESOLUTION_MAP.get(interval or cfg.CANDLE_INTERVAL_MINUTES, "5")
    now = datetime.now()
    frm = int((now.replace(hour=9, minute=15, second=0, microsecond=0)).timestamp())
    to = int(now.timestamp())

    resp = requests.get(
        f"https://symbol-search.tradingview.com/history",
        params={"symbol": TV_SYMBOL, "resolution": interval, "from": frm, "to": to},
        headers=_HEADERS,
        timeout=getattr(cfg, "TRADINGVIEW_REQUEST_TIMEOUT", 10),
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("s") != "ok":
        raise RuntimeError(f"TradingView history request returned status: {data.get('s')}")

    candles = []
    for i in range(len(data.get("t", []))):
        candles.append(
            Candle(
                timestamp=datetime.fromtimestamp(data["t"][i]),
                open=data["o"][i],
                high=data["h"][i],
                low=data["l"][i],
                close=data["c"][i],
                volume=data["v"][i] if "v" in data else 0,
            )
        )
    return candles
