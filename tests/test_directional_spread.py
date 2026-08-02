"""
Tests for the directional credit-spread strategy (directional_spread_*).

End-to-end coverage of: direction selection from market bias, strike/
hedge selection, plan pricing (net credit / max loss / breakeven) for
BOTH sides, risk gating (concurrency, daily cap, min credit, max
capital), and position lifecycle (open -> mark-to-market -> profit
target / stop loss / breach warning / expiry settlement).

Run: python -m pytest tests/ -q
"""

import json

import pytest

import config_directional_spread as dcfg
import directional_spread_scanner as dss
import directional_spread_plan_generator as dpg
import directional_spread_risk_checker as drc
import directional_spread_tracker as dst
from models import MarketSnapshot, OptionQuote


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    state_path = tmp_path / "directional_spread_position.json"
    journal_path = tmp_path / "directional_spread_journal.jsonl"
    monkeypatch.setattr(dst, "STATE_PATH", state_path)
    monkeypatch.setattr(dst, "JOURNAL_PATH", journal_path)
    return state_path, journal_path


def _quote(strike, opt, ltp, oi=50000, volume=5000):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-04", strike=strike, option_type=opt,
        ltp=ltp, oi=oi, oi_change_pct=10.0, volume=volume, iv=12.0, iv_percentile=50.0,
    )


def _bull_put_chain():
    """Spot 24000. PE premiums rise as strikes rise toward spot (normal skew shape)."""
    chain = []
    for strike, pe_ltp in [(23600, 15), (23650, 20), (23700, 28), (23750, 35),
                           (23800, 45), (23850, 58), (23900, 72)]:
        chain.append(_quote(strike, "PE", pe_ltp))
    return chain


def _bear_call_chain():
    """Spot 24000. CE premiums rise as strikes fall toward spot."""
    chain = []
    for strike, ce_ltp in [(24100, 72), (24150, 58), (24200, 45), (24250, 35),
                           (24300, 28), (24350, 20), (24400, 15)]:
        chain.append(_quote(strike, "CE", ce_ltp))
    return chain


# --------------------------------------------------------------------------
# Direction selection
# --------------------------------------------------------------------------

def test_select_direction_bullish_sells_puts():
    assert dss.select_direction("bullish", 2.5) == "PE"


def test_select_direction_bearish_sells_calls():
    assert dss.select_direction("bearish", -2.5) == "CE"


def test_select_direction_none_when_bias_too_weak():
    assert dss.select_direction("bullish", 1.0) is None  # below dcfg.BIAS_STRONG_THRESHOLD (2.0)


def test_select_direction_none_when_neutral():
    assert dss.select_direction("neutral/range", 0.0) is None


def test_select_direction_none_when_bias_missing():
    assert dss.select_direction(None, None) is None


# --------------------------------------------------------------------------
# Strike / hedge selection
# --------------------------------------------------------------------------

def test_find_short_strike_prefers_highest_oi_within_band(monkeypatch):
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MIN", 30.0)
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MAX", 60.0)
    chain = [_quote(23800, "PE", 45, oi=10000), _quote(23850, "PE", 58, oi=90000)]
    result = dss.find_short_strike(chain, "PE")
    assert result.strike == 23850  # higher OI wins even though not closer to a band edge


def test_find_short_strike_none_when_nothing_in_band(monkeypatch):
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MIN", 30.0)
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MAX", 60.0)
    chain = [_quote(23600, "PE", 15)]
    assert dss.find_short_strike(chain, "PE") is None


def test_find_hedge_strike_for_put_spread_goes_lower(monkeypatch):
    monkeypatch.setattr(dcfg, "HEDGE_DISTANCE_POINTS", 150)
    chain = _bull_put_chain()
    hedge = dss.find_hedge_strike(chain, "PE", short_strike=23850)
    assert hedge.strike <= 23850 - 150


def test_find_hedge_strike_for_call_spread_goes_higher(monkeypatch):
    monkeypatch.setattr(dcfg, "HEDGE_DISTANCE_POINTS", 150)
    chain = _bear_call_chain()
    hedge = dss.find_hedge_strike(chain, "CE", short_strike=24150)
    assert hedge.strike >= 24150 + 150


def test_find_hedge_strike_none_when_chain_doesnt_extend_far_enough(monkeypatch):
    monkeypatch.setattr(dcfg, "HEDGE_DISTANCE_POINTS", 500)
    chain = _bull_put_chain()
    assert dss.find_hedge_strike(chain, "PE", short_strike=23850) is None


