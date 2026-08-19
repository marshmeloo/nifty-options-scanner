"""
Reconstructs historical option-chain MarketSnapshots from Dhan's Expired
Options Data API, so strategies can be evaluated over years of market
history instead of only the handful of days recorded live.

WHY THIS EXISTS
---------------
snapshot_recorder.py breaks the "you can only learn by trading" deadlock
going FORWARD, but it can only record days that have already happened
since it was built -- 7 of them as of 2026-07-31, all of which turned out
to be below the 6-month median daily range (see market_regime.py). Every
conclusion drawn from that sample is conditional on an unrepresentative
one, and waiting for a representative sample means waiting quarters.

Dhan's Expired Options Data endpoint sells the same raw observables for
the past 5 years at minute granularity. Reconstructing those into the
exact MarketSnapshot shape snapshot_recorder.load_day() yields means
replay.py and shadow.py work over historical data with NO changes -- the
existing measurement machinery just gets a much bigger sample.

WHAT THE API GIVES, AND THE ONE CONSTRAINT THAT MATTERS
-------------------------------------------------------
POST /v2/charts/rollingoption returns parallel arrays (open/high/low/
close/volume/oi/iv/strike/spot/timestamp) for ONE strike offset and ONE
option type per call, on a ROLLING basis: `expiryCode: 1` means "whatever
was the nearest weekly expiry at each point in time", which is exactly
the contract our strategies trade. Strikes are expressed as ATM-relative
offsets, so no expired-contract security-ID lookup is needed.

THE CONSTRAINT: offsets are capped at ATM+/-10. Probed directly on
2026-07-31 -- ATM-10 returns a full 375 bars, ATM-12 and beyond return
HTTP 200 with a silently EMPTY data block, not an error. At NIFTY's 50pt
strike spacing that is a hard +/-500 point window around spot.

Consequences, per strategy:
  - Momentum (main_live.py): fully covered. Its premium band keeps
    candidates well inside +/-500pts of spot.
  - Directional spread: mostly covered. Short strike plus a 150pt hedge
    fits unless the short is already >350pts out.
  - Iron condor: NOT fully covered. It picks shorts by premium
    (Rs 23-30), often 400-600pts out, then hedges 300pts BEYOND that --
    routinely outside the window. Condor backtests over this data are
    therefore partial by construction, and reconstruct_day() reports
    coverage explicitly rather than quietly handing back a chain with
    the wings missing. Same failure mode as the STRIKE_RANGE_POINTS bug
    of 2026-07-30 -- a silently truncated chain -- so it is surfaced
    loudly here rather than left to be discovered in the results.

WHAT IS NOT RECONSTRUCTED
-------------------------
  - Bid/ask. The endpoint returns OHLC only, no top of book. Snapshots
    built here leave bid/ask None, so shadow.py falls back to LTP-based
    fills and results are correspondingly OPTIMISTIC versus live, where
    entry pays the ask and exit hits the bid. Compare historical results
    against live ones with that bias in mind -- never pool them.
  - Greeks (delta/theta/vega/gamma). Not returned. plan_generator's ATR x
    delta stop distance falls back to its no-delta path. gamma_exposure.py
    reconstructs gamma itself from what IS available here (spot, strike,
    time to expiry, iv) via Black-Scholes -- see that module's docstring,
    including the validation against Dhan's own live-reported gamma that
    justifies trusting the reconstruction.
  - VWAP. No intraday VWAP is published; spot is used as the same
    placeholder nse_source.py already uses.
  - Sub-interval path. Minute is the finest granularity available, and
    5-minute is the practical default here; a spike between bars is
    invisible, exactly as it is in live 30s recording.

These gaps are why historical results reject variants more credibly than
they accept them -- the same rule replay.py and shadow.py already carry.
"""

import argparse
import logging
import os
from datetime import datetime, date, timedelta

import requests

import config
import dhan_rate_limiter
import oi_analytics
from models import OptionQuote, MarketSnapshot

log = logging.getLogger("nifty_scanner")

ROLLING_URL = "https://api.dhan.co/v2/charts/rollingoption"

# NIFTY index, per Dhan's underlying scrip table -- the same id
# dhan_source.py uses for the live option chain.
NIFTY_SECURITY_ID = 13

