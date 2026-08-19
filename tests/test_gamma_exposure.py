"""
Tests for gamma_exposure.py -- RESEARCH ONLY, not wired into any live
decision (see that module's own docstring). These pin the mechanics:
correct aggregation, sign convention, zero-gamma interpolation, and wall
detection -- not any claim about whether GEX actually predicts anything,
which is what gamma_exposure_study.py's backtest exists to answer.

Run: python -m pytest tests/test_gamma_exposure.py -q
"""

from datetime import datetime

import pytest

from research import gamma_exposure as gex
from models import MarketSnapshot, OptionQuote


NOW = datetime(2026, 8, 18, 10, 0)
EXPIRY = "2026-08-25"   # 6.8 days out from NOW -- matches the validated black_scholes range


def _quote(strike, option_type, oi, iv_pct=10.0, expiry=EXPIRY):
    return OptionQuote(
        symbol="NIFTY", expiry=expiry, strike=strike, option_type=option_type,
        ltp=100.0, oi=oi, oi_change_pct=0.0, volume=0, iv=iv_pct, iv_percentile=50.0,
    )


def _snapshot(quotes, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=quotes, timestamp=NOW)


LOT_SIZE = 65


# --------------------------------------------------------------------------
# Basic aggregation and regime
# --------------------------------------------------------------------------

def test_empty_chain_is_safe():
    result = gex.compute(_snapshot([]), LOT_SIZE, NOW)
    assert result.net_gex == 0.0
    assert result.regime == "LONG_GAMMA"   # 0 is not < 0, so it lands on the LONG_GAMMA side by convention
    assert result.zero_gamma_level is None
    assert result.gamma_call_wall_strike is None
    assert result.gamma_put_wall_strike is None


