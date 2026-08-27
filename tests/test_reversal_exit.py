"""
Tests for trade_tracker._reversal_exit_opposite_positions() and its
wiring into try_open_new_trade() via config.REVERSAL_EXIT_ENABLED.

Origin: research/reversal_exit_study.py (retrospective, paired: 1,907
events, mean R held to close -0.71 vs closed on the signal -0.32,
t=19.1) and the real mechanism re-run forward through shadow.py before
this shipped (trades 9,115 -> 10,856, total return +362.1% -> +546.6%,
max drawdown 24.1% -> 26.2% -- worse, the one thing the retrospective
measurement couldn't see). See config.REVERSAL_EXIT_ENABLED's own
comment and BACKLOG.md for the full numbers.

CRITICAL PROPERTY: ships ON by default (Anchor v1.2), Sentinel opts out
-- same split as the gate itself, tested the same two ways here.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import pytest

import config
import trade_tracker as tt
from models import MarketSnapshot, OptionQuote

NOW = datetime(2026, 8, 27, 14, 0, 0)


def _trade(strike=24000.0, option_type="CE", entry=100.0, stop=70.0, opened_at=None, **overrides):
    base = {
        "strike": strike, "option_type": option_type,
        "entry": entry, "stop": stop,
        "target": entry + (entry - stop) * config.DEFAULT_TARGET_RR,
        "lots": 1,
        "max_ltp_seen": entry, "min_ltp_seen": entry, "max_r_seen": 0.0,
        "rr_milestones_hit": {}, "reason_tags": ["long_buildup"],
        "opened_at": (opened_at or NOW).isoformat(),
    }
    base.update(overrides)
    return base


def _quote(strike, option_type, ltp):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-27", strike=strike, option_type=option_type,
        ltp=ltp, oi=50000, oi_change_pct=20.0, volume=5000, iv=12.0, iv_percentile=12.0,
    )


def _snapshot(quotes, ts=NOW):
    return MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, quotes, timestamp=ts)


@pytest.fixture
def journal_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "JOURNAL_PATH", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(tt, "OPEN_TRADES_PATH", tmp_path / "open_trades.json")


# --------------------------------------------------------------------------
# Real default: on for Anchor
# --------------------------------------------------------------------------

def test_enabled_by_default():
    """The actual shipped config.py value -- Anchor's real process."""
    assert config.REVERSAL_EXIT_ENABLED is True


# --------------------------------------------------------------------------
# _reversal_exit_opposite_positions() in isolation
# --------------------------------------------------------------------------

def test_closes_opposite_direction_positions(journal_paths):
    ce = _trade(strike=24000.0, option_type="CE", entry=100.0, stop=70.0)
    state = {"trades": [ce]}
    snap = _snapshot([_quote(24000.0, "CE", ltp=85.0)])

    closed = tt._reversal_exit_opposite_positions(state, "PE", snap)

    assert len(closed) == 1
    assert closed[0]["outcome"] == "REVERSAL_EXIT"
    assert closed[0]["exit_ltp"] == 85.0
    assert closed[0]["pnl_inr"] == tt._pnl_inr(100.0, 85.0, 1)
    assert state["trades"] == []  # removed from open trades


def test_leaves_same_direction_positions_open(journal_paths):
    """The gate this hooks into is opposite-direction only -- a PE
    signal must never touch an open PE."""
    pe = _trade(strike=24000.0, option_type="PE", entry=100.0, stop=70.0)
    state = {"trades": [pe]}
    snap = _snapshot([_quote(24000.0, "PE", ltp=85.0)])

    closed = tt._reversal_exit_opposite_positions(state, "PE", snap)

    assert closed == []
    assert state["trades"] == [pe]
    assert pe.get("outcome") is None


def test_leaves_position_open_if_no_quote_this_cycle(journal_paths):
    """Can't price an exit without a quote -- must not guess, leave it
    for next cycle, same discipline update_open_trades() already uses."""
    ce = _trade(strike=24000.0, option_type="CE")
    state = {"trades": [ce]}
    snap = _snapshot([])  # empty chain, no quote for 24000 CE this cycle

    closed = tt._reversal_exit_opposite_positions(state, "PE", snap)

    assert closed == []
    assert state["trades"] == [ce]


def test_journals_the_closed_trade(journal_paths):
    ce = _trade(strike=24000.0, option_type="CE", entry=100.0, stop=70.0)
    state = {"trades": [ce]}
    snap = _snapshot([_quote(24000.0, "CE", ltp=85.0)])

    tt._reversal_exit_opposite_positions(state, "PE", snap)

    journal = tt.JOURNAL_PATH.read_text(encoding="utf-8").strip()
    assert journal
    import json
    row = json.loads(journal)
    assert row["outcome"] == "REVERSAL_EXIT"