# Bank Nifty index. Confirmed 2026-08-04 by direct probe, NOT the
# UNDERLYING_SECURITY_ID (26009) found in Dhan's instrument-master CSV
# for BANKNIFTY option rows -- that ID returns HTTP 200 with an empty
# block on this endpoint every time, regardless of date/expiryFlag. This
# ID (25) is the same one banknifty_context.py already uses for the
# INDEX candle endpoints, and confirmed here to also be what
# /charts/rollingoption expects (verified by checking the returned spot
# values are genuinely Bank Nifty's ~57,000 level, not NIFTY's ~24,000).
BANKNIFTY_SECURITY_ID = 25

# Bank Nifty has no weekly expiries anymore (confirmed against Dhan's
# instrument master: its currently-listed expiries are monthly). "WEEK"
# and "MONTH" were confirmed to return IDENTICAL data for Bank Nifty on
# this endpoint (it appears to just resolve "nearest" regardless of the
# flag when there's only one expiry series), but "MONTH" is the
# accurate, honest label for what's actually being fetched.
BANKNIFTY_EXPIRY_FLAG = "MONTH"

# NIFTY's weekly options expiry moved from Thursday to Tuesday effective
# 2025-09-01 (SEBI circular, October 2024) -- both regimes fall inside
# the recorded history this module reconstructs, so which weekday a
# contract actually settled on depends on WHEN the snapshot is from, not
# a single constant. Mirrors shadow_directional_spread.py's own
# _EXPIRY_WEEKDAY_* constants (kept as a separate copy here rather than
# imported from there, to avoid this data-reconstruction module
# depending on a strategy-specific backtest module -- small enough that
# the duplication risk is low next to that cleaner dependency direction).
_EXPIRY_WEEKDAY_CHANGE_DATE = date(2025, 9, 1)
_EXPIRY_WEEKDAY_BEFORE = 3   # Thursday
_EXPIRY_WEEKDAY_ON_OR_AFTER = 1   # Tuesday


def nominal_expiry_date(on_date: date) -> date:
    """
    Real calendar expiry date for a NIFTY "rolling:week1"-labelled
    contract seen on `on_date` -- next occurrence of the regime-
    appropriate weekday (Thursday before 2025-09-01, Tuesday on/after)
    on or after `on_date` itself. NIFTY ONLY: Bank Nifty's rolling
    contracts here use MONTH, not WEEK (see BANKNIFTY_EXPIRY_FLAG's own
    comment), and monthly expiry-date reconstruction (last
    Tuesday/Thursday of the month, adjusted for holidays) is a different
    and more involved calculation this function does not attempt.

    Built for gamma_exposure.py, which needs an actual date to compute
    time-to-expiry from -- see that module's _resolve_expiry_date().
    """
    target = _EXPIRY_WEEKDAY_ON_OR_AFTER if on_date >= _EXPIRY_WEEKDAY_CHANGE_DATE else _EXPIRY_WEEKDAY_BEFORE
    return on_date + timedelta(days=(target - on_date.weekday()) % 7)


# Real data start dates, found by direct probe (binary search on
# 2026-08-04), NOT from any documented retention window -- Dhan's own
# "5 years" claim for this endpoint undersold NIFTY's actual depth by a
# year. Weekends/holidays show up as empty and are not part of this
# boundary; these are the first WEEKDAYS with real, non-empty data.
NIFTY_DATA_START = "2020-08-03"
BANKNIFTY_DATA_START = "2021-08-04"

# Hard API limit, probed directly 2026-07-31: ATM-10 returns full data,
# ATM-12+ returns HTTP 200 with an empty block. Not documented as an
# error, so nothing but this constant stops us requesting strikes that
# silently come back empty.
MAX_STRIKE_OFFSET = 10

# The API rejects a 0 expiryCode ("expiryCode is required" -- it treats
# 0 as absent). 1 is the nearest expiry at each point in time.
NEAREST_EXPIRY_CODE = 1

# Documented cap: at most 30 days of data per call.
MAX_DAYS_PER_CALL = 30

REQUIRED_DATA = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]


def _headers() -> dict:
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise RuntimeError(
            "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables first."
        )
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def _offset_label(offset: int) -> str:
    """0 -> 'ATM', 3 -> 'ATM+3', -3 -> 'ATM-3' (the API's own spelling)."""
    if offset == 0:
        return "ATM"
    return f"ATM{offset:+d}"


