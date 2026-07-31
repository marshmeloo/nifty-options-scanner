"""
Live data source for Nifty using the Dhan API (v2).

Setup:
  pip install requests
  export DHAN_CLIENT_ID="1000000001"
  export DHAN_ACCESS_TOKEN="your-jwt-access-token"

Docs: https://dhanhq.co/docs/v2/option-chain/
Auth: https://dhanhq.co/docs/v2/authentication/

IMPORTANT ASSUMPTIONS, read before trusting the numbers:

1. IV percentile: Dhan's option chain endpoint returns raw implied_volatility,
   not a percentile. This module computes a CROSS-SECTIONAL percentile:
   each option's IV is ranked against all other calls (or puts) in the
   SAME snapshot, within the strike range around spot (see
   STRIKE_RANGE_POINTS in config.py). This answers "is this strike's IV
   rich or cheap relative to the rest of today's chain" — it deliberately
   does NOT compare against a rolling multi-day history, since an earlier
   version compared every strike's IV against the ATM option's IV history
   only, which just measured "how far is this strike from ATM" (normal
   volatility skew) and pinned nearly every OTM strike at 0th or 100th
   percentile. If you want genuine day-over-day IV rank, that needs
   per-strike historical storage, which isn't built yet.

2. VWAP: Nifty is an index, it has no traded volume of its own, so a true
   volume-weighted average price doesn't apply the way it would for a
   stock or future. This module tracks a simple session moving average of
   the index LTP sampled each poll as a VWAP *proxy*, stored in
   live_state.json. Treat this as a rough intraday mean-reversion
   reference, not a real VWAP. If you want a true VWAP, pull Nifty futures
   volume data via the Historical Data API and compute it from that
   instead.

Rate limit: Dhan's option chain endpoint allows 1 request per 3 seconds.
The polling loop in main_live.py defaults to a much slower interval since
OI/IV data itself doesn't update meaningfully every few seconds.
"""

import os
import json
import time
import statistics
from datetime import datetime, date
from pathlib import Path

import requests

import config as cfg
import oi_analytics
from models import OptionQuote, MarketSnapshot, Candle
from atomic_state import atomic_write_json

DHAN_BASE_URL = "https://api.dhan.co/v2"
NIFTY_UNDERLYING_SCRIP = 13       # Dhan's security ID for the Nifty 50 index
NIFTY_UNDERLYING_SEG = "IDX_I"

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
IV_HISTORY_PATH = STATE_DIR / "iv_history.json"
LIVE_STATE_PATH = STATE_DIR / "live_state.json"
IV_HISTORY_WINDOW = 252


