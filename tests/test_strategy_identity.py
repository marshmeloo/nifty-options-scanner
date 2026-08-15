"""
Tests for the named strategy identity stamped onto every real trade
(config.STRATEGY_NAME / STRATEGY_VERSION, see STRATEGY_VERSIONS.md).

Deliberately separate from logic_version.py, which is designed to
change whenever any decision-relevant setting does -- this stamp is
the opposite, a stable label a later comparison can group by, and its
whole point breaks if it moves when it shouldn't.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import config
import trade_tracker as tt
from models import MarketSnapshot, Setup, TradePlan


def _setup():
    return Setup("NIFTY", 24050.0, "PE", "2026-08-15", ["long_buildup"], 6.0)


def _plan(setup):
    return TradePlan(setup=setup, entry=100.0, target=160.0, stop=70.0,
                     invalidation="stop hit", lots=1, capital_at_risk=1950.0,
                     risk_pct_of_capital=0.39, risk_level="Low", stop_basis="ATR")


def _snapshot():
    return MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [], timestamp=datetime(2026, 8, 15, 10, 0))


def test_new_trade_is_stamped_with_the_live_strategy_identity():
    setup = _setup()
    trade = tt.open_new_trade(setup, _plan(setup), _snapshot())
    assert trade["strategy_name"] == config.STRATEGY_NAME
    assert trade["strategy_version"] == config.STRATEGY_VERSION


def test_strategy_identity_is_independent_of_the_logic_version_hash():
    """The whole point of a NAMED version is that it doesn't move when
    the hash does -- conflating the two would defeat the reason this
    exists (a stable label to group live results by)."""
    setup = _setup()
    trade = tt.open_new_trade(setup, _plan(setup), _snapshot())
    assert trade["strategy_name"] != trade["logic_version"]
    assert trade["strategy_version"] != trade["logic_version"]


def test_strategy_name_is_not_a_decision_relevant_setting():
    """Changing the NAME must never churn logic_version.py's hash --
    that hash exists to flag when DECISIONS change, and a label is not
    a decision."""
    import logic_version
    assert "STRATEGY_NAME" not in logic_version.DECISION_RELEVANT_SETTINGS
    assert "STRATEGY_VERSION" not in logic_version.DECISION_RELEVANT_SETTINGS


def test_missing_strategy_identity_degrades_to_none_not_a_crash(monkeypatch):
    """A checkout that predates this feature (or a config missing the
    constant for any reason) must not fail to open a trade over a label."""
    monkeypatch.delattr(config, "STRATEGY_NAME", raising=False)
    monkeypatch.delattr(config, "STRATEGY_VERSION", raising=False)
    setup = _setup()
    trade = tt.open_new_trade(setup, _plan(setup), _snapshot())
    assert trade["strategy_name"] is None
    assert trade["strategy_version"] is None
