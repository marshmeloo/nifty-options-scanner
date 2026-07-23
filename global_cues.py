"""
Overnight global cues for the pre-market brief: how US markets closed,
crude oil, the dollar/rupee, and India VIX -- the things a NIFTY trader
checks before 9:15 IST to form a rough directional lean for the day.

Honesty note upfront: GIFT Nifty (the actual pre-market NIFTY futures
proxy, traded on NSE IX in Gujarat) is the single most-watched overnight
indicator for Indian index traders, but there is no free public API for
it -- it lives behind broker terminals and paid data feeds. This module
does NOT fake a GIFT Nifty number. What it gives you instead is the next
best free thing: how US markets actually closed (largely what GIFT Nifty
reacts to overnight anyway), crude, USD/INR, and India VIX -- enough to
form a lean, just one step removed from the real thing. If you get
access to a GIFT Nifty feed later (several Indian brokers expose one),
that's the highest-value single field to wire in on top of this.

Source: Yahoo Finance's public (unofficial, no-key) chart/quote endpoint
-- the same one yfinance wraps. Free, but undocumented and can change
without notice; treat this module as the integration point to fix if it
breaks, not a guaranteed-stable API client.
"""

import requests

import config as cfg

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# symbol -> plain-language label used in the brief
SYMBOLS = {
    "^GSPC": "S&P 500",
    "^DJI": "Dow Jones",
    "^IXIC": "Nasdaq",
    "CL=F": "Crude Oil (WTI)",
    "INR=X": "USD/INR",
    "^INDIAVIX": "India VIX",
    "^NSEI": "NIFTY 50 (prior close)",
}


def _fetch_one(symbol: str) -> dict:
    """
    Returns {"symbol", "label", "close", "prev_close", "change_pct"} for
    one Yahoo symbol using the chart endpoint's `meta` block (current
    price + previous close), which is cheaper and more reliable than
    pulling the full daily-close history for a single-point read. Raises
    on any failure -- callers should catch per-symbol so one bad ticker
    doesn't kill the whole brief.
    """
    resp = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        headers=_HEADERS,
        timeout=getattr(cfg, "GLOBAL_CUES_REQUEST_TIMEOUT", 10),
    )
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]

    last = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    if last is None or prev_close is None:
        raise ValueError(f"Missing price/previousClose in response for {symbol}")

    change_pct = round((last - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    return {
        "symbol": symbol,
        "label": SYMBOLS.get(symbol, symbol),
        "close": round(last, 2),
        "prev_close": round(prev_close, 2),
        "change_pct": change_pct,
    }


def get_global_cues() -> list:
    """
    Fetch overnight cues for every symbol in SYMBOLS. Returns a list of
    per-symbol dicts (see _fetch_one); a symbol that fails comes back
    with an "error" key instead of raising, so one bad/rate-limited
    ticker doesn't blank out the rest of the brief.
    """
    cues = []
    for symbol in SYMBOLS:
        try:
            cues.append(_fetch_one(symbol))
        except Exception as e:
            cues.append({"symbol": symbol, "label": SYMBOLS.get(symbol, symbol), "error": str(e)})
    return cues


def summarize_bias(cues: list) -> str:
    """
    Very rough directional lean from US index cues alone: majority of
    S&P/Dow/Nasdaq up -> "positive", majority down -> "negative", mixed
    -> "mixed". A starting point for the brief's synthesis, not a
    prediction -- Indian markets can and do gap the other way, especially
    on domestic news days.
    """
    us_indices = [c for c in cues if c["symbol"] in ("^GSPC", "^DJI", "^IXIC") and "change_pct" in c]
    if not us_indices:
        return "unknown (US index data unavailable)"

    up = sum(1 for c in us_indices if c["change_pct"] > 0)
    down = sum(1 for c in us_indices if c["change_pct"] < 0)

    if up == len(us_indices):
        return "positive"
    if down == len(us_indices):
        return "negative"
    return "mixed"