def fetch_series(offset: int, option_type: str, from_date: str, to_date: str,
                 interval: str = "5", expiry_flag: str = "WEEK",
                 expiry_code: int = NEAREST_EXPIRY_CODE,
                 security_id: int = NIFTY_SECURITY_ID) -> dict:
    """
    One strike offset, one option type, over a date range. Returns the
    raw parallel-array block (or {} when the API returns its silent
    empty response).

    `option_type` is our own "CE"/"PE"; the API spells these CALL/PUT on
    the way in and ce/pe on the way out.

    `security_id` defaults to NIFTY; pass BANKNIFTY_SECURITY_ID (with
    expiry_flag=BANKNIFTY_EXPIRY_FLAG) for Bank Nifty.
    """
    if abs(offset) > MAX_STRIKE_OFFSET:
        raise ValueError(
            f"strike offset {offset} exceeds the API's ATM+/-{MAX_STRIKE_OFFSET} limit "
            f"(requests beyond it return HTTP 200 with empty data, not an error)"
        )

    body = {
        "exchangeSegment": "NSE_FNO",
        "interval": str(interval),
        "securityId": security_id,
        "instrument": "OPTIDX",
        "expiryFlag": expiry_flag,
        "expiryCode": expiry_code,
        "strike": _offset_label(offset),
        "drvOptionType": "CALL" if option_type == "CE" else "PUT",
        "requiredData": REQUIRED_DATA,
        "fromDate": from_date,
        "toDate": to_date,
    }

    dhan_rate_limiter.wait_for_slot()
    resp = requests.post(ROLLING_URL, headers=_headers(), json=body,
                         timeout=getattr(config, "DHAN_REQUEST_TIMEOUT", 30))
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    block = data.get("ce" if option_type == "CE" else "pe")
    return block or {}


def _rows(block: dict, interval_minutes: int = 5):
    """
    Walk one block's parallel arrays as per-timestamp dicts.

    Arrays are zipped to the SHORTEST length rather than assumed equal:
    a truncated array would otherwise silently misalign every field after
    the gap, pairing one bar's price with another bar's OI -- corruption
    that looks like data rather than like an error.

    Each bar's timestamp is the bar's START, but the value we read from it
    is its CLOSE -- the price at the bar's END. So `interval_minutes` is
    added to place the observation at the instant it actually describes.

    This is not a guess. Validated against the 2026-07-30 live recording
    (validate_historical.py) by sweeping candidate shifts; agreement has a
    sharp minimum at exactly one interval:

        shift    spot median    LTP median
        +0s        7.90pts        2.53%
        +150s      5.20pts        1.86%
        +300s      1.20pts        0.41%   <- one full 5-minute bar
        +600s      8.05pts        2.62%

    Without this, every backtest would run against prices lagging the
    market by one bar -- producing confident, entirely fictional results,
    since nothing about it errors.
    """
    stamps = block.get("timestamp") or []
    if not stamps:
        return
    keys = [k for k in REQUIRED_DATA if isinstance(block.get(k), list)]
    n = min([len(stamps)] + [len(block[k]) for k in keys])
    for i in range(n):
        row = {k: block[k][i] for k in keys}
        row["timestamp"] = (datetime.fromtimestamp(stamps[i])
                            + timedelta(minutes=interval_minutes))
        yield row