def test_find_directional_spread_legs_end_to_end(monkeypatch):
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MIN", 30.0)
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MAX", 60.0)
    monkeypatch.setattr(dcfg, "HEDGE_DISTANCE_POINTS", 150)
    legs = dss.find_directional_spread_legs(_bull_put_chain(), "bullish", 2.5)
    assert legs["direction"] == "PE"
    assert legs["short"] is not None
    assert legs["hedge"] is not None
    assert legs["hedge"].strike < legs["short"].strike


def test_find_directional_spread_legs_none_when_bias_weak(monkeypatch):
    legs = dss.find_directional_spread_legs(_bull_put_chain(), "neutral/range", 0.0)
    assert legs == {"direction": None, "short": None, "hedge": None}


# --------------------------------------------------------------------------
# Plan pricing -- both sides, exact arithmetic
# --------------------------------------------------------------------------

def test_bull_put_spread_pricing(monkeypatch):
    monkeypatch.setattr(dcfg, "NIFTY_LOT_SIZE", 65)
    monkeypatch.setattr(dcfg, "LOTS_PER_LEG", 1)
    legs = {"direction": "PE", "short": _quote(23850, "PE", 58), "hedge": _quote(23700, "PE", 28)}
    plan = dpg.build_directional_spread_plan(legs, "2026-08-04", "bullish", 2.5)

    assert plan.net_credit == pytest.approx(30.0)          # 58 - 28
    assert plan.net_credit_inr == pytest.approx(30.0 * 65)
    wing_width = 23850 - 23700                              # 150
    assert plan.max_loss_inr == pytest.approx((wing_width - 30.0) * 65)
    assert plan.breakeven == pytest.approx(23850 - 30.0)    # short_strike - net_credit


def test_bear_call_spread_pricing(monkeypatch):
    monkeypatch.setattr(dcfg, "NIFTY_LOT_SIZE", 65)
    monkeypatch.setattr(dcfg, "LOTS_PER_LEG", 1)
    legs = {"direction": "CE", "short": _quote(24150, "CE", 58), "hedge": _quote(24300, "CE", 28)}
    plan = dpg.build_directional_spread_plan(legs, "2026-08-04", "bearish", -2.5)

    assert plan.net_credit == pytest.approx(30.0)
    wing_width = 24300 - 24150
    assert plan.max_loss_inr == pytest.approx((wing_width - 30.0) * 65)
    assert plan.breakeven == pytest.approx(24150 + 30.0)    # short_strike + net_credit for a call spread


def test_plan_is_none_when_direction_missing():
    assert dpg.build_directional_spread_plan(
        {"direction": None, "short": None, "hedge": None}, "2026-08-04", "neutral/range", 0.0
    ) is None


def test_plan_is_none_when_hedge_leg_missing():
    legs = {"direction": "PE", "short": _quote(23850, "PE", 58), "hedge": None}
    assert dpg.build_directional_spread_plan(legs, "2026-08-04", "bullish", 2.5) is None


# --------------------------------------------------------------------------
# Risk checks
# --------------------------------------------------------------------------

def _approved_plan(monkeypatch, **overrides):
    monkeypatch.setattr(dcfg, "NIFTY_LOT_SIZE", 65)
    monkeypatch.setattr(dcfg, "LOTS_PER_LEG", 1)
    legs = {"direction": "PE", "short": _quote(23850, "PE", 58), "hedge": _quote(23700, "PE", 28)}
    plan = dpg.build_directional_spread_plan(legs, "2026-08-04", "bullish", 2.5)
    for k, v in overrides.items():
        setattr(plan, k, v)
    return plan


def test_risk_check_rejects_when_no_plan():
    verdict = drc.check(None)
    assert verdict.decision == "REJECTED"


def test_risk_check_rejects_at_max_concurrency(monkeypatch):
    plan = _approved_plan(monkeypatch)
    monkeypatch.setattr(dcfg, "MAX_CONCURRENT_POSITIONS", 1)
    verdict = drc.check(plan, currently_open_positions=1, opened_today=0)
    assert verdict.decision == "REJECTED"
    assert any("concurrent" in r.lower() for r in verdict.reasons)


