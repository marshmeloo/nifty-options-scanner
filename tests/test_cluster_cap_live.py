"""
Tests for trade_tracker.cluster_cap_blocks() -- the LIVE counterpart of
shadow.correlated_cluster_blocked(), same rule applied to
state["trades"] instead of a backtest's `positions` list.

CRITICAL PROPERTY: Anchor v1.0's real config leaves CLUSTER_CAP_ENABLED
False, so this must return False unconditionally for Anchor's process
regardless of what state or timing is passed in -- that is the whole
guarantee "we did not touch Anchor" rests on. Several tests here exist
specifically to nail that down, not just to exercise the happy path.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config
import trade_tracker as tt

NOW = datetime(2026, 8, 17, 10, 20, 0)


def _trade(strike, option_type, opened_at):
    return {"strike": strike, "option_type": option_type, "opened_at": opened_at.isoformat()}


# --------------------------------------------------------------------------
# Anchor's real default: must be a permanent no-op
# --------------------------------------------------------------------------

def test_disabled_by_default_never_blocks(monkeypatch):
    """Anchor's actual config.py value -- must not need monkeypatching
    to prove this; it's what ships."""
    assert config.CLUSTER_CAP_ENABLED is False
    state = {"trades": [_trade(24050.0, "PE", NOW)]}
    assert tt.cluster_cap_blocks(state, 24050.0, "PE", NOW) is False


def test_stays_off_even_with_an_exact_same_strike_pileup(monkeypatch):
    """Not just 'usually off' -- provably off even in the worst-case
    input (identical strike, identical instant) that WOULD trip every
    other check in the function if enabled."""
    assert config.CLUSTER_CAP_ENABLED is False
    state = {"trades": [_trade(24050.0, "PE", NOW), _trade(24050.0, "PE", NOW)]}
    assert tt.cluster_cap_blocks(state, 24050.0, "PE", NOW) is False


# --------------------------------------------------------------------------
# Behaviour when a process (Sentinel) explicitly enables it
# --------------------------------------------------------------------------

def test_enabled_blocks_a_nearby_recent_same_direction_position(monkeypatch):
    monkeypatch.setattr(config, "CLUSTER_CAP_ENABLED", True)
    monkeypatch.setattr(config, "CLUSTER_CAP_ADJACENCY_POINTS", 200)
    monkeypatch.setattr(config, "CLUSTER_CAP_WINDOW_MINUTES", 30)
    state = {"trades": [_trade(23900.0, "PE", NOW - timedelta(minutes=5))]}   # 150pt away, 5min ago
    assert tt.cluster_cap_blocks(state, 24050.0, "PE", NOW) is True


def test_enabled_allows_a_far_strike(monkeypatch):
    monkeypatch.setattr(config, "CLUSTER_CAP_ENABLED", True)
    monkeypatch.setattr(config, "CLUSTER_CAP_ADJACENCY_POINTS", 200)
    monkeypatch.setattr(config, "CLUSTER_CAP_WINDOW_MINUTES", 30)
    state = {"trades": [_trade(23000.0, "PE", NOW - timedelta(minutes=5))]}   # 1050pt away
    assert tt.cluster_cap_blocks(state, 24050.0, "PE", NOW) is False


def test_enabled_allows_the_opposite_direction(monkeypatch):
    monkeypatch.setattr(config, "CLUSTER_CAP_ENABLED", True)
    monkeypatch.setattr(config, "CLUSTER_CAP_ADJACENCY_POINTS", 200)
    monkeypatch.setattr(config, "CLUSTER_CAP_WINDOW_MINUTES", 30)
    state = {"trades": [_trade(24060.0, "CE", NOW - timedelta(minutes=5))]}   # 10pt away, opposite direction
    assert tt.cluster_cap_blocks(state, 24050.0, "PE", NOW) is False


def test_enabled_allows_a_stale_position_outside_the_window(monkeypatch):
    """The exact fix over the untimed version: a nearby same-direction
    position from hours ago must stop blocking."""
    monkeypatch.setattr(config, "CLUSTER_CAP_ENABLED", True)
    monkeypatch.setattr(config, "CLUSTER_CAP_ADJACENCY_POINTS", 200)
    monkeypatch.setattr(config, "CLUSTER_CAP_WINDOW_MINUTES", 30)
    state = {"trades": [_trade(23900.0, "PE", NOW - timedelta(hours=5))]}   # 150pt away, 5 hours ago
    assert tt.cluster_cap_blocks(state, 24050.0, "PE", NOW) is False


def test_enabled_but_missing_thresholds_is_a_safe_noop(monkeypatch):
    """Enabling the flag without both threshold values set must not
    crash or silently block everything -- fail safe to 'no cap'."""
    monkeypatch.setattr(config, "CLUSTER_CAP_ENABLED", True)
    monkeypatch.delattr(config, "CLUSTER_CAP_ADJACENCY_POINTS", raising=False)
    state = {"trades": [_trade(24050.0, "PE", NOW)]}
    assert tt.cluster_cap_blocks(state, 24050.0, "PE", NOW) is False


def test_no_open_trades_never_blocks(monkeypatch):
    monkeypatch.setattr(config, "CLUSTER_CAP_ENABLED", True)
    monkeypatch.setattr(config, "CLUSTER_CAP_ADJACENCY_POINTS", 200)
    monkeypatch.setattr(config, "CLUSTER_CAP_WINDOW_MINUTES", 30)
    assert tt.cluster_cap_blocks({"trades": []}, 24050.0, "PE", NOW) is False


# --------------------------------------------------------------------------
# Wired into try_open_new_trade -- Anchor's real entry path must be
# provably unaffected end to end, not just at the gate function itself.
# --------------------------------------------------------------------------

def test_try_open_new_trade_unaffected_for_anchor(monkeypatch, journal_paths):
    """Full entry-path check: even with a nearby same-direction trade
    already open, Anchor's real (disabled) config must still open the
    new trade -- the gate must not be reachable in Anchor's process."""
    import scanner
    from models import RiskVerdict, TradePlan
    from datetime import date as _date

    assert config.CLUSTER_CAP_ENABLED is False

    setup = scanner.Setup("NIFTY", 24050.0, "PE", "2026-08-21", ["long_buildup"], 6.0)
    plan = TradePlan(setup=setup, entry=100.0, target=160.0, stop=70.0,
                     invalidation="stop hit", lots=1, capital_at_risk=1950.0,
                     risk_pct_of_capital=0.39, risk_level="Low", stop_basis="ATR")
    verdict = RiskVerdict(decision="APPROVED", reasons=[], checks={})

    from models import MarketSnapshot
    snapshot = MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [], timestamp=NOW)

    state = {
        "trades": [_trade(23900.0, "PE", NOW - timedelta(minutes=1))],  # 150pt away, 1 min ago
        "opened_today": 0, "stop_cooldowns": {}, "direction_cooldowns": {},
        "date": _date.today().isoformat(),
    }
    trade = tt.try_open_new_trade([(setup, plan, verdict)], state, snapshot)
    assert trade is not None
    assert trade["strike"] == 24050.0


@pytest.fixture
def journal_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "JOURNAL_PATH", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(tt, "OPEN_TRADES_PATH", tmp_path / "open_trades.json")
