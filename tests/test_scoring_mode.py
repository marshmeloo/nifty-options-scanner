"""
Tests for config.SCORING_MODE == "momentum_only", adopted live 2026-08-02
after the 493-day forward-return study found momentum ROC alignment was
the strongest predictive component by a wide margin while carrying the
smallest weight in the legacy scorer. See config.py's SCORING_MODE
docstring for the full evidence trail.

Run: python -m pytest tests/ -q
"""

import datetime

import pytest

import config
from scanner import scan
from models import Candle, MarketSnapshot, OptionQuote


def _quote(strike=24000.0, option_type="PE"):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-04", strike=strike, option_type=option_type,
        ltp=100.0, oi=50000, oi_change_pct=0.0, volume=1000, iv=15.0,
        iv_percentile=50.0, timestamp=datetime.datetime.now(),
    )


def _candles_with_roc(pct: float, n: int = 20):
    """A flat run then a final bar moving `pct`% -- gives context.roc_pct."""
    base = datetime.datetime(2026, 8, 3, 9, 15)
    candles = []
    price = 24000.0
    for i in range(n):
        candles.append(Candle(timestamp=base + datetime.timedelta(minutes=5 * i),
                              open=price, high=price + 1, low=price - 1,
                              close=price, volume=1000))
    moved = price * (1 + pct / 100)
    candles.append(Candle(timestamp=base + datetime.timedelta(minutes=5 * n),
                          open=price, high=max(price, moved), low=min(price, moved),
                          close=moved, volume=1000))
    return candles


def _context_with_roc(pct: float):
    import price_action
    candles = _candles_with_roc(pct, n=max(config.ROC_PERIOD + 5, 20))
    _levels, ctx = price_action.analyze_with_context(candles)
    return ctx


def test_momentum_aligned_gets_the_aligned_score(monkeypatch):
    monkeypatch.setattr(config, "SCORING_MODE", "momentum_only")
    ctx = _context_with_roc(config.ROC_SIGNIFICANT_PCT + 1.0)  # bullish move
    snap = MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0,
                          chain=[_quote(option_type="CE")], timestamp=datetime.datetime.now())
    setups = scan(snap, context=ctx)
    assert setups
    assert any("Momentum aligned" in r for r in setups[0].reasons)
    assert setups[0].score == config.MOMENTUM_ONLY_ALIGNED_SCORE


def test_momentum_against_gets_the_against_score(monkeypatch):
    monkeypatch.setattr(config, "SCORING_MODE", "momentum_only")
    ctx = _context_with_roc(config.ROC_SIGNIFICANT_PCT + 1.0)  # bullish move
    snap = MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0,
                          chain=[_quote(option_type="PE")], timestamp=datetime.datetime.now())
    setups = scan(snap, context=ctx)
    assert setups
    assert any("Momentum against" in r for r in setups[0].reasons)
    assert setups[0].score == config.MOMENTUM_ONLY_AGAINST_SCORE


def test_only_aligned_score_clears_the_conviction_bar():
    """
    The whole point of adopting this mode: only momentum-aligned
    candidates should be able to reach MIN_CONVICTION_SCORE_TO_TRACK.
    """
    assert config.MOMENTUM_ONLY_ALIGNED_SCORE >= config.MIN_CONVICTION_SCORE_TO_TRACK
    assert config.MOMENTUM_ONLY_AGAINST_SCORE < config.MIN_CONVICTION_SCORE_TO_TRACK
    assert config.MOMENTUM_ONLY_NEUTRAL_SCORE < config.MIN_CONVICTION_SCORE_TO_TRACK


def test_reasons_are_preserved_under_the_override(monkeypatch):
    """
    apply_learned_adjustment and the decision log read `reasons`, not the
    override math -- the override must only change what final_score is,
    never erase the component breakdown that produced it.
    """
    monkeypatch.setattr(config, "SCORING_MODE", "momentum_only")
    ctx = _context_with_roc(config.ROC_SIGNIFICANT_PCT + 1.0)
    snap = MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0,
                          chain=[_quote(option_type="CE")], timestamp=datetime.datetime.now())
    setups = scan(snap, context=ctx)
    assert len(setups[0].reasons) >= 1


def test_legacy_mode_computes_the_real_weighted_score_not_the_override(monkeypatch):
    """
    Switching back to "legacy" is meant to be a one-line, fully-reversible
    change: the score must come from the weighted component sum, not
    silently keep applying the momentum_only override.
    """
    monkeypatch.setattr(config, "SCORING_MODE", "legacy")
    ctx = _context_with_roc(config.ROC_SIGNIFICANT_PCT + 1.0)
    snap = MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0,
                          chain=[_quote(option_type="CE")], timestamp=datetime.datetime.now())
    momentum_only_setups = scan(snap, context=ctx)  # SCORING_MODE still "legacy" here

    monkeypatch.setattr(config, "SCORING_MODE", "momentum_only")
    override_setups = scan(snap, context=ctx)

    # Same candidate, same reasons, but legacy's raw weighted sum must
    # differ from the override constant momentum_only would have applied.
    assert momentum_only_setups[0].reasons == override_setups[0].reasons
    assert momentum_only_setups[0].score != config.MOMENTUM_ONLY_ALIGNED_SCORE
    assert override_setups[0].score == config.MOMENTUM_ONLY_ALIGNED_SCORE


def test_scoring_mode_is_fingerprinted_in_logic_version(monkeypatch):
    """Results under the two modes must never be silently pooled."""
    import logic_version

    logic_version.compute.cache_clear()
    monkeypatch.setattr(config, "SCORING_MODE", "legacy")
    v1 = logic_version.compute()
    logic_version.compute.cache_clear()
    monkeypatch.setattr(config, "SCORING_MODE", "momentum_only")
    v2 = logic_version.compute()
    logic_version.compute.cache_clear()

    assert v1 != v2