def reconstruct_range(from_date: str, to_date: str, interval: str = "5",
                      max_offset: int = MAX_STRIKE_OFFSET,
                      expiry_flag: str = "WEEK",
                      expiry_code: int = NEAREST_EXPIRY_CODE,
                      security_id: int = NIFTY_SECURITY_ID,
                      symbol: str = "NIFTY") -> list:
    """
    Fetch every strike offset x option type over a date range and pivot
    them by timestamp into MarketSnapshots, oldest first.

    Costs (2 * max_offset + 1) * 2 API calls -- 42 at the default -- each
    spaced by dhan_rate_limiter, so a 30-day pull takes roughly 2.5
    minutes. That is the whole reason this writes to disk (see
    backfill()) rather than being re-fetched per backtest.

    `security_id`/`symbol` default to NIFTY; pass BANKNIFTY_SECURITY_ID/
    "BANKNIFTY" (with expiry_flag=BANKNIFTY_EXPIRY_FLAG) for Bank Nifty.
    """
    by_time = {}
    spot_by_time = {}
    missing = []

    for offset in range(-max_offset, max_offset + 1):
        for option_type in ("CE", "PE"):
            block = fetch_series(offset, option_type, from_date, to_date,
                                 interval=interval, expiry_flag=expiry_flag,
                                 expiry_code=expiry_code, security_id=security_id)
            if not block:
                missing.append(f"{_offset_label(offset)} {option_type}")
                continue
            for row in _rows(block, interval_minutes=int(interval)):
                ts = row["timestamp"]
                if row.get("spot") is not None:
                    spot_by_time[ts] = float(row["spot"])
                by_time.setdefault(ts, []).append(
                    OptionQuote(
                        symbol=symbol,
                        # The rolling series doesn't name the contract's
                        # expiry date, only that it was the nearest one
                        # at that timestamp, so label it honestly rather
                        # than inventing a date that might be wrong.
                        # UPDATE 2026-08-19: gamma_exposure.py became the
                        # first downstream consumer that legitimately
                        # needs a real date (Black-Scholes gamma needs
                        # time-to-expiry) -- see nominal_expiry_date()
                        # below, which reconstructs it from NIFTY's known
                        # weekly-expiry weekday regime rather than
                        # guessing. Still correct for anything that
                        # doesn't call that: this field itself remains
                        # the honest "rolling:weekN" label.
                        expiry=f"rolling:{expiry_flag.lower()}{expiry_code}",
                        strike=float(row.get("strike") or 0.0),
                        option_type=option_type,
                        ltp=float(row.get("close") or 0.0),
                        oi=int(row.get("oi") or 0),
                        # Cross-cycle deltas are recomputed at replay time
                        # from consecutive snapshots, exactly as they are
                        # for live recordings -- see snapshot_recorder's
                        # note on what is recomputable.
                        oi_change_pct=0.0,
                        volume=int(row.get("volume") or 0),
                        iv=float(row.get("iv") or 0.0),
                        iv_percentile=50.0,  # no history to rank against here; neutral, as in nse_source
                        timestamp=ts,
                        # bid/ask/Greeks deliberately absent -- see module docstring.
                    )
                )

    if missing:
        log.info(f"  [historical] no data returned for {len(missing)} series: {', '.join(missing[:6])}"
                 + (" ..." if len(missing) > 6 else ""))

    snapshots = []
    for ts in sorted(by_time):
        chain = by_time[ts]
        spot = spot_by_time.get(ts)
        if spot is None:
            continue  # no spot means no ATM reference; the cycle is unusable
        _apply_iv_percentiles(chain)
        total_ce = sum(q.oi for q in chain if q.option_type == "CE")
        total_pe = sum(q.oi for q in chain if q.option_type == "PE")
        snapshots.append(
            MarketSnapshot(
                symbol=symbol,
                spot=spot,
                vwap=spot,  # no published intraday VWAP; same placeholder nse_source uses
                pcr=round(total_pe / total_ce, 2) if total_ce else 0.0,
                chain=chain,
                timestamp=ts,
                oi_analysis=oi_analytics.analyze(chain, spot),
                source="dhan_historical",
            )
        )
    return snapshots


def _apply_iv_percentiles(chain: list):
    """
    Rank each quote's IV against the other strikes of its OWN option type
    in the same cycle, in place.

    WHY THIS IS NOT LEFT AT A FLAT 50.0
    -----------------------------------
    scanner.py scores "IV rich" and "IV cheap" off `iv_percentile`, and
    nothing else reads it. A constant 50.0 sits between
    IV_PERCENTILE_LOW and IV_PERCENTILE_HIGH, so neither branch can ever
    fire -- the backtest would silently evaluate a momentum scorer with
    its IV component amputated and report the result as if it were the
    live strategy. Measured on a real live day, iv_percentile spans
    3.0-100.0 across 64 distinct values, so this is a large part of the
    scoring surface, not a rounding detail.

    Deliberately mirrors dhan_source's `_cross_sectional_percentile`
    exactly, INCLUDING its treatment of zero IVs as ordinary values and
    its <5-strike bail-out. Roughly 18% of quotes carry iv == 0.0 --
    Dhan simply doesn't publish IV for thinner strikes, and the live
    recording shows the identical 18.6% rate, so this is normal data
    rather than corruption. "Correct" here means matching what the live
    system actually does with these numbers, not what would be
    theoretically better: the backtest exists to evaluate the code that
    runs, warts included.
    """
    for option_type in ("CE", "PE"):
        side = [q for q in chain if q.option_type == option_type]
        ivs = sorted(q.iv for q in side)
        if len(ivs) < 5:
            continue  # leave the neutral 50.0; too few strikes to rank meaningfully
        for q in side:
            below = sum(1 for x in ivs if x <= q.iv)
            q.iv_percentile = round(below / len(ivs) * 100, 1)


