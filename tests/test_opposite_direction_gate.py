"""
Tests for trade_tracker.opposite_direction_blocks() -- the LIVE
counterpart of shadow.opposite_direction_blocked(), same rule applied
to state["trades"] instead of a backtest's `positions` list.

CRITICAL PROPERTY: ships ON by default in config.py (Anchor's real,
live value -- STRATEGY_VERSION bumped 1.0 -> 1.1 for it), but Sentinel
explicitly OPTS OUT (main_live_sentinel.py / main_live_banknifty_sentinel.py
override it back to False). Opposite split from CLUSTER_CAP_ENABLED,
which defaults OFF and Sentinel opts INTO -- here Anchor is the one
that gets it. Both directions are tested: the raw config default, AND
that importing Sentinel's process files actually flips it back off.

Origin: 2026-08-27, a real Bank Nifty session (5 CE positions opened,
then 12 PE positions opened while the CE side was still open, as spot
round-tripped ~1,700pts) -- see BACKLOG.md. Real backtest
(research/opposite_direction_gate_backtest.py, the actual gate wired
into shadow.py, not an approximation): net positive for Anchor (total
return +331.4% -> +362.1%, max drawdown 44.8% -> 24.1%) but a net LOSS
in total return for Sentinel (+335.8% -> +300.4%) despite improving
every per-trade/risk metric -- hence the Anchor-only split.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config
import trade_tracker as tt

NOW = datetime(2026, 8, 27, 14, 0, 0)


def _trade(strike, option_type, opened_at):
    return {"strike": strike, "option_type": option_type, "opened_at": opened_at.isoformat()}


# --------------------------------------------------------------------------
# Real default: on, for both Anchor and Sentinel
# --------------------------------------------------------------------------

def test_enabled_by_default():
    """The actual shipped config.py value -- no monkeypatching needed to
    prove this; it's what Anchor's real process runs with."""
    assert config.OPPOSITE_DIRECTION_GATE_ENABLED is True


def test_sentinel_process_files_opt_out():
    """Importing Sentinel's own process files must flip this back to
    False -- the real guarantee that Sentinel does NOT get this gate,
    checked the same way Anchor's isolation from CLUSTER_CAP_ENABLED
    was originally verified when Sentinel was first built.

    Restores EVERY config attribute main_live_sentinel.py patches, not
    just the one under test -- the import itself only ever runs once
    (module caching), and every mutation it makes is real and
    process-global. Missing even one (e.g. CLUSTER_CAP_ENABLED) would
    silently leak into every OTHER test in this suite that runs
    afterward, in this file or any other -- caught exactly this way
    while writing this test, see git history if curious.
    """
    patched_attrs = [
        "STRATEGY_NAME", "STRATEGY_VERSION", "CLUSTER_CAP_ENABLED",
        "CLUSTER_CAP_ADJACENCY_POINTS", "CLUSTER_CAP_WINDOW_MINUTES",
        "RECORD_SNAPSHOTS", "OPPOSITE_DIRECTION_GATE_ENABLED",
    ]
    originals = {name: getattr(config, name) for name in patched_attrs}
    try:
        import main_live_sentinel  # noqa: F401 -- import for its config-patching side effect
        assert config.OPPOSITE_DIRECTION_GATE_ENABLED is False
    finally:
        for name, value in originals.items():
            setattr(config, name, value)


def test_blocks_a_new_pe_while_a_ce_is_open():
    state = {"trades": [_trade(58000.0, "CE", NOW - timedelta(minutes=5))]}
    assert tt.opposite_direction_blocks(state, "PE") is True


def test_blocks_a_new_ce_while_a_pe_is_open():
    state = {"trades": [_trade(24050.0, "PE", NOW - timedelta(minutes=5))]}
    assert tt.opposite_direction_blocks(state, "CE") is True


def test_allows_the_same_direction():
    """This gate is opposite-direction only -- cluster_cap_blocks (or
    nothing, for Anchor) still governs same-direction stacking."""
    state = {"trades": [_trade(24050.0, "PE", NOW - timedelta(minutes=5))]}
    assert tt.opposite_direction_blocks(state, "PE") is False


def test_no_open_trades_never_blocks():
    assert tt.opposite_direction_blocks({"trades": []}, "PE") is False


def test_no_adjacency_or_window_narrowing():
    """Deliberately simpler than the cluster cap: a FAR strike and an
    OLD position both still block -- this gate has no band/window
    parameters, unlike cluster_cap_blocks. The measured population was
    same-day opposite-direction overlap full stop."""
    far_and_old = _trade(19000.0, "CE", NOW - timedelta(hours=5))
    state = {"trades": [far_and_old]}
    assert tt.opposite_direction_blocks(state, "PE") is True


# --------------------------------------------------------------------------
# Explicitly disabled -- must be a clean, safe no-op
# --------------------------------------------------------------------------

def test_disabled_via_config_never_blocks(monkeypatch):
    monkeypatch.setattr(config, "OPPOSITE_DIRECTION_GATE_ENABLED", False)
    state = {"trades": [_trade(58000.0, "CE", NOW)]}
    assert tt.opposite_direction_blocks(state, "PE") is False


# --------------------------------------------------------------------------
# Wired into try_open_new_trade -- the actual entry path, not just the
# gate function in isolation.
# --------------------------------------------------------------------------

def test_try_open_new_trade_blocks_opposite_direction(journal_paths):
    import scanner
    from datetime import date as _date
    from models import MarketSnapshot, RiskVerdict, TradePlan

    assert config.OPPOSITE_DIRECTION_GATE_ENABLED is True

    setup = scanner.Setup("NIFTY", 24050.0, "PE", "2026-08-27", ["long_buildup"], 6.0)
    plan = TradePlan(setup=setup, entry=100.0, target=160.0, stop=70.0,
                     invalidation="stop hit", lots=1, capital_at_risk=1950.0,
                     risk_pct_of_capital=0.39, risk_level="Low", stop_basis="ATR")
    verdict = RiskVerdict(decision="APPROVED", reasons=[], checks={})
    snapshot = MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [], timestamp=NOW)

    state = {
        "trades": [_trade(24000.0, "CE", NOW - timedelta(minutes=1))],
        "opened_today": 0, "stop_cooldowns": {}, "direction_cooldowns": {},
        "date": _date.today().isoformat(),
    }
    trade = tt.try_open_new_trade([(setup, plan, verdict)], state, snapshot)
    assert trade is None


def test_try_open_new_trade_allows_same_direction(journal_paths):
    import scanner
    from datetime import date as _date
    from models import MarketSnapshot, RiskVerdict, TradePlan

    setup = scanner.Setup("NIFTY", 24050.0, "CE", "2026-08-27", ["long_buildup"], 6.0)
    plan = TradePlan(setup=setup, entry=100.0, target=160.0, stop=70.0,
                     invalidation="stop hit", lots=1, capital_at_risk=1950.0,
                     risk_pct_of_capital=0.39, risk_level="Low", stop_basis="ATR")
    verdict = RiskVerdict(decision="APPROVED", reasons=[], checks={})
    snapshot = MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [], timestamp=NOW)

    state = {
        "trades": [_trade(24000.0, "CE", NOW - timedelta(minutes=1))],
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


# --------------------------------------------------------------------------
# Regression: the real 2026-08-27 Bank Nifty sequence this gate is for.
# Reduced to NIFTY-shaped inputs since the gate itself is index-agnostic
# (works purely off state["trades"]).
# --------------------------------------------------------------------------

def test_real_20260827_sequence_would_have_been_blocked():
    """The actual live sequence: 5 CE opened 13:12-13:14, then the FIRST
    PE candidate at 14:00 -- that PE (and everything after it) should
    have been blocked, since a CE position was still open the whole time
    (none of the CE trades closed until the 15:14 EOD flatten)."""
    ce_opened_at = datetime.fromisoformat("2026-08-27T13:12:36.206847")
    first_pe_attempt_at = datetime.fromisoformat("2026-08-27T14:00:26.876835")

    state = {"trades": [_trade(58000.0, "CE", ce_opened_at)]}
    assert tt.opposite_direction_blocks(state, "PE") is True
    # sanity: the CE position itself was real and still "open" at that
    # later time in the sense that nothing in state ever closed it here
    # (state["trades"] only ever holds OPEN positions in the live code
    # path -- a closed trade is removed and journalled separately).
    assert (first_pe_attempt_at - ce_opened_at).total_seconds() > 0
