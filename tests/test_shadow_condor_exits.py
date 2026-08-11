"""
Tests for shadow_condor.py's optional early-exit rules
(CondorPolicy.profit_target_pct / stop_loss_credit_multiple).

These do NOT exist in the live strategy (condor_tracker.py only stages
a breach warning for human review, never auto-closes) -- they exist
here purely to MEASURE whether either rule would have helped, before
either is ever wired into the live tracker.

Run: python -m pytest tests/test_shadow_condor_exits.py -q
"""
from datetime import datetime, timedelta

import pytest

import config_condor as ccfg
import shadow_condor as sc
from models import MarketSnapshot, OptionQuote


def _plan_dict():
    net_credit = 40.0
    lot_size = ccfg.NIFTY_LOT_SIZE
    lots = ccfg.LOTS_PER_LEG
    return {
        "short_ce_strike": 24500.0, "short_pe_strike": 23500.0,
        "hedge_ce_strike": 24800.0, "hedge_pe_strike": 23200.0,
        "net_credit": net_credit,
        "net_credit_inr": round(net_credit * lot_size * lots, 2),
        "max_profit_inr": round(net_credit * lot_size * lots, 2),
        "max_loss_inr": 999999.0,  # not exercised by these tests
        "lots": lots,
    }


def _quote(strike, opt, ltp):
    return OptionQuote(symbol="NIFTY", expiry="rolling:week1", strike=strike, option_type=opt,
                       ltp=ltp, oi=1000, oi_change_pct=0.0, volume=100, iv=14.0, iv_percentile=50.0)


def _snapshot_for_cost(ts, short_ce, short_pe, hedge_ce, hedge_pe, spot=24000.0):
    """cost_to_close = (short_ce+short_pe) - (hedge_ce+hedge_pe); mtm =
    (net_credit - cost_to_close) * lot_size. Built directly so each
    test's expected mtm is exact, not approximate."""
    plan = _plan_dict()
    chain = [
        _quote(plan["short_ce_strike"], "CE", short_ce),
        _quote(plan["short_pe_strike"], "PE", short_pe),
        _quote(plan["hedge_ce_strike"], "CE", hedge_ce),
        _quote(plan["hedge_pe_strike"], "PE", hedge_pe),
    ]
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0,
                          chain=chain, timestamp=ts, source="dhan_historical")


def _cond():
    plan = _plan_dict()
    return sc.ShadowCondor(
        short_ce_strike=plan["short_ce_strike"], short_pe_strike=plan["short_pe_strike"],
        hedge_ce_strike=plan["hedge_ce_strike"], hedge_pe_strike=plan["hedge_pe_strike"],
        opened_at="2026-06-01T09:20:00", net_credit=plan["net_credit"],
        max_profit_inr=plan["max_profit_inr"], max_loss_inr=plan["max_loss_inr"],
        nominal_expiry="2026-06-04",
    ), plan


def test_default_policy_reproduces_hold_to_expiry_exactly():
    """No policy passed at all -- must settle at expiry, same as before
    this change existed."""
    condor, plan = _cond()
    base = datetime(2026, 6, 1, 9, 20)
    # cost_to_close drops a lot (premium decayed hard) -- would trip a
    # profit target if one were active, but none is here.
    cycles = [(_snapshot_for_cost(base + timedelta(minutes=5 * i), 2.0, 2.0, 0.5, 0.5), [], {})
             for i in range(3)]
    out = sc._walk_condor_forward(cycles, plan, condor)  # no policy arg at all
    assert out.exit_reason == "expiry_settlement"