def coverage(snapshot: MarketSnapshot) -> dict:
    """
    How wide a strike window this reconstructed snapshot actually spans,
    so a caller can tell whether the strategy it is about to evaluate
    even fits inside the data.

    Reported rather than assumed BECAUSE of the ATM+/-10 cap: an iron
    condor whose hedge sits 700pts out will find no quote and look like
    it simply had no opportunity, when in truth the data never contained
    its legs. That is the 2026-07-30 truncated-chain failure mode again,
    and it must not be able to masquerade as a result.
    """
    if not snapshot.chain:
        return {"strikes": 0, "min_strike": None, "max_strike": None, "points_from_spot": 0.0}
    strikes = sorted({q.strike for q in snapshot.chain})
    return {
        "strikes": len(strikes),
        "min_strike": strikes[0],
        "max_strike": strikes[-1],
        "points_from_spot": round(
            min(snapshot.spot - strikes[0], strikes[-1] - snapshot.spot), 1
        ),
    }


def _chunks(from_date: str, to_date: str, days: int = MAX_DAYS_PER_CALL):
    """Split a range into <=`days` windows, since the API caps each call."""
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    while start <= end:
        stop = min(start + timedelta(days=days - 1), end)
        yield start.isoformat(), stop.isoformat()
        start = stop + timedelta(days=1)


def apply_oi_buildup(cycles: list):
    """
    Fill in OI-buildup fields across a day's cycles, in place.

    WHY THIS IS ESSENTIAL, NOT AN EXTRA
    -----------------------------------
    OI buildup is the single largest scoring input the momentum scanner
    has. Measured on a real live day it fired 5,517 times, more than any
    other reason; on a reconstructed day it fired ZERO times, because the
    rolling-option endpoint gives a per-bar OI snapshot with no history
    attached and every quote was written with buildup_type=None.

    The consequence was not a slightly weaker backtest -- it was a dead
    one. Historical scores topped out at 3.0 against a
    MIN_CONVICTION_SCORE_TO_TRACK of 5.0, so the momentum strategy took
    exactly zero trades across the whole 2-year pull. Reported without
    this, "no trades in 493 days" would have looked like a devastating
    verdict on the strategy when it was really a missing column.

    HOW
    ---
    dhan_source builds these live from a rolling window of its own past
    samples. Sequential snapshots ARE that history, so the same window can
    be reconstructed offline -- this is exactly the recomputability
    snapshot_recorder's docstring describes.

    _intraday_change and _classify_buildup are imported from dhan_source
    rather than reimplemented, so the classification cannot drift from
    what live does. The day-cumulative `oi_change_pct` stays 0.0: it needs
    the previous session's closing OI, which this endpoint doesn't
    provide. Live falls back to the cumulative figure only in the opening
    minutes before enough samples exist, so the intraday path -- the one
    reconstructed here -- is what runs for nearly the whole session.
    """
    import config as cfg
    from dhan_source import _intraday_change, _classify_buildup, _prune_samples

    samples = {}  # (strike, option_type) -> [{"t", "oi", "ltp"}, ...]

    for snapshot, _candles, _meta in cycles:
        now_ts = snapshot.timestamp.timestamp()
        for q in snapshot.chain:
            key = (q.strike, q.option_type)
            past = samples.get(key, [])

            oi_intraday, price_intraday, window = _intraday_change(
                past, now_ts, q.oi, q.ltp
            )
            q.oi_change_pct_intraday = oi_intraday
            q.price_change_pct_intraday = price_intraday
            q.buildup_window_minutes = window
            if oi_intraday is not None and price_intraday is not None:
                q.buildup_type = _classify_buildup(
                    oi_intraday, price_intraday, cfg.OI_INTRADAY_BUILDUP_PCT
                )

            past = _prune_samples(past, now_ts)
            past.append({"t": now_ts, "oi": q.oi, "ltp": q.ltp})
            samples[key] = past


