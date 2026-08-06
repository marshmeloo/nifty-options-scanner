"""
Tests for config.TIEBREAK_MODE -- how the scanner chooses among
candidates whose SCORES ARE IDENTICAL.

WHY THIS MATTERS (found live 2026-08-05)
----------------------------------------
Momentum ROC is a CHAIN-WIDE read, so under SCORING_MODE="momentum_only"
every qualifying strike on the same side scores exactly the same. On
2026-08-05 11:51, 14 PE strikes all tied at 6.0 -- and the winner was
decided purely by Python's stable sort preserving CHAIN ORDER, not by
any trading judgement. That resolved ASYMMETRICALLY by side: ascending
strike means "cheapest, deepest OTM" for a PE but "most expensive still
under PREMIUM_MAX" for a CE, which the live journal confirms (PE entries
clustered at the premium floor, CE entries at the cap).

These tests pin that the tie-break is now an explicit, per-mode choice,
and -- critically -- that the DEFAULT still reproduces the old chain-order
behaviour exactly, since the 493-day study that adopted momentum_only ran
with it and changing the default silently would move the strategy off its
own evidence.

Run: python -m pytest tests/ -q
"""

import datetime

import pytest

import config
import scanner
from models import MarketSnapshot, OptionQuote


def _quote(strike, option_type="PE", oi_change_pct=0.0, oi_change_pct_intraday=None):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-07", strike=strike, option_type=option_type,
        ltp=50.0, oi=50000, oi_change_pct=oi_change_pct, volume=1000, iv=15.0,
        iv_percentile=50.0, timestamp=datetime.datetime.now(),
        oi_change_pct_intraday=oi_change_pct_intraday,
    )


def _snapshot(chain, spot=24500.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0,
                          chain=chain, timestamp=datetime.datetime.now())


# --- _tiebreak_value itself -----------------------------------------------

def test_chain_order_mode_returns_the_chain_index(monkeypatch):
    monkeypatch.setattr(config, "TIEBREAK_MODE", "chain_order")
    snap = _snapshot([])
    assert scanner._tiebreak_value(_quote(23900.0), snap, chain_index=7) == 7


def test_nearest_atm_mode_returns_distance_from_spot(monkeypatch):
    monkeypatch.setattr(config, "TIEBREAK_MODE", "nearest_atm")
    snap = _snapshot([], spot=24500.0)
    # Lower sorts first, so the closer strike must produce the smaller value.
    near = scanner._tiebreak_value(_quote(24450.0), snap, chain_index=0)
    far = scanner._tiebreak_value(_quote(23900.0), snap, chain_index=0)
    assert near == 50.0
    assert far == 600.0
    assert near < far


def test_highest_oi_change_mode_prefers_the_largest_buildup(monkeypatch):
    monkeypatch.setattr(config, "TIEBREAK_MODE", "highest_oi_change")
    snap = _snapshot([])
    big = scanner._tiebreak_value(_quote(24000.0, oi_change_pct=300.0), snap, chain_index=0)
    small = scanner._tiebreak_value(_quote(24000.0, oi_change_pct=20.0), snap, chain_index=0)
    assert big < small  # negated, so the biggest buildup sorts first


def test_highest_oi_change_prefers_the_intraday_figure_when_present(monkeypatch):
    """
    Same reasoning as the scanner's own buildup scoring: the daily figure
    is measured against yesterday's close and only ever grows through the
    session, so it saturates and stops discriminating. The intraday
    figure is the fresher signal whenever it exists.
    """
    monkeypatch.setattr(config, "TIEBREAK_MODE", "highest_oi_change")
    snap = _snapshot([])
    q = _quote(24000.0, oi_change_pct=500.0, oi_change_pct_intraday=10.0)
    assert scanner._tiebreak_value(q, snap, chain_index=0) == -10.0


def test_unknown_mode_falls_back_to_chain_order(monkeypatch):
    monkeypatch.setattr(config, "TIEBREAK_MODE", "not_a_real_mode")
    snap = _snapshot([])
    assert scanner._tiebreak_value(_quote(24000.0), snap, chain_index=3) == 3


# --- end-to-end through scan() --------------------------------------------

def _tied_pe_chain():
    """
    Five PE strikes that will all score identically under momentum_only.
    Deliberately listed in ascending-strike chain order, which is exactly
    what produced the accidental deep-OTM bias live.
    """
    return [
        _quote(23900.0, "PE", oi_change_pct=50.0),
        _quote(24000.0, "PE", oi_change_pct=90.0),
        _quote(24100.0, "PE", oi_change_pct=400.0),   # strongest buildup
        _quote(24200.0, "PE", oi_change_pct=30.0),
        _quote(24450.0, "PE", oi_change_pct=60.0),    # nearest to spot (24500)
    ]


def _context_bearish():
    import price_action
    base = datetime.datetime(2026, 8, 3, 9, 15)
    candles = []
    price = 24500.0
    n = max(config.ROC_PERIOD + 5, 20)
    for i in range(n):
        candles.append(__import__("models").Candle(
            timestamp=base + datetime.timedelta(minutes=5 * i),
            open=price, high=price + 1, low=price - 1, close=price, volume=1000))
    moved = price * 0.997  # -0.3%, bearish -> aligns with PE
    candles.append(__import__("models").Candle(
        timestamp=base + datetime.timedelta(minutes=5 * n),
        open=price, high=price, low=moved, close=moved, volume=1000))
    _levels, ctx = price_action.analyze_with_context(candles)
    return ctx


def test_all_candidates_really_do_tie_under_momentum_only(monkeypatch):
    """The premise of this whole module -- if scores weren't tied, the
    tie-break would be irrelevant."""
    monkeypatch.setattr(config, "SCORING_MODE", "momentum_only")
    snap = _snapshot(_tied_pe_chain())
    setups = scanner.scan(snap, context=_context_bearish())
    scores = {s.score for s in setups}
    assert len(setups) == 5
    assert len(scores) == 1, f"expected one tied score, got {scores}"


@pytest.mark.parametrize("mode,expected_first", [
    ("chain_order", 23900.0),        # first in chain order = deepest OTM
    ("nearest_atm", 24450.0),        # closest to spot 24500
    ("highest_oi_change", 24100.0),  # +400% buildup
])
def test_each_mode_picks_its_own_winner(monkeypatch, mode, expected_first):
    monkeypatch.setattr(config, "SCORING_MODE", "momentum_only")
    monkeypatch.setattr(config, "TIEBREAK_MODE", mode)
    snap = _snapshot(_tied_pe_chain())
    setups = scanner.scan(snap, context=_context_bearish())
    assert setups[0].strike == expected_first


def test_tiebreak_never_overrides_a_real_score_difference(monkeypatch):
    """
    A tie-break must only apply WITHIN equal scores. A genuinely
    higher-scoring candidate has to keep winning regardless of mode,
    otherwise this stops being a tie-break and starts being a scorer.
    """
    monkeypatch.setattr(config, "SCORING_MODE", "legacy")
    monkeypatch.setattr(config, "TIEBREAK_MODE", "nearest_atm")

    chain = [
        # Far from spot, but IV-cheap so it scores higher under legacy.
        _quote(23900.0, "PE"),
        _quote(24450.0, "PE"),   # nearest ATM, but no scoring edge
    ]
    chain[0].iv_percentile = 5.0     # "IV cheap" -> positive score
    chain[1].iv_percentile = 50.0    # neutral

    snap = _snapshot(chain)
    setups = scanner.scan(snap, context=_context_bearish())
    assert setups[0].score > setups[1].score
    assert setups[0].strike == 23900.0  # score wins, not proximity
