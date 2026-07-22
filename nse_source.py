"""
Fallback data source: NSE's public option-chain API. Used when Dhan is
down, rate-limited, or the access token has expired.

NSE's website exposes the same option-chain data its own site renders at:
  https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
It's undocumented and unofficial (no SLA, can change or start blocking
requests without notice), but it's free, needs no API key, and returns
a very similar shape to Dhan's chain (OI, LTP, IV, volume; NO Greeks --
NSE doesn't publish delta/theta/vega for you, unlike Dhan).

NSE blocks requests that don't look like a real browser: no cookies, no
plausible User-Agent -> 401/403. The fix is a short "warm-up" GET against
the plain homepage first to pick up NSE's session cookies, then reuse
that same session for the API call. This is standard practice for this
endpoint and doesn't bypass any login or paywall -- the data is public.
"""

from datetime import datetime

import requests

import config as cfg
import oi_analytics
from models import OptionQuote, MarketSnapshot

NSE_HOME_URL = "https://www.nseindia.com/"
NSE_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


def _warmed_up_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_HEADERS)
    # Hit the homepage first purely to receive NSE's session cookies --
    # the API call below will 401 without them.
    session.get(NSE_HOME_URL, timeout=getattr(cfg, "NSE_SESSION_WARMUP_TIMEOUT", 5))
    return session


def _fetch_raw_chain() -> dict:
    session = _warmed_up_session()
    resp = session.get(NSE_CHAIN_URL, timeout=getattr(cfg, "NSE_REQUEST_TIMEOUT", 10))
    resp.raise_for_status()
    return resp.json()


def get_nifty_snapshot(expiry: str = None) -> MarketSnapshot:
    """
    Fetch a live Nifty option chain from NSE's public API and return it in
    the same MarketSnapshot shape as dhan_source.get_nifty_snapshot, so
    the rest of the pipeline can't tell the difference. No Greeks (delta/
    theta/vega stay None); everything OI/IV/PCR-based still works.
    """
    raw = _fetch_raw_chain()
    records = raw["records"]
    spot = records["underlyingValue"]

    all_expiries = records["expiryDates"]
    target_expiry = expiry or all_expiries[0]

    rows = [r for r in records["data"] if r.get("expiryDate") == target_expiry]
    if getattr(cfg, "STRIKE_RANGE_POINTS", None):
        rows = [r for r in rows if abs(r["strikePrice"] - spot) <= cfg.STRIKE_RANGE_POINTS]

    chain = []
    for row in rows:
        strike = float(row["strikePrice"])
        for opt_type, key in (("CE", "CE"), ("PE", "PE")):
            side = row.get(key)
            if not side:
                continue
            ltp = side.get("lastPrice", 0.0)
            if getattr(cfg, "PREMIUM_MIN", None) is not None and ltp < cfg.PREMIUM_MIN:
                continue
            if getattr(cfg, "PREMIUM_MAX", None) is not None and ltp > cfg.PREMIUM_MAX:
                continue
            oi = side.get("openInterest", 0)
            oi_change_pct = side.get("pchangeinOpenInterest", 0.0)
            chain.append(
                OptionQuote(
                    symbol="NIFTY",
                    expiry=target_expiry,
                    strike=strike,
                    option_type=opt_type,
                    ltp=ltp,
                    oi=oi,
                    oi_change_pct=round(oi_change_pct, 2),
                    volume=side.get("totalTradedVolume", 0),
                    iv=side.get("impliedVolatility", 0.0),
                    iv_percentile=50.0,  # NSE doesn't give us history to rank against; neutral placeholder
                    price_change_pct=side.get("pChange"),
                    # No Greeks and no cross-session buildup classification in this
                    # fallback tier -- those need Dhan's greeks payload / our own
                    # tracked baseline respectively. Structural signals (OI/IV/PCR)
                    # still work fine; strategies leaning on delta/theta/vega or
                    # buildup_type should expect None here.
                )
            )

    total_ce_oi = records.get("filtered", {}).get("CE", {}).get("totOI") or sum(
        q.oi for q in chain if q.option_type == "CE"
    )
    total_pe_oi = records.get("filtered", {}).get("PE", {}).get("totOI") or sum(
        q.oi for q in chain if q.option_type == "PE"
    )
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else 0.0

    return MarketSnapshot(
        symbol="NIFTY",
        spot=spot,
        vwap=spot,  # NSE's public payload has no VWAP proxy of its own; use spot until a real one is tracked
        pcr=pcr,
        chain=chain,
        timestamp=datetime.now(),
        oi_analysis=oi_analytics.analyze(chain, spot),
        source="nse",
    )