def test_risk_check_rejects_at_daily_cap(monkeypatch):
    plan = _approved_plan(monkeypatch)
    monkeypatch.setattr(dcfg, "MAX_NEW_POSITIONS_PER_DAY", 1)
    verdict = drc.check(plan, currently_open_positions=0, opened_today=1)
    assert verdict.decision == "REJECTED"
    assert any("max new positions" in r.lower() for r in verdict.reasons)


def test_risk_check_rejects_thin_credit(monkeypatch):
    monkeypatch.setattr(dcfg, "MIN_NET_CREDIT", 100.0)   # way above the 30-point credit
    plan = _approved_plan(monkeypatch)
    verdict = drc.check(plan, currently_open_positions=0, opened_today=0)
    assert verdict.decision == "REJECTED"


def test_risk_check_rejects_excess_capital_at_risk(monkeypatch):
    monkeypatch.setattr(dcfg, "MAX_CAPITAL_AT_RISK", 100.0)   # far below actual max loss
    plan = _approved_plan(monkeypatch)
    verdict = drc.check(plan, currently_open_positions=0, opened_today=0)
    assert verdict.decision == "REJECTED"


def test_risk_check_approves_a_clean_plan(monkeypatch):
    monkeypatch.setattr(dcfg, "MAX_CONCURRENT_POSITIONS", 1)
    monkeypatch.setattr(dcfg, "MAX_NEW_POSITIONS_PER_DAY", 1)
    monkeypatch.setattr(dcfg, "MIN_NET_CREDIT", 8.0)
    monkeypatch.setattr(dcfg, "MAX_CAPITAL_AT_RISK", 30000.0)
    plan = _approved_plan(monkeypatch)
    verdict = drc.check(plan, currently_open_positions=0, opened_today=0)
    assert verdict.decision == "APPROVED"


# --------------------------------------------------------------------------
# Position lifecycle
# --------------------------------------------------------------------------

def _snapshot(chain, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=chain, source="test")


def test_open_position_increments_daily_counter(state_paths, monkeypatch):
    plan = _approved_plan(monkeypatch)
    state = {"position": None, "opened_today": 0, "opened_today_date": None}
    dst.open_position(state, plan)
    assert state["opened_today"] == 1
    assert state["position"]["status"] == "OPEN"


def test_load_state_resets_daily_counter_on_a_new_day(state_paths, monkeypatch):
    import datetime
    state_paths[0].write_text(json.dumps(
        {"position": None, "opened_today": 1, "opened_today_date": "2020-01-01"}
    ))
    loaded = dst.load_state()
    assert loaded["opened_today"] == 0
    assert loaded["opened_today_date"] == datetime.date.today().isoformat()


def test_mark_to_market_pnl_tracks_credit_captured(state_paths, monkeypatch):
    plan = _approved_plan(monkeypatch)   # short PE 23850 @ 58, hedge PE 23700 @ 28, credit 30
    state = {"position": None, "opened_today": 0}
    dst.open_position(state, plan)

    # Both legs decayed favourably: short now worth 20, hedge worth 8.
    chain = [_quote(23850, "PE", 20), _quote(23700, "PE", 8)]
    position = dst.update_position(state, _snapshot(chain, spot=24050))
    # cost to close now = 20 - 8 = 12; captured = (30 - 12) * 65
    assert position["current_mtm_pnl_inr"] == pytest.approx((30 - 12) * 65)


def test_profit_target_closes_the_position_automatically(state_paths, monkeypatch):
    monkeypatch.setattr(dcfg, "PROFIT_TARGET_PCT_OF_MAX_PROFIT", 60.0)
    plan = _approved_plan(monkeypatch)   # max_profit = 30 * 65 = 1950
    state = {"position": None, "opened_today": 0}
    dst.open_position(state, plan)

    # Captured needs to reach 60% of 1950 = 1170, i.e. net cost-to-close <= 30 - 18 = 12.
    chain = [_quote(23850, "PE", 10), _quote(23700, "PE", 2)]  # cost to close = 8, captured = 22*65=1430
    position = dst.update_position(state, _snapshot(chain, spot=24100))

    assert position["status"] == "CLOSED"
    assert position["close_reason"] == "profit_target"
    assert state["position"] is None   # cleared from state
    journal_lines = state_paths[1].read_text().strip().split("\n")
    assert len(journal_lines) == 1