def _headers():
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise EnvironmentError(
            "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables first."
        )
    return {
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def get_expiry_list() -> list:
    """
    Full sorted list of active Nifty expiries (nearest first), not just
    the nearest one. Needed by strategies that may need to look PAST the
    nearest expiry -- e.g. the condor rolling to next week's expiry when
    run on expiry day itself, when the "nearest" expiry has ~0 days left.
    """
    resp = requests.post(
        f"{DHAN_BASE_URL}/optionchain/expirylist",
        headers=_headers(),
        json={"UnderlyingScrip": NIFTY_UNDERLYING_SCRIP, "UnderlyingSeg": NIFTY_UNDERLYING_SEG},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def get_nearest_expiry() -> str:
    """Fetch the list of active Nifty expiries and return the nearest one."""
    return get_expiry_list()[0]


def _fetch_raw_chain(expiry: str) -> dict:
    resp = requests.post(
        f"{DHAN_BASE_URL}/optionchain",
        headers=_headers(),
        json={
            "UnderlyingScrip": NIFTY_UNDERLYING_SCRIP,
            "UnderlyingSeg": NIFTY_UNDERLYING_SEG,
            "Expiry": expiry,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]


PRICE_BASELINE_PATH = STATE_DIR / "price_baseline.json"
_PREV_CLOSE_KEYS = ("previous_close_price", "prev_close_price", "close_price", "previous_close")

# Top-of-book field names. Dhan's option-chain docs don't pin these down
# as clearly as last_price/oi, so several plausible spellings are tried --
# the same defensive approach already used for previous close above.
# VERIFY ON FIRST RUN: if `snapshot.chain[0].has_book` is False against a
# live session, none of these matched and every fill silently falls back
# to LTP. main_live.py logs a one-line warning per cycle when that's the
# case, precisely so it can't go unnoticed.
_BID_KEYS = ("top_bid_price", "bid_price", "best_bid_price", "bidprice", "bid")
_ASK_KEYS = ("top_ask_price", "ask_price", "best_ask_price", "askprice", "ask")
_BID_QTY_KEYS = ("top_bid_quantity", "bid_quantity", "bid_qty", "bidQty")
_ASK_QTY_KEYS = ("top_ask_quantity", "ask_quantity", "ask_qty", "askQty")


def _first_present(side: dict, keys):
    """First non-zero value among `keys`, else None."""
    for k in keys:
        v = side.get(k)
        if v:
            return v
    return None


def _load_price_baseline() -> dict:
    """
    Per-strike premium baseline, reset daily, used to classify OI buildup
    as long buildup / short buildup / short covering / long unwinding —
    the same OI% alone can't tell you that; you need price direction too.

    Dhan's option-chain response doesn't clearly document a previous-close
    field for each option side (only previous_oi is documented). This
    checks a few plausible field names in case one shows up, and falls
    back to our own tracked baseline (first LTP seen this session for
    that strike+type) if none are present. The session-baseline fallback
    is a reasonable proxy but not identical to "previous day's close" —
    worth knowing if you're cross-checking against a terminal.
    """
    if PRICE_BASELINE_PATH.exists():
        data = json.loads(PRICE_BASELINE_PATH.read_text())
        if data.get("date") == date.today().isoformat():
            return data
    return {"date": date.today().isoformat(), "prices": {}}


def _save_price_baseline(state: dict):
    atomic_write_json(PRICE_BASELINE_PATH, state)


OI_HISTORY_PATH = STATE_DIR / "oi_history.json"


def _load_oi_history() -> dict:
    """
    Short rolling per-contract history of (timestamp, oi, ltp), reset
    daily. Exists so buildup can be classified on what OI did in the last
    few MINUTES rather than versus yesterday's close -- see the
    OI_INTRADAY_* block in config.py for the incident that motivated it.
    """
    if OI_HISTORY_PATH.exists():
        try:
            data = json.loads(OI_HISTORY_PATH.read_text())
            if data.get("date") == date.today().isoformat():
                return data
        except (json.JSONDecodeError, OSError):
            pass  # corrupt or unreadable -> start fresh, this is only a cache
    return {"date": date.today().isoformat(), "samples": {}}


def _save_oi_history(history: dict):
    atomic_write_json(OI_HISTORY_PATH, history)


def _intraday_change(samples: list, now_ts: float, oi: float, ltp: float):
    """
    Compare current OI/LTP against the oldest sample still inside the
    lookback window. Returns (oi_change_pct, price_change_pct,
    window_minutes), or (None, None, None) when there isn't enough
    history yet -- which is normal for the first few minutes of a session
    and must NOT be silently reported as "no change".
    """
    lookback_seconds = cfg.OI_INTRADAY_LOOKBACK_MINUTES * 60
    in_window = [s for s in samples if now_ts - s["t"] <= lookback_seconds]
    if not in_window:
        return None, None, None

    baseline = min(in_window, key=lambda s: s["t"])
    span_minutes = round((now_ts - baseline["t"]) / 60, 1)
    # A baseline from seconds ago says nothing about a buildup; require at
    # least a third of the intended window before trusting the comparison.
    if span_minutes < cfg.OI_INTRADAY_LOOKBACK_MINUTES / 3:
        return None, None, None

    prev_oi, prev_ltp = baseline["oi"], baseline["ltp"]
    oi_change = round((oi - prev_oi) / prev_oi * 100, 2) if prev_oi else None
    price_change = round((ltp - prev_ltp) / prev_ltp * 100, 2) if prev_ltp else None
    return oi_change, price_change, span_minutes


def _prune_samples(samples: list, now_ts: float) -> list:
    """Keep only what the lookback window can still use, plus a margin."""
    keep_seconds = cfg.OI_INTRADAY_LOOKBACK_MINUTES * 60 * 2
    return [s for s in samples if now_ts - s["t"] <= keep_seconds]


def _classify_buildup(oi_change_pct: float, price_change_pct, oi_threshold: float):
    """
    Standard options OI/price interpretation:
      price up   + OI up   -> long buildup     (buyers accumulating, bullish for this contract)
      price down + OI up   -> short buildup    (writers accumulating, bearish for this contract)
      price up   + OI down -> short covering   (writers exiting, bullish for this contract)
      price down + OI down -> long unwinding   (longs exiting, bearish for this contract)
    Returns None if the OI move doesn't clear the threshold, or if there's
    no price baseline yet to compare against.
    """
    if abs(oi_change_pct) < oi_threshold or price_change_pct is None:
        return None
    oi_up = oi_change_pct > 0
    price_up = price_change_pct > 0
    if oi_up and price_up:
        return "long_buildup"
    if oi_up and not price_up:
        return "short_buildup"
    if not oi_up and price_up:
        return "short_covering"
    return "long_unwinding"


def _load_iv_history() -> list:
    if IV_HISTORY_PATH.exists():
        return json.loads(IV_HISTORY_PATH.read_text())
    return []


def _save_iv_history(history: list):
    atomic_write_json(IV_HISTORY_PATH, history[-IV_HISTORY_WINDOW:])


def _load_vwap_proxy_state() -> dict:
    if LIVE_STATE_PATH.exists():
        return json.loads(LIVE_STATE_PATH.read_text())
    return {"date": None, "samples": []}


def _save_vwap_proxy_state(state: dict):
    atomic_write_json(LIVE_STATE_PATH, state)


def _update_vwap_proxy(spot: float) -> float:
    """Simple session moving average of spot, reset each trading day."""
    state = _load_vwap_proxy_state()
    today = date.today().isoformat()
    if state.get("date") != today:
        state = {"date": today, "samples": []}
    state["samples"].append(spot)
    _save_vwap_proxy_state(state)
    return round(statistics.mean(state["samples"]), 2)


def get_nifty_intraday_candles(interval: str = None, from_date: str = None, to_date: str = None) -> list:
    """
    Fetch intraday OHLC candles for the Nifty index, for use with
    price_action.analyze(). Defaults to today's session so far.

    interval: "1","5","15","25","60" (minutes)
    from_date/to_date: "YYYY-MM-DD HH:MM:SS" strings. Defaults to
    today 09:15:00 through now.
    """
    import config as cfg

    interval = interval or cfg.CANDLE_INTERVAL_MINUTES
    if from_date is None:
        from_date = datetime.now().strftime("%Y-%m-%d 09:15:00")
    if to_date is None:
        to_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    resp = requests.post(
        f"{DHAN_BASE_URL}/charts/intraday",
        headers=_headers(),
        json={
            "securityId": str(NIFTY_UNDERLYING_SCRIP),
            "exchangeSegment": NIFTY_UNDERLYING_SEG,
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


def get_nifty_daily_candles(days_back: int = 180) -> list:
    """
    DAILY OHLC candles for the Nifty index, going `days_back` calendar
    days into the past. Used by market_regime.py to build the reference
    distribution of "what a normal day's range looks like" -- the
    intraday endpoint above can't answer that, since it only covers the
    current session.

    Note this is /v2/charts/historical (daily), a different endpoint
    from /v2/charts/intraday used above.
    """
    from datetime import timedelta

    resp = requests.post(
        f"{DHAN_BASE_URL}/charts/historical",
        headers=_headers(),
        json={
            "securityId": str(NIFTY_UNDERLYING_SCRIP),
            "exchangeSegment": NIFTY_UNDERLYING_SEG,
            "instrument": "INDEX",
            "fromDate": (date.today() - timedelta(days=days_back)).isoformat(),
            "toDate": date.today().isoformat(),
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    candles = []
    for i in range(len(data.get("timestamp", []))):
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


def get_nifty_snapshot(expiry: str = None, must_include_strikes: set = None) -> MarketSnapshot:
    """
    Fetch a live Nifty option chain snapshot from Dhan and return it in the
    same MarketSnapshot shape the rest of the pipeline already consumes.

    `must_include_strikes`: strikes that must survive the
    STRIKE_RANGE_POINTS filter below regardless of distance from spot.

    BUG FIXED 2026-07-30: STRIKE_RANGE_POINTS (default 800pts of CURRENT
    spot) was applied here unconditionally, silently dropping any strike
    that drifted outside the window on every fetch -- including strikes
    an open position is actively tracking. Confirmed live: an iron
    condor's hedge PE, opened 300pts below its short strike, crossed the
    800pt line as spot drifted through a session and its MTM P&L read
    "unavailable" for four consecutive cycles with no error logged,
    exactly tracking the leg's distance from spot crossing 800.

    Same bug CLASS as the 2026-07-22 incident (README), but a DIFFERENT
    filter: that fix moved the PREMIUM band out of chain-build time
    entirely because a NEW candidate's premium filter has no business
    touching the raw chain. STRIKE_RANGE_POINTS is different -- it's a
    deliberate universe-narrowing choice for scanning/IV-percentile
    purposes (config.py: "deep OTM strikes are usually near-worthless
    and just add noise") that's fine to keep, it just also needs the
    same protection PREMIUM_MIN/MAX already has: never remove a strike
    something is already tracking. Callers with an open position
    (main_condor.py, main_directional_spread.py, main_live.py) pass
    their own tracked strikes through this parameter.
    """
    if expiry is None:
        expiry = get_nearest_expiry()

    raw = _fetch_raw_chain(expiry)
    spot = raw["last_price"]
    vwap_proxy = _update_vwap_proxy(spot)

    protected = must_include_strikes or set()
    raw_chain = raw["oc"]
    if getattr(cfg, "STRIKE_RANGE_POINTS", None):
        raw_chain = {
            k: v for k, v in raw_chain.items()
            if abs(float(k) - spot) <= cfg.STRIKE_RANGE_POINTS or float(k) in protected
        }

    iv_history = _load_iv_history()
    atm_strike = min(raw["oc"].keys(), key=lambda k: abs(float(k) - spot))
    atm_ce_iv = raw["oc"][atm_strike]["ce"]["implied_volatility"]
    atm_pe_iv = raw["oc"][atm_strike]["pe"]["implied_volatility"]
    atm_straddle_iv = (atm_ce_iv + atm_pe_iv) / 2
    iv_history.append(atm_straddle_iv)
    _save_iv_history(iv_history)

    # First pass: build raw quotes without percentile (need the full CE/PE
    # IV lists first to rank each option against its own side of the chain)
    raw_quotes = []
    for strike_str, sides in raw_chain.items():
        strike = float(strike_str)
        for opt_type, key in (("CE", "ce"), ("PE", "pe")):
            side = sides.get(key)
            if not side:
                continue
            ltp = side["last_price"]
            # NOTE: no PREMIUM_MIN/MAX filter here on purpose. This chain
            # feeds OI analytics (max pain/walls/PCR need every strike) AND
            # trade_tracker's open-trade lookup (which must be able to find
            # an already-open trade's quote even after its premium has
            # moved well outside the "worth entering fresh" band -- that's
            # normal and expected as a position runs toward its target).
            # The premium filter is applied at candidate-selection time in
            # scanner.py instead, where it actually belongs.
            oi = side["oi"]
            prev_oi = side.get("previous_oi", 0)
            oi_change_pct = ((oi - prev_oi) / prev_oi * 100) if prev_oi else 0.0
            raw_quotes.append((strike, opt_type, side, oi, oi_change_pct))

    # Cross-sectional IV percentile: rank each option's IV against all other
    # CEs (or PEs) in THIS snapshot, not against a mismatched ATM-only
    # history. This is what actually answers "is this strike's IV rich or
    # cheap relative to the rest of today's chain," instead of just
    # reflecting normal volatility skew between ATM and OTM strikes.
    ce_ivs = sorted(q[2]["implied_volatility"] for q in raw_quotes if q[1] == "CE")
    pe_ivs = sorted(q[2]["implied_volatility"] for q in raw_quotes if q[1] == "PE")

    def _cross_sectional_percentile(iv, sorted_ivs):
        if len(sorted_ivs) < 5:
            return 50.0  # not enough strikes in range to rank meaningfully
        below = sum(1 for x in sorted_ivs if x <= iv)
        return round((below / len(sorted_ivs)) * 100, 1)

    chain = []
    price_baseline = _load_price_baseline()
    baseline_prices = price_baseline["prices"]

    oi_history = _load_oi_history()
    history_samples = oi_history["samples"]
    now_ts = time.time()
    # The fast position check re-fetches the chain every few seconds; only
    # append a new sample when enough time has passed since the last one,
    # so history stays small and the window means what it says.
    record_sample = (now_ts - oi_history.get("last_sample_t", 0)) >= cfg.OI_HISTORY_SAMPLE_SPACING_SECONDS

    for strike, opt_type, side, oi, oi_change_pct in raw_quotes:
        iv = side["implied_volatility"]
        iv_pct = _cross_sectional_percentile(iv, ce_ivs if opt_type == "CE" else pe_ivs)
        ltp = side["last_price"]

        # Previous price for this specific contract: prefer a Dhan-provided
        # field if one shows up, otherwise fall back to our own tracked
        # session baseline (see _load_price_baseline docstring).
        key = f"{strike}_{opt_type}"
        prev_price = None
        for pk in _PREV_CLOSE_KEYS:
            if side.get(pk):
                prev_price = side[pk]
                break
        if prev_price is None:
            prev_price = baseline_prices.get(key)
        if key not in baseline_prices:
            baseline_prices[key] = ltp  # first time seeing this contract today

        price_change_pct = None
        if prev_price:
            price_change_pct = round((ltp - prev_price) / prev_price * 100, 2)

        # Intraday deltas over the rolling window, then record this tick.
        samples = history_samples.get(key, [])
        oi_intraday, price_intraday, window_minutes = _intraday_change(samples, now_ts, oi, ltp)
        if record_sample:
            samples = _prune_samples(samples, now_ts)
            samples.append({"t": now_ts, "oi": oi, "ltp": ltp})
            history_samples[key] = samples

        # Classify on the INTRADAY move when we have it -- that's the whole
        # point of this history. Fall back to the day-cumulative figures
        # only during the session's opening minutes, before enough samples
        # exist to say anything about recent positioning.
        if oi_intraday is not None and price_intraday is not None:
            buildup_type = _classify_buildup(
                oi_intraday, price_intraday, cfg.OI_INTRADAY_BUILDUP_PCT
            )
        else:
            buildup_type = _classify_buildup(oi_change_pct, price_change_pct, cfg.OI_BUILDUP_PCT)

        chain.append(
            OptionQuote(
                symbol="NIFTY",
                expiry=expiry,
                strike=strike,
                option_type=opt_type,
                ltp=ltp,
                oi=oi,
                oi_change_pct=round(oi_change_pct, 2),
                volume=side.get("volume", 0),
                iv=iv,
                iv_percentile=iv_pct,
                delta=side.get("greeks", {}).get("delta"),
                theta=side.get("greeks", {}).get("theta"),
                vega=side.get("greeks", {}).get("vega"),
                price_change_pct=price_change_pct,
                buildup_type=buildup_type,
                oi_change_pct_intraday=oi_intraday,
                price_change_pct_intraday=price_intraday,
                buildup_window_minutes=window_minutes,
                bid=_first_present(side, _BID_KEYS),
                ask=_first_present(side, _ASK_KEYS),
                bid_qty=_first_present(side, _BID_QTY_KEYS),
                ask_qty=_first_present(side, _ASK_QTY_KEYS),
            )
        )

    _save_price_baseline(price_baseline)
    if record_sample:
        oi_history["last_sample_t"] = now_ts
        _save_oi_history(oi_history)

    # PCR reflects the whole chain's OI sentiment, computed BEFORE the
    # premium filter narrows things down to the strikes we actually score.
    total_ce_oi = sum(
        sides["ce"]["oi"] for sides in raw_chain.values() if sides.get("ce")
    )
    total_pe_oi = sum(
        sides["pe"]["oi"] for sides in raw_chain.values() if sides.get("pe")
    )
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else 0.0
    oi_analysis = oi_analytics.analyze(chain, spot)

    return MarketSnapshot(
        symbol="NIFTY",
        spot=spot,
        vwap=vwap_proxy,
        pcr=pcr,
        chain=chain,
        timestamp=datetime.now(),
        oi_analysis=oi_analysis,
        source="dhan",
    )