def test_put_heavy_chain_is_short_gamma():
    """Sign convention: PE OI subtracts from net GEX. A chain with much
    heavier put OI than call OI at every strike should net negative."""
    quotes = [
        _quote(24000, "CE", oi=1000), _quote(24000, "PE", oi=50000),
        _quote(24100, "CE", oi=1000), _quote(24100, "PE", oi=50000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.net_gex < 0
    assert result.regime == "SHORT_GAMMA"


def test_call_heavy_chain_is_long_gamma():
    quotes = [
        _quote(24000, "CE", oi=50000), _quote(24000, "PE", oi=1000),
        _quote(24100, "CE", oi=50000), _quote(24100, "PE", oi=1000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.net_gex > 0
    assert result.regime == "LONG_GAMMA"


def test_missing_one_side_at_a_strike_is_treated_as_zero_not_a_crash():
    quotes = [_quote(24000, "CE", oi=10000)]   # no PE at this strike at all
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.net_gex > 0   # only the CE side contributes, positive by convention


def test_zero_oi_contributes_nothing():
    quotes = [_quote(24000, "CE", oi=0), _quote(24000, "PE", oi=0)]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.net_gex == 0.0


def test_expired_contract_contributes_zero_not_negative_infinity():
    """A quote whose own expiry has already passed relative to `now`
    must contribute 0, via black_scholes.gamma()'s own T<=0 handling --
    not raise, not silently produce a huge or negative number."""
    quotes = [_quote(24000, "CE", oi=10000, expiry="2020-01-01"),
              _quote(24000, "PE", oi=10000, expiry="2020-01-01")]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.net_gex == 0.0


# --------------------------------------------------------------------------
# Zero-gamma level interpolation
# --------------------------------------------------------------------------

def test_zero_gamma_level_interpolates_between_bracketing_strikes():
    """
    zero_gamma_level answers "at what HYPOTHETICAL spot would aggregate
    GEX flip sign", not "walking today's strike ladder at today's
    gamma, where does concentration change" -- those are different
    computations (see gamma_exposure._find_zero_gamma_level's own
    docstring for why the naive strike-walk version this module
    originally shipped with was wrong, caught before this was ever used
    for anything). With OI fixed and gamma recomputed at each candidate
    spot, a chain that's heavily put-OI-weighted at 23800/23900 and
    heavily call-OI-weighted at 24100/24200 should cross near the
    midpoint between those two zones, roughly 24000 -- verified by
    running the real grid search once and reading back where it landed
    (23966.1) rather than asserting a value derived any other way.
    """
    quotes = [
        _quote(23800, "CE", oi=1000), _quote(23800, "PE", oi=60000),
        _quote(23900, "CE", oi=1000), _quote(23900, "PE", oi=60000),
        _quote(24100, "CE", oi=60000), _quote(24100, "PE", oi=1000),
        _quote(24200, "CE", oi=60000), _quote(24200, "PE", oi=1000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.zero_gamma_level is not None
    assert 23900 <= result.zero_gamma_level <= 24100


def test_no_zero_gamma_level_when_the_chain_never_crosses():
    """Every strike net-negative -- cumulative sum never changes sign,
    so there is no crossing to report."""
    quotes = [
        _quote(23800, "CE", oi=1000), _quote(23800, "PE", oi=60000),
        _quote(23900, "CE", oi=1000), _quote(23900, "PE", oi=60000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.zero_gamma_level is None


def test_single_strike_has_no_zero_gamma_level():
    quotes = [_quote(24000, "CE", oi=10000), _quote(24000, "PE", oi=10000)]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.zero_gamma_level is None


def test_zero_gamma_level_is_where_the_regime_actually_flips():
    """
    Implementation-independent check of the DEFINING property: aggregate
    GEX evaluated just below the reported level and just above it must
    have opposite signs, using the same real chain/OI data the level was
    derived from (gamma_exposure._net_gex_at_hypothetical_spot, not
    compute()'s own per_strike, which is evaluated at the actual current
    spot and answers a different question).
    """
    quotes = [
        _quote(23800, "CE", oi=1000), _quote(23800, "PE", oi=60000),
        _quote(23900, "CE", oi=1000), _quote(23900, "PE", oi=60000),
        _quote(24100, "CE", oi=60000), _quote(24100, "PE", oi=1000),
        _quote(24200, "CE", oi=60000), _quote(24200, "PE", oi=1000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    level = result.zero_gamma_level
    assert level is not None

    just_below = gex._net_gex_at_hypothetical_spot(quotes, level - 5, LOT_SIZE, NOW)
    just_above = gex._net_gex_at_hypothetical_spot(quotes, level + 5, LOT_SIZE, NOW)
    assert (just_below < 0) != (just_above < 0), "the regime must actually flip either side of the reported level"


# --------------------------------------------------------------------------
# Call / put walls
# --------------------------------------------------------------------------

def test_call_wall_is_the_strike_with_the_most_call_gamma_exposure():
    quotes = [
        _quote(23900, "CE", oi=5000), _quote(23900, "PE", oi=5000),
        _quote(24000, "CE", oi=80000), _quote(24000, "PE", oi=5000),   # far and away the biggest CE OI
        _quote(24100, "CE", oi=5000), _quote(24100, "PE", oi=5000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.gamma_call_wall_strike == 24000


def test_put_wall_is_the_strike_with_the_most_put_gamma_exposure():
    quotes = [
        _quote(23900, "CE", oi=5000), _quote(23900, "PE", oi=5000),
        _quote(24000, "CE", oi=5000), _quote(24000, "PE", oi=90000),   # far and away the biggest PE OI
        _quote(24100, "CE", oi=5000), _quote(24100, "PE", oi=5000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.gamma_put_wall_strike == 24000


def test_walls_can_differ_from_oi_analytics_walls():
    """The whole reason these are separate fields from OIAnalysis's own
    call_wall_strike/put_wall_strike: a deep-OTM strike can carry the
    largest raw OI in the chain while contributing almost nothing to
    gamma exposure, because gamma itself decays fast away from the
    money. Pin that a huge-OI-but-far-OTM strike does NOT automatically
    win the gamma wall."""
    quotes = [
        # Deep OTM: enormous OI, but far from spot (24000) so gamma is tiny.
        _quote(25000, "CE", oi=500000), _quote(25000, "PE", oi=1000),
        # Near ATM: much smaller OI, but gamma is large this close to spot.
        _quote(24000, "CE", oi=20000), _quote(24000, "PE", oi=1000),
    ]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    assert result.gamma_call_wall_strike == 24000, (
        "near-ATM strike should win on gamma exposure despite the deep-OTM strike's much larger raw OI"
    )


# --------------------------------------------------------------------------
# per_strike shape
# --------------------------------------------------------------------------

def test_per_strike_is_sorted_ascending_and_shaped_correctly():
    quotes = [_quote(24100, "CE", oi=1000), _quote(23900, "PE", oi=1000), _quote(24000, "CE", oi=1000)]
    result = gex.compute(_snapshot(quotes), LOT_SIZE, NOW)
    strikes = [row[0] for row in result.per_strike]
    assert strikes == sorted(strikes)
    for row in result.per_strike:
        assert len(row) == 4   # (strike, call_gex, put_gex, net_gex)
        assert row[1] >= 0 and row[2] >= 0   # per-side magnitudes are never negative


def test_net_gex_and_regime_matches_full_compute():
    """The cheap fast-path (skips the zero-gamma grid search) must agree
    exactly with compute()'s own net_gex/regime for the same inputs --
    it's the same underlying aggregation, just without the extra work."""
    quotes = [
        _quote(23900, "CE", oi=1000), _quote(23900, "PE", oi=40000),
        _quote(24000, "CE", oi=1000), _quote(24000, "PE", oi=40000),
        _quote(24100, "CE", oi=40000), _quote(24100, "PE", oi=1000),
    ]
    snap = _snapshot(quotes)
    full = gex.compute(snap, LOT_SIZE, NOW)
    fast_net, fast_regime = gex.net_gex_and_regime(snap, LOT_SIZE, NOW)
    assert fast_net == full.net_gex
    assert fast_regime == full.regime


def test_historical_caller_must_pass_now_explicitly():
    """compute() takes `now` with no default -- calling it positionally
    wrong (omitting now) must be a TypeError, not a silent wall-clock
    fallback that would zero every historical gamma. This is a
    signature/API test, not a behavioural one."""
    import inspect
    sig = inspect.signature(gex.compute)
    assert sig.parameters["now"].default is inspect.Parameter.empty