def test_stop_loss_closes_the_position_automatically(state_paths, monkeypatch):
    monkeypatch.setattr(dcfg, "STOP_LOSS_PCT_OF_MAX_LOSS", 50.0)
    monkeypatch.setattr(dcfg, "PROFIT_TARGET_PCT_OF_MAX_PROFIT", 60.0)
    plan = _approved_plan(monkeypatch)   # max_loss = (150-30)*65 = 7800
    state = {"position": None, "opened_today": 0}
    dst.open_position(state, plan)

    # Short leg premium spiked (spot fell toward it) -- a real adverse move.
    # max_loss = (150-30)*65 = 7800; 50% trigger = -3900. cost to close =
    # 110-10=100, pnl = (30-100)*65 = -4550, comfortably past the trigger.
    chain = [_quote(23850, "PE", 110), _quote(23700, "PE", 10)]
    position = dst.update_position(state, _snapshot(chain, spot=23860))

    assert position["status"] == "CLOSED"
    assert position["close_reason"] == "stop_loss"


def test_breach_warning_stages_but_does_not_close(state_paths, monkeypatch):
    monkeypatch.setattr(dcfg, "BREACH_WARNING_BUFFER_POINTS", 40)
    monkeypatch.setattr(dcfg, "PROFIT_TARGET_PCT_OF_MAX_PROFIT", 60.0)
    monkeypatch.setattr(dcfg, "STOP_LOSS_PCT_OF_MAX_LOSS", 90.0)   # avoid tripping stop first
    plan = _approved_plan(monkeypatch)   # short PE 23850
    state = {"position": None, "opened_today": 0}
    dst.open_position(state, plan)

    chain = [_quote(23850, "PE", 62), _quote(23700, "PE", 30)]   # small move, cost=32, pnl=(30-32)*65=-130
    position = dst.update_position(state, _snapshot(chain, spot=23880))  # within 40pts of 23850

    assert position["status"] == "OPEN"    # not auto-closed
    assert position["breach_staged"] is True


def test_breach_warning_direction_for_call_spread(state_paths, monkeypatch):
    """A bear call spread breaches when spot rises TOWARD the short strike, not away."""
    monkeypatch.setattr(dcfg, "BREACH_WARNING_BUFFER_POINTS", 40)
    monkeypatch.setattr(dcfg, "NIFTY_LOT_SIZE", 65)
    legs = {"direction": "CE", "short": _quote(24150, "CE", 58), "hedge": _quote(24300, "CE", 28)}
    plan = dpg.build_directional_spread_plan(legs, "2026-08-04", "bearish", -2.5)

    assert dst.check_breach_warning(spot=24120, plan_dict=plan.__dict__) is not None   # close beneath short strike
    assert dst.check_breach_warning(spot=23900, plan_dict=plan.__dict__) is None       # far below, safe


def test_expiry_settlement_falls_back_to_intrinsic_value(state_paths, monkeypatch):
    monkeypatch.setattr(dcfg, "PROFIT_TARGET_PCT_OF_MAX_PROFIT", 999.0)  # never trip early
    monkeypatch.setattr(dcfg, "STOP_LOSS_PCT_OF_MAX_LOSS", 999.0)
    plan = _approved_plan(monkeypatch)   # short PE 23850, hedge PE 23700
    state = {"position": None, "opened_today": 0}
    dst.open_position(state, plan)

    # No quotes available at all for either leg (both expired out of the chain).
    closed = dst.close_position(state, _snapshot([], spot=24200), reason="expiry_settlement")

    assert closed["status"] == "EXPIRED"
    # Spot 24200 is above both PE strikes -> both intrinsic values are 0 -> full credit kept.
    assert closed["pnl_inr"] == pytest.approx(30.0 * 65)
    assert set(closed["legs_settled_at_intrinsic"]) == {"short", "hedge"}


def test_live_config_matches_the_2026_08_02_sweep_winner():
    """
    Pins the config actually adopted from sweep_spread_config.py's 493-day
    grid, so an unrelated future edit can't silently drift back toward
    the old (worse, worst-in-grid) values without anyone noticing. See
    config_directional_spread.py's own comment for the full evidence:
    old (30-60/150) was the WORST cell in a 12-cell grid where every
    cell was profitable; new (40-70/100) has the best return-per-unit-
    of-drawdown (15.30 vs the old config's 6.22).
    """
    assert dcfg.SHORT_PREMIUM_MIN == 40.0
    assert dcfg.SHORT_PREMIUM_MAX == 70.0
    assert dcfg.HEDGE_DISTANCE_POINTS == 100
