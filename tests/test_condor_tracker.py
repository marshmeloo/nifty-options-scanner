"""
Tests for condor_tracker.py's position lifecycle: mark-to-market,
breach-warning staging, expiry-day intrinsic-value settlement, and (new,
2026-08-02) profit-milestone excursion tracking.

No dedicated test file existed for this module before -- test_shadow_condor.py
covers the BACKTEST module, test_condor_expiry.py covers expiry selection,
but condor_tracker.py's own open/update/close cycle had no direct coverage.

Run: python -m pytest tests/ -q
"""

import json

import pytest

import config_condor as ccfg
import condor_tracker as ct
from models import CondorPlan, MarketSnapshot, OptionQuote


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    state_path = tmp_path / "condor_position.json"
    journal_path = tmp_path / "condor_journal.jsonl"
    monkeypatch.setattr(ct, "STATE_PATH", state_path)
    monkeypatch.setattr(ct, "JOURNAL_PATH", journal_path)
    return state_path, journal_path


def _quote(strike, opt, ltp):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-04", strike=strike, option_type=opt,
        ltp=ltp, oi=50000, oi_change_pct=0.0, volume=5000, iv=12.0, iv_percentile=50.0,
    )


def _plan(**overrides):
    """
    Short CE 24300 @ 25, short PE 23700 @ 25 (credit 50); hedge CE 24600 @ 8,
    hedge PE 23400 @ 8 (cost 16). Net credit 34/leg-pair -> Rs 34*65=2210.
    Wing width 300 both sides -> max_loss = (300-34)*65 = 17,290.
    """
    values = dict(
        expiry="2026-08-04",
        short_ce_strike=24300.0, short_ce_premium=25.0,
        short_pe_strike=23700.0, short_pe_premium=25.0,
        hedge_ce_strike=24600.0, hedge_ce_premium=8.0,
        hedge_pe_strike=23400.0, hedge_pe_premium=8.0,
        lots=1, net_credit=34.0, net_credit_inr=34.0 * 65,
        max_loss_inr=(300 - 34) * 65, max_profit_inr=34.0 * 65,
        breakeven_upper=24300.0 + 34.0, breakeven_lower=23700.0 - 34.0,
    )
    values.update(overrides)
    return CondorPlan(**values)


def _snapshot(chain, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=chain, source="test")


def _open(state_paths, monkeypatch, **plan_overrides):
    plan = _plan(**plan_overrides)
    state = {"position": None}
    ct.open_position(state, plan)
    return state, plan


# --------------------------------------------------------------------------
# Mark-to-market / breach warning (existing behaviour, previously untested)
# --------------------------------------------------------------------------

def test_mark_to_market_tracks_credit_captured(state_paths, monkeypatch):
    state, plan = _open(state_paths, monkeypatch)
    # All four legs decayed favourably.
    chain = [_quote(24300, "CE", 15), _quote(23700, "PE", 15),
            _quote(24600, "CE", 4), _quote(23400, "PE", 4)]
    position = ct.update_position(state, _snapshot(chain))
    # cost to close = (15+15) - (4+4) = 22; captured = (34-22)*65
    assert position["current_mtm_pnl_inr"] == pytest.approx((34 - 22) * 65)


def test_missing_leg_quote_yields_unavailable_not_zero(state_paths, monkeypatch):
    """The exact distinction behind the 2026-07-30 'MTM unavailable' incident."""
    state, plan = _open(state_paths, monkeypatch)
    chain = [_quote(24300, "CE", 15), _quote(23700, "PE", 15), _quote(24600, "CE", 4)]  # hedge PE missing
    position = ct.update_position(state, _snapshot(chain))
    assert position["current_mtm_pnl_inr"] is None


def test_breach_warning_stages_but_never_auto_closes(state_paths, monkeypatch):
    """condor_tracker has NO auto-close on breach -- only staging for human review."""
    state, plan = _open(state_paths, monkeypatch)
    chain = [_quote(24300, "CE", 60), _quote(23700, "PE", 10),
            _quote(24600, "CE", 20), _quote(23400, "PE", 3)]
    position = ct.update_position(state, _snapshot(chain, spot=24280))  # within 50pts of short CE
    assert position["status"] == "OPEN"
    assert position["breach_staged_this_week"] is True


def test_expiry_settlement_falls_back_to_intrinsic_value(state_paths, monkeypatch):
    state, plan = _open(state_paths, monkeypatch)
    # No quotes at all -- spot settles between both short strikes -> full credit kept.
    closed = ct.close_position(state, _snapshot([], spot=24000), reason="expiry_settlement")
    assert closed["status"] == "EXPIRED"
    assert closed["pnl_inr"] == pytest.approx(34.0 * 65)
    assert set(closed["legs_settled_at_intrinsic"]) == {"short_ce", "short_pe", "hedge_ce", "hedge_pe"}