def test_profit_target_closes_early_when_reached():
    condor, plan = _cond()
    policy = sc.CondorPolicy(profit_target_pct=50.0)
    base = datetime(2026, 6, 1, 9, 20)
    # Premium decays to near nothing fast -> cost_to_close ~0 -> mtm ~ max_profit
    cycles = [(_snapshot_for_cost(base + timedelta(minutes=5 * i), 1.0, 1.0, 0.5, 0.5), [], {})
             for i in range(5)]
    out = sc._walk_condor_forward(cycles, plan, condor, policy)
    assert out.exit_reason == "profit_target"
    assert out.pnl_inr >= 0.5 * plan["max_profit_inr"]
    assert out.closed_at is not None


def test_profit_target_not_hit_settles_at_expiry():
    condor, plan = _cond()
    policy = sc.CondorPolicy(profit_target_pct=90.0)  # very hard to reach
    base = datetime(2026, 6, 1, 9, 20)
    # Premium barely moves -> mtm stays small, well under 90% of max profit
    cycles = [(_snapshot_for_cost(base + timedelta(minutes=5 * i), 39.0, 39.0, 20.0, 20.0), [], {})
             for i in range(3)]
    out = sc._walk_condor_forward(cycles, plan, condor, policy)
    assert out.exit_reason == "expiry_settlement"


def test_stop_loss_closes_early_when_breached():
    condor, plan = _cond()
    policy = sc.CondorPolicy(stop_loss_credit_multiple=2.0)  # stop at -2x credit
    base = datetime(2026, 6, 1, 9, 20)
    # Shorts blow out hard (spot ran toward a strike) -> cost_to_close
    # rises well past 2x net_credit -> mtm deeply negative.
    cycles = [(_snapshot_for_cost(base + timedelta(minutes=5 * i), 150.0, 2.0, 5.0, 0.5), [], {})
             for i in range(5)]
    out = sc._walk_condor_forward(cycles, plan, condor, policy)
    assert out.exit_reason == "stop_loss"
    assert out.pnl_inr <= -2.0 * plan["net_credit_inr"]


def test_stop_loss_not_breached_settles_at_expiry():
    condor, plan = _cond()
    policy = sc.CondorPolicy(stop_loss_credit_multiple=5.0)  # very wide stop
    base = datetime(2026, 6, 1, 9, 20)
    cycles = [(_snapshot_for_cost(base + timedelta(minutes=5 * i), 39.0, 39.0, 20.0, 20.0), [], {})
             for i in range(3)]
    out = sc._walk_condor_forward(cycles, plan, condor, policy)
    assert out.exit_reason == "expiry_settlement"


def test_profit_target_checked_before_stop_loss_when_both_set_and_target_hit():
    """Sanity: with both rules active and only the profit condition
    actually true, the exit reason must reflect that, not misfire."""
    condor, plan = _cond()
    policy = sc.CondorPolicy(profit_target_pct=50.0, stop_loss_credit_multiple=2.0)
    base = datetime(2026, 6, 1, 9, 20)
    cycles = [(_snapshot_for_cost(base + timedelta(minutes=5 * i), 1.0, 1.0, 0.5, 0.5), [], {})
             for i in range(5)]
    out = sc._walk_condor_forward(cycles, plan, condor, policy)
    assert out.exit_reason == "profit_target"


def test_early_close_marks_at_current_price_not_intrinsic():
    """_close_early must NOT fall back to intrinsic value the way
    _settle_at_expiry does -- a real book still exists at an early exit,
    this isn't a forced settlement."""
    condor, plan = _cond()
    policy = sc.CondorPolicy(profit_target_pct=50.0)
    base = datetime(2026, 6, 1, 9, 20)
    cycles = [(_snapshot_for_cost(base + timedelta(minutes=5 * i), 1.0, 1.0, 0.5, 0.5), [], {})
             for i in range(5)]
    out = sc._walk_condor_forward(cycles, plan, condor, policy)
    lot_size = ccfg.NIFTY_LOT_SIZE
    expected_mtm = (plan["net_credit"] - ((1.0 + 1.0) - (0.5 + 0.5))) * lot_size
    assert out.pnl_inr == pytest.approx(expected_mtm)