def repair_oi_buildup(days: list = None, snapshot_dir=None) -> dict:
    """
    Recompute OI-buildup fields on already-written historical days.

    Pure recomputation from chains already on disk -- no API calls, so the
    2-year pull needs no re-fetching. Refuses to touch live recordings,
    which already carry what dhan_source computed at the time.
    """
    import gzip
    import json
    import snapshot_recorder

    target_dir = snapshot_dir or snapshot_recorder.SNAPSHOT_DIR
    days = days or snapshot_recorder.available_days(snapshot_dir=target_dir)
    repaired = {}

    for day in days:
        path = snapshot_recorder.path_for(day, snapshot_dir=target_dir)
        if not path.exists():
            continue
        cycles = list(snapshot_recorder.load_day(day, snapshot_dir=target_dir))
        if not cycles or cycles[0][0].source != "dhan_historical":
            continue

        apply_oi_buildup(cycles)

        with gzip.open(path, "wt", encoding="utf-8") as f:
            for snap, candles, meta in cycles:
                f.write(json.dumps({
                    "timestamp": snap.timestamp.isoformat(),
                    "logic_version": meta.get("logic_version"),
                    "spot": snap.spot,
                    "vwap": snap.vwap,
                    "pcr": snap.pcr,
                    "source": snap.source,
                    "chain": [snapshot_recorder._quote_to_dict(q) for q in snap.chain],
                    "candles": [snapshot_recorder._candle_to_dict(c) for c in candles],
                }, default=str) + "\n")
        repaired[day] = sum(
            1 for snap, _c, _m in cycles for q in snap.chain if q.buildup_type
        )

    return repaired


def repair_iv_percentiles(days: list = None) -> dict:
    """
    Recompute `iv_percentile` in place on already-written historical days.

    Needed because the first backfill wrote a flat 50.0 (see
    _apply_iv_percentiles for why that silently amputates scanner.py's IV
    scoring). iv_percentile is derived entirely from the chain, which is
    already on disk, so this is pure recomputation -- no API calls, and
    the 2-year pull does not need re-fetching.

    Refuses to touch live recordings, same reasoning as backfill_candles:
    those hold the percentiles dhan_source computed at the time, which
    are what the live system actually saw.
    """
    import gzip
    import json
    import snapshot_recorder

    days = days or snapshot_recorder.available_days()
    repaired = {}

    for day in days:
        path = snapshot_recorder.path_for(day)
        if not path.exists():
            continue
        cycles = list(snapshot_recorder.load_day(day))
        if not cycles or cycles[0][0].source != "dhan_historical":
            continue

        for snap, _candles, _meta in cycles:
            _apply_iv_percentiles(snap.chain)

        with gzip.open(path, "wt", encoding="utf-8") as f:
            for snap, candles, meta in cycles:
                f.write(json.dumps({
                    "timestamp": snap.timestamp.isoformat(),
                    "logic_version": meta.get("logic_version"),
                    "spot": snap.spot,
                    "vwap": snap.vwap,
                    "pcr": snap.pcr,
                    "source": snap.source,
                    "chain": [snapshot_recorder._quote_to_dict(q) for q in snap.chain],
                    "candles": [snapshot_recorder._candle_to_dict(c) for c in candles],
                }, default=str) + "\n")
        repaired[day] = len(cycles)

    return repaired