# --------------------------------------------------------------------------
# Profit-milestone tracking (2026-08-02) -- analysis only, no live gate
# --------------------------------------------------------------------------

def test_update_position_tracks_peak_mtm_and_milestones(state_paths, monkeypatch):
    monkeypatch.setattr(ccfg, "PROFIT_MILESTONES_PCT", [10, 20, 30])
    state, plan = _open(state_paths, monkeypatch)   # max_profit = 34*65 = 2210

    # cost to close = 22, captured = (34-22)*65 = 780 -> 780/2210 = ~35%
    chain = [_quote(24300, "CE", 15), _quote(23700, "PE", 15),
            _quote(24600, "CE", 4), _quote(23400, "PE", 4)]
    position = ct.update_position(state, _snapshot(chain))

    assert position["max_pnl_inr_seen"] == pytest.approx(position["current_mtm_pnl_inr"])
    assert set(position["profit_milestones_hit"]) == {"10", "20", "30"}


def test_milestones_are_sticky_after_a_pullback(state_paths, monkeypatch):
    monkeypatch.setattr(ccfg, "PROFIT_MILESTONES_PCT", [10, 20, 30])
    state, plan = _open(state_paths, monkeypatch)

    ct.update_position(state, _snapshot([   # peak: ~35% captured
        _quote(24300, "CE", 15), _quote(23700, "PE", 15),
        _quote(24600, "CE", 4), _quote(23400, "PE", 4)]))
    position = ct.update_position(state, _snapshot([   # pulls back to near flat
        _quote(24300, "CE", 30), _quote(23700, "PE", 30),
        _quote(24600, "CE", 12), _quote(23400, "PE", 12)]))

    assert set(position["profit_milestones_hit"]) == {"10", "20", "30"}
    assert position["current_mtm_pnl_inr"] < position["max_pnl_inr_seen"]


def test_missing_quote_cycle_does_not_update_excursion(state_paths, monkeypatch):
    state, plan = _open(state_paths, monkeypatch)
    ct.update_position(state, _snapshot([
        _quote(24300, "CE", 15), _quote(23700, "PE", 15),
        _quote(24600, "CE", 4), _quote(23400, "PE", 4)]))
    before = dict(state["position"])

    position = ct.update_position(state, _snapshot([]))  # nothing priced
    assert position["max_pnl_inr_seen"] == before["max_pnl_inr_seen"]
    assert position["min_pnl_inr_seen"] == before["min_pnl_inr_seen"]


def test_closed_position_gets_excursion_summary_fields(state_paths, monkeypatch):
    """
    close_position() runs one final excursion update at the settlement
    price (see its own comment on why), so settling at full credit
    correctly becomes the new peak here -- 100%, not the 35% seen at the
    last open-cycle poll.
    """
    monkeypatch.setattr(ccfg, "PROFIT_MILESTONES_PCT", [10, 20, 30])
    state, plan = _open(state_paths, monkeypatch)

    ct.update_position(state, _snapshot([   # intermediate poll, ~35% captured
        _quote(24300, "CE", 15), _quote(23700, "PE", 15),
        _quote(24600, "CE", 4), _quote(23400, "PE", 4)]))
    closed = ct.close_position(state, _snapshot([], spot=24000), reason="manual")  # settles at full credit

    assert closed["max_pct_of_max_profit"] == pytest.approx(100.0, abs=0.1)
    assert closed["would_have_won_at"] == [10, 20, 30]
    assert closed["capture_efficiency_pct"] == pytest.approx(100.0, abs=0.1)  # closed AT its own peak


def test_capture_efficiency_reflects_a_real_giveback(state_paths, monkeypatch):
    """A position that peaks then is closed lower (a genuine early manual close) must show <100%."""
    state, plan = _open(state_paths, monkeypatch)

    ct.update_position(state, _snapshot([   # peak: cost to close 22, captured (34-22)*65=780
        _quote(24300, "CE", 15), _quote(23700, "PE", 15),
        _quote(24600, "CE", 4), _quote(23400, "PE", 4)]))
    closed = ct.close_position(state, _snapshot([   # closed much lower than the peak
        _quote(24300, "CE", 40), _quote(23700, "PE", 40),
        _quote(24600, "CE", 15), _quote(23400, "PE", 15)], spot=24000), reason="manual")

    assert closed["capture_efficiency_pct"] < 100.0