def test_does_not_arm_direction_chase_cooldown(journal_paths):
    """A REVERSAL_EXIT is the opposite situation to a chase -- exiting
    BECAUSE the read flipped, not re-chasing the same losing read. Must
    not arm is_direction_chase()'s cooldown the way a real LOSS does."""
    ce = _trade(strike=24000.0, option_type="CE", entry=100.0, stop=70.0)
    state = {"trades": [ce], "direction_cooldowns": {}}
    snap = _snapshot([_quote(24000.0, "CE", ltp=60.0)])  # closes as a real loss-sized move

    tt._reversal_exit_opposite_positions(state, "PE", snap)

    assert state["direction_cooldowns"].get("CE", []) == []


# --------------------------------------------------------------------------
# Wired into try_open_new_trade -- the real entry path
# --------------------------------------------------------------------------

def _setup_plan_verdict(strike=24050.0, option_type="PE"):
    import scanner
    from models import RiskVerdict, TradePlan
    setup = scanner.Setup("NIFTY", strike, option_type, "2026-09-01", ["long_buildup"], 6.0)
    plan = TradePlan(setup=setup, entry=100.0, target=160.0, stop=70.0,
                     invalidation="stop hit", lots=1, capital_at_risk=1950.0,
                     risk_pct_of_capital=0.39, risk_level="Low", stop_basis="ATR")
    verdict = RiskVerdict(decision="APPROVED", reasons=[], checks={})
    return setup, plan, verdict


def test_try_open_new_trade_closes_blocker_when_enabled(journal_paths, monkeypatch):
    from datetime import date as _date
    monkeypatch.setattr(config, "REVERSAL_EXIT_ENABLED", True)

    ce = _trade(strike=24000.0, option_type="CE", entry=100.0, stop=70.0,
               opened_at=NOW.replace(minute=0))
    state = {
        "trades": [ce], "opened_today": 0, "stop_cooldowns": {}, "direction_cooldowns": {},
        "date": _date.today().isoformat(),
    }
    snap = _snapshot([_quote(24000.0, "CE", ltp=85.0)], ts=NOW)
    setup, plan, verdict = _setup_plan_verdict(strike=24050.0, option_type="PE")

    result = tt.try_open_new_trade([(setup, plan, verdict)], state, snap)

    assert result is None  # the new PE candidate still does NOT open
    assert state["trades"] == []  # the blocking CE was closed
    import json
    journal_row = json.loads(tt.JOURNAL_PATH.read_text(encoding="utf-8").strip())
    assert journal_row["outcome"] == "REVERSAL_EXIT"


def test_try_open_new_trade_leaves_blocker_open_when_disabled(journal_paths, monkeypatch):
    """Default-off behaviour (and Sentinel's real config): the candidate
    is still blocked, but the existing position must be left exactly as
    it was -- this is the ORIGINAL v1.1 gate behaviour, must not regress."""
    from datetime import date as _date
    monkeypatch.setattr(config, "REVERSAL_EXIT_ENABLED", False)

    ce = _trade(strike=24000.0, option_type="CE", entry=100.0, stop=70.0,
               opened_at=NOW.replace(minute=0))
    state = {
        "trades": [ce], "opened_today": 0, "stop_cooldowns": {}, "direction_cooldowns": {},
        "date": _date.today().isoformat(),
    }
    snap = _snapshot([_quote(24000.0, "CE", ltp=85.0)], ts=NOW)
    setup, plan, verdict = _setup_plan_verdict(strike=24050.0, option_type="PE")

    result = tt.try_open_new_trade([(setup, plan, verdict)], state, snap)

    assert result is None
    assert state["trades"] == [ce]  # untouched
    assert ce.get("outcome") is None


# --------------------------------------------------------------------------
# Sentinel opts out -- same isolation check as the gate itself
# --------------------------------------------------------------------------

def test_sentinel_process_files_opt_out():
    """
    Real-import verification of Sentinel's opt-out already lives in
    tests/test_opposite_direction_gate.py's own
    test_sentinel_process_files_opt_out -- deliberately NOT duplicated
    here as a second real import. main_live_sentinel is a heavy module
    with its own import-order assertions and real side effects
    (including direct, non-monkeypatch reassignment of
    trade_tracker.JOURNAL_PATH/OPEN_TRADES_PATH); a second real import
    or reload() of it from here proved fragile in practice -- see that
    other test's own docstring for what went wrong when this was tried.

    Reads the FILE'S SOURCE TEXT directly from disk instead -- no
    import, no module execution, zero side effects, immune to caching/
    ordering entirely. Can't verify config.py's own attribute name is
    spelled correctly the way a real import would (that part is already
    covered by test_enabled_by_default reading the live config.py value
    directly), but a source-text check is a real, honest guarantee for
    what THIS test cares about: does the shipped file contain the
    opt-out line at all.
    """
    from pathlib import Path
    source_path = Path(__file__).parent.parent / "main_live_sentinel.py"
    source = source_path.read_text(encoding="utf-8")
    assert "config.REVERSAL_EXIT_ENABLED = False" in source