def backfill_candles(days: list = None, interval: str = "5",
                     snapshot_dir=None, candle_fetcher=None) -> dict:
    """
    Attach real index candles to reconstructed days.

    `candle_fetcher(interval=, from_date=, to_date=)` defaults to
    dhan_source.get_nifty_intraday_candles; pass
    banknifty_context.get_banknifty_candles for Bank Nifty -- same
    signature shape, different endpoint underneath (see that function's
    own docstring for why it's a separate function, not a shared one
    with a symbol parameter).

    WHY THIS IS A SEPARATE PASS
    ---------------------------
    The rolling-option endpoint returns a per-bar `spot` value but no
    index OHLC, so backfill() writes `"candles": []`. That is not a
    cosmetic gap: price_action.analyze() drives support/resistance levels
    and the ATR that plan_generator uses to size every stop, so replaying
    momentum against candle-less days would silently evaluate a crippled
    version of the strategy and report the result as if it were the real
    one.

    Synthesising candles from the spot series was the obvious shortcut and
    is deliberately NOT taken -- one spot print per 5-minute bar gives no
    intra-bar high or low, so every ATR derived from it would be far too
    small and every stop correspondingly too tight. Dhan's /charts/intraday
    endpoint returns genuine index OHLC and was probed back two full years
    (2024-08-01 returns a complete 74-candle session), so the real thing is
    available for the entire backfill window.

    Costs ONE extra API call per day, versus 42 for the option chain, which
    is why this is worth doing as its own pass rather than folding into
    backfill() and re-fetching everything.

    Each cycle gets only the candles at or before its own timestamp, which
    is exactly what main_live.py hands the recorder live -- a cycle must
    never see a candle from its own future.
    """
    import gzip
    import json
    import dhan_source
    import snapshot_recorder

    fetch = candle_fetcher or dhan_source.get_nifty_intraday_candles
    target_dir = snapshot_dir or snapshot_recorder.SNAPSHOT_DIR
    days = days or snapshot_recorder.available_days(snapshot_dir=target_dir)
    updated = {}

    for day in days:
        path = snapshot_recorder.path_for(day, snapshot_dir=target_dir)
        if not path.exists():
            continue

        cycles = list(snapshot_recorder.load_day(day, snapshot_dir=target_dir))
        if not cycles:
            continue
        # Only ever touch reconstructed days. A live recording already has
        # the candles main_live.py captured at the time, and rewriting one
        # would replace what actually happened with a later re-fetch.
        if cycles[0][0].source != "dhan_historical":
            continue
        if any(candles for _snap, candles, _meta in cycles):
            continue  # already enriched

        try:
            day_candles = fetch(
                interval=interval,
                # 09:14, not 09:15 -- Dhan's fromDate is exclusive, so
                # 09:15 silently dropped each session's opening bar from
                # every reconstructed day. See
                # dhan_source.SESSION_FETCH_FROM_TIME.
                from_date=f"{day} {dhan_source.SESSION_FETCH_FROM_TIME}",
                to_date=f"{day} 15:30:00",
            )
        except Exception as e:
            log.info(f"  [historical] candles for {day} failed, leaving as-is: {e}")
            continue
        if not day_candles:
            log.info(f"  [historical] no candles returned for {day}, leaving as-is")
            continue

        with gzip.open(path, "wt", encoding="utf-8") as f:
            for snap, _old_candles, meta in cycles:
                visible = [c for c in day_candles if c.timestamp <= snap.timestamp]
                f.write(json.dumps({
                    "timestamp": snap.timestamp.isoformat(),
                    "logic_version": meta.get("logic_version"),
                    "spot": snap.spot,
                    "vwap": snap.vwap,
                    "pcr": snap.pcr,
                    "source": snap.source,
                    "chain": [snapshot_recorder._quote_to_dict(q) for q in snap.chain],
                    "candles": [snapshot_recorder._candle_to_dict(c) for c in visible],
                }, default=str) + "\n")

        updated[day] = len(day_candles)
        log.info(f"  [historical] {day}: attached {len(day_candles)} candles")

    return updated


