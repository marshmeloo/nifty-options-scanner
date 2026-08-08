"""
Tests for spread_cost_study.py -- the real-cost re-pricing of the
directional spread backtest.

Run: python -m pytest tests/test_spread_cost_study.py -q
"""
import json

import pytest

import spread_cost_study as scs


def _trade(pnl=1000.0, short_premium=60.0, hedge_premium=25.0, exit_reason="profit_target"):
    return {"pnl_inr": pnl, "short_premium": short_premium,
            "hedge_premium": hedge_premium, "exit_reason": exit_reason}


def test_spread_cost_charged_on_gross_premium_not_net_credit():
    """The whole point: both legs cross their own book. Charging the net
    credit (60-25=35) instead of the gross (60+25=85) would understate
    the cost ~2.4x here."""
    cost = scs._spread_cost(60.0, 25.0, spread_pct=1.0, lots=1, lot_size=100,
                            expiry_settled=True)
    # (60+25) * 1% * 100 = 85.0, NOT (60-25) * 1% * 100 = 35.0
    assert cost == pytest.approx(85.0)


def test_spread_cost_doubles_when_traded_out_vs_expiry_settled():
    settled = scs._spread_cost(60.0, 25.0, 1.0, lots=1, lot_size=100, expiry_settled=True)
    traded = scs._spread_cost(60.0, 25.0, 1.0, lots=1, lot_size=100, expiry_settled=False)
    assert traded == pytest.approx(settled * 2)


def test_taxes_lower_when_expiry_settled_fewer_orders():
    settled = scs._brokerage_and_taxes(60.0, 25.0, lots=1, lot_size=100, expiry_settled=True)
    traded = scs._brokerage_and_taxes(60.0, 25.0, lots=1, lot_size=100, expiry_settled=False)
    assert settled < traded


def test_analyse_reports_all_three_measured_scenarios(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps([_trade() for _ in range(10)]))
    result = scs.analyse(str(path))

    assert result["n_resolved"] == 10
    assert result["n_usable"] == 10
    assert set(result["scenarios"]) == {"median", "p75", "p90"}
    # Wider spread must never produce a BETTER net result.
    nets = [result["scenarios"][k]["net_total_inr"] for k in ("median", "p75", "p90")]
    assert nets[0] >= nets[1] >= nets[2]


def test_analyse_flags_and_excludes_trades_without_leg_premiums(tmp_path):
    """Pins the real case hit live: results saved BEFORE ShadowSpread
    gained short_premium/hedge_premium can't be costed, and must be
    reported as excluded rather than silently costed as zero."""
    stale = {"pnl_inr": 1000.0, "exit_reason": "profit_target"}
    path = tmp_path / "r.json"
    path.write_text(json.dumps([stale, stale, _trade()]))
    result = scs.analyse(str(path))

    assert result["n_resolved"] == 3
    assert result["n_missing_leg_premiums"] == 2
    assert result["n_usable"] == 1
    assert "WARNING" in scs.describe(result)


def test_net_is_always_below_gross(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps([_trade(pnl=500.0) for _ in range(20)]))
    result = scs.analyse(str(path))
    for name in result["scenarios"]:
        assert result["scenarios"][name]["net_total_inr"] < result["gross_total_inr"]