def test_profit_milestone_stats_when_target_disabled(state_paths, monkeypatch):
    monkeypatch.setattr(ccfg, "PROFIT_MILESTONES_PCT", [50])
    monkeypatch.setattr(ccfg, "PROFIT_TARGET_PCT", None)
    entries = [
        {"max_pct_of_max_profit": 80.0, "pnl_pct_of_max_profit": 80.0},
        {"max_pct_of_max_profit": 20.0, "pnl_pct_of_max_profit": -40.0},
    ]
    state_paths[1].write_text("\n".join(json.dumps(e) for e in entries))

    stats = ct.profit_milestone_stats()
    assert stats["sample"] == 2
    assert stats["milestones"][0]["reached"] == 1
    assert stats["milestones"][0]["hit_rate_pct"] == 50.0
    assert "current_target_pct" not in stats

    text = ct.summarize_profit_milestones()
    assert "no active target" in text.lower()


def test_profit_milestone_stats_when_target_active(state_paths, monkeypatch):
    monkeypatch.setattr(ccfg, "PROFIT_MILESTONES_PCT", [50])
    monkeypatch.setattr(ccfg, "PROFIT_TARGET_PCT", 50.0)
    entries = [{"max_pct_of_max_profit": 80.0, "pnl_pct_of_max_profit": 80.0}]
    state_paths[1].write_text("\n".join(json.dumps(e) for e in entries))

    stats = ct.profit_milestone_stats()
    assert stats["current_target_pct"] == 50.0

    text = ct.summarize_profit_milestones()
    assert "active target 50%" in text.lower()
    assert "no active target" not in text.lower()


# --------------------------------------------------------------------------
# Profit-target auto-close (2026-08-12) -- adopted after the full PT/IV-rank
# sweep; see config_condor.py's PROFIT_TARGET_PCT comment and BACKLOG.md.
# --------------------------------------------------------------------------

def test_profit_target_reached_closes_automatically(state_paths, monkeypatch):
    monkeypatch.setattr(ccfg, "PROFIT_TARGET_PCT", 50.0)
    state, plan = _open(state_paths, monkeypatch)  # max_profit = 2210
    # cost to close = 5, captured = (34-5)*65 = 1885 -> 85.3% of max profit
    chain = [_quote(24300, "CE", 3), _quote(23700, "PE", 2),
             _quote(24600, "CE", 0), _quote(23400, "PE", 0)]
    position = ct.update_position(state, _snapshot(chain))
    assert position["status"] == "CLOSED"
    assert position["close_reason"] == "profit_target"
    assert position["pnl_inr"] == pytest.approx((34 - 5) * 65)


def test_below_profit_target_stays_open(state_paths, monkeypatch):
    monkeypatch.setattr(ccfg, "PROFIT_TARGET_PCT", 50.0)
    state, plan = _open(state_paths, monkeypatch)
    # cost to close = 22, captured = (34-22)*65 = 780 -> ~35%, below the 50% target
    chain = [_quote(24300, "CE", 15), _quote(23700, "PE", 15),
             _quote(24600, "CE", 4), _quote(23400, "PE", 4)]
    position = ct.update_position(state, _snapshot(chain))
    assert position["status"] == "OPEN"


def test_profit_target_disabled_holds_to_expiry_as_before(state_paths, monkeypatch):
    monkeypatch.setattr(ccfg, "PROFIT_TARGET_PCT", None)
    state, plan = _open(state_paths, monkeypatch)
    chain = [_quote(24300, "CE", 3), _quote(23700, "PE", 2),
             _quote(24600, "CE", 0), _quote(23400, "PE", 0)]  # ~85% of max profit
    position = ct.update_position(state, _snapshot(chain))
    assert position["status"] == "OPEN"


def test_profit_target_close_stages_an_advisory(state_paths, monkeypatch, tmp_path):
    import trade_staging as staging
    monkeypatch.setattr(staging, "STAGED_ORDERS_PATH", tmp_path / "staged_orders.json")
    monkeypatch.setattr(ccfg, "PROFIT_TARGET_PCT", 50.0)
    state, plan = _open(state_paths, monkeypatch)
    chain = [_quote(24300, "CE", 3), _quote(23700, "PE", 2),
             _quote(24600, "CE", 0), _quote(23400, "PE", 0)]
    ct.update_position(state, _snapshot(chain))

    staged = staging.list_staged()
    kinds = [r["kind"] for r in staged]
    assert "condor_profit_target_close" in kinds


def test_profit_target_ignored_when_mtm_unavailable(state_paths, monkeypatch):
    """A missing-leg cycle must never be treated as having reached the target."""
    monkeypatch.setattr(ccfg, "PROFIT_TARGET_PCT", 50.0)
    state, plan = _open(state_paths, monkeypatch)
    chain = [_quote(24300, "CE", 3), _quote(23700, "PE", 2), _quote(24600, "CE", 0)]  # hedge PE missing
    position = ct.update_position(state, _snapshot(chain))
    assert position["status"] == "OPEN"


def test_profit_milestone_stats_empty_journal():
    assert ct.profit_milestone_stats() == {"sample": 0, "milestones": []}


def test_summarize_profit_milestones_with_no_history():
    assert "none yet" in ct.summarize_profit_milestones()