def backfill(from_date: str, to_date: str, interval: str = "5",
            security_id: int = NIFTY_SECURITY_ID, symbol: str = "NIFTY",
            expiry_flag: str = "WEEK", snapshot_dir=None) -> dict:
    """
    Pull a date range and write it into logs/snapshots/ (or `snapshot_dir`
    if given) in the SAME format snapshot_recorder writes live, so
    replay.py and shadow.py load it through the existing load_day() path
    with no idea it came from a different source (beyond the
    "dhan_historical" source tag, which is kept precisely so historical
    and live days can be told apart and never pooled by accident).

    `snapshot_dir` MUST be set to something other than the default when
    `symbol` isn't "NIFTY" -- both underlyings' data would otherwise
    fight over the exact same YYYYMMDD.jsonl.gz filenames for any
    overlapping date, and one would silently overwrite the other's real
    history. See BANKNIFTY_SECURITY_ID's own comment for the values to
    pass for Bank Nifty.

    Returns {day: cycles_written}.
    """
    import gzip
    import json
    import snapshot_recorder
    import historical_consistency as hc

    target_dir = snapshot_dir or snapshot_recorder.SNAPSHOT_DIR

    written = {}
    all_day_snaps = {}
    for chunk_from, chunk_to in _chunks(from_date, to_date):
        log.info(f"  [historical] fetching {chunk_from} .. {chunk_to}")
        snapshots = reconstruct_range(chunk_from, chunk_to, interval=interval,
                                      security_id=security_id, symbol=symbol,
                                      expiry_flag=expiry_flag)

        by_day = {}
        for snap in snapshots:
            by_day.setdefault(snap.timestamp.date(), []).append(snap)
        all_day_snaps.update({d.isoformat(): s for d, s in by_day.items()})

        target_dir.mkdir(parents=True, exist_ok=True)
        for day, day_snaps in by_day.items():
            path = snapshot_recorder.path_for(day, snapshot_dir=target_dir)
            if path.exists():
                # Never append to an existing day: that would interleave
                # reconstructed cycles with live-recorded ones in a single
                # file, silently mixing two sources of differing fidelity
                # (no bid/ask, no Greeks here) into one "day" that no
                # downstream consumer could separate again.
                log.info(f"  [historical] {day} already on disk, skipping (delete it to re-pull)")
                continue
            with gzip.open(path, "wt", encoding="utf-8") as f:
                for snap in day_snaps:
                    f.write(json.dumps({
                        "timestamp": snap.timestamp.isoformat(),
                        "logic_version": None,  # not produced by any decision logic
                        "spot": snap.spot,
                        "vwap": snap.vwap,
                        "pcr": snap.pcr,
                        "source": snap.source,
                        "chain": [snapshot_recorder._quote_to_dict(q) for q in snap.chain],
                        "candles": [],  # reconstructed from the chain's spot series at replay time
                    }, default=str) + "\n")
            written[day.isoformat()] = len(day_snaps)
            log.info(f"  [historical] wrote {day}: {len(day_snaps)} cycles")

    if written:
        sweep = hc.check_range(
            {d: s for d, s in all_day_snaps.items() if d in written},
            interval_minutes=int(interval),
        )
        log.info("  " + hc.describe(sweep).replace("\n", "\n  "))
        if not sweep["passed"]:
            log.info(
                f"  [historical] {len(sweep['failed_days'])} day(s) failed consistency "
                f"checks -- look before trusting any backtest that includes them"
            )

    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--from", dest="from_date", help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="YYYY-MM-DD")
    parser.add_argument("--interval", default="5", choices=["1", "5", "15", "25", "60"])
    parser.add_argument("--probe", action="store_true",
                        help="fetch one range and report coverage without writing to disk")
    parser.add_argument("--candles", action="store_true",
                        help="attach real index candles to already-reconstructed days "
                             "(second pass; 1 API call per day)")
    parser.add_argument("--repair-iv", dest="repair_iv", action="store_true",
                        help="recompute iv_percentile on already-written days (no API calls)")
    parser.add_argument("--repair-buildup", dest="repair_buildup", action="store_true",
                        help="recompute OI buildup fields on already-written days (no API calls)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.repair_iv:
        repaired = repair_iv_percentiles()
        print(f"{len(repaired)} day(s) had iv_percentile recomputed")
        return

    if args.repair_buildup:
        repaired = repair_oi_buildup()
        total = sum(repaired.values())
        print(f"{len(repaired)} day(s) had OI buildup recomputed "
              f"({total:,} classified quotes)")
        return

    if args.candles:
        updated = backfill_candles(interval=args.interval)
        print(f"{len(updated)} day(s) enriched with index candles")
        return

    if not args.from_date or not args.to_date:
        parser.error("--from and --to are required unless using --candles")

    if args.probe:
        snaps = reconstruct_range(args.from_date, args.to_date, interval=args.interval)
        print(f"{len(snaps)} cycles reconstructed")
        if snaps:
            mid = snaps[len(snaps) // 2]
            cov = coverage(mid)
            print(f"sample cycle {mid.timestamp}: spot {mid.spot}, "
                  f"{cov['strikes']} strikes, {cov['min_strike']}-{cov['max_strike']} "
                  f"(+/-{cov['points_from_spot']}pts of spot)")
        return

    written = backfill(args.from_date, args.to_date, interval=args.interval)
    print(f"{len(written)} day(s) written, {sum(written.values())} cycles total")


if __name__ == "__main__":
    main()
