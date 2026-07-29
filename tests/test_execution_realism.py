"""
Tests for the four audit fixes that remove optimistic bias from recorded
results and connect a signal that was being discarded:

  1. transaction costs        (costs.py)
  2. side-correct fills       (OptionQuote.buy_price / sell_price)
  3. liquidity screening      (scanner._is_liquid_enough)
  4. market-bias gating       (scanner.apply_bias_gate)

The first three make performance look WORSE, on purpose -- they remove
bias that was making a negative-expectancy system read as near
break-even. Tests here pin the direction of each correction, not just
that the code runs.

Run: python -m pytest tests/ -q
"""

import pytest

import config
import costs
import scanner
import trade_tracker as tt
from models import MarketSnapshot, OptionQuote
from plan_generator import build_plan


def _quote(**kw):
    base = dict(
        symbol="NIFTY", expiry="2026-08-04", strike=24050.0, option_type="PE",
        ltp=76.7, oi=50000, oi_change_pct=20.0, volume=5000, iv=12.0,
        iv_percentile=12.0, delta=-0.45,
    )
    base.update(kw)
    return OptionQuote(**base)


# --------------------------------------------------------------------------
# 1. Transaction costs
# --------------------------------------------------------------------------

def test_round_trip_cost_components_are_all_charged():
    bd = costs.round_trip(entry_price=76.7, exit_price=53.69, lots=1)
    for component in ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst"):
        assert bd[component] > 0, f"{component} not charged"
    assert bd["total_inr"] == pytest.approx(sum(
        bd[k] for k in ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst")
    ), abs=0.01)


def test_stt_is_charged_on_the_sell_side_only():
    """
    A classic modelling error is charging STT on both legs. On options it
    applies to the sell side, on premium.
    """
    cheap_exit = costs.round_trip(entry_price=100.0, exit_price=10.0, lots=1)
    rich_exit = costs.round_trip(entry_price=100.0, exit_price=200.0, lots=1)
    assert rich_exit["stt"] > cheap_exit["stt"]

    expected = config.STT_RATE_SELL * 200.0 * config.NIFTY_LOT_SIZE
    assert rich_exit["stt"] == pytest.approx(expected, abs=0.01)


def test_cost_in_r_is_material_against_a_real_plan():
    """
    The finding that motivated this: on the real 2026-07-28 plan, costs
    are a large fraction of a 1R risk unit -- comparable to the entire
    measured gross expectancy of -0.028R at the 2R target.
    """
    cost_r = costs.cost_in_r(entry_price=76.7, exit_price=53.69, stop_price=53.69)
    assert 0.02 < cost_r < 0.08, f"expected a material but not absurd cost, got {cost_r}R"


def test_cost_in_r_handles_degenerate_risk_unit():
    assert costs.cost_in_r(76.7, 80.0, stop_price=76.7) is None    # zero risk
    assert costs.cost_in_r(76.7, 80.0, stop_price=90.0) is None    # inverted


def test_apply_to_trade_keeps_gross_and_adds_net():
    trade = {"entry": 76.7, "exit_ltp": 92.0, "stop": 53.69, "lots": 1, "pnl_inr": 994.5}
    fields = costs.apply_to_trade(trade)

    assert fields["pnl_inr_net"] < trade["pnl_inr"], "net must be below gross"
    assert fields["costs_inr"] > 0
    assert fields["r_at_exit_net"] < (994.5 / ((76.7 - 53.69) * 65))
    assert "pnl_inr" not in fields, "gross must be preserved, not overwritten"


def test_costs_make_a_marginal_winner_a_loser():
    """The case that matters: a trade that is barely green gross, red net."""
    gross_inr = 40.0                      # smaller than a round trip's cost
    trade = {"entry": 76.7, "exit_ltp": 77.3, "stop": 53.69, "lots": 1, "pnl_inr": gross_inr}
    fields = costs.apply_to_trade(trade)
    assert gross_inr > 0
    assert fields["pnl_inr_net"] < 0


def test_expectancy_stats_use_net_r(monkeypatch):
    """
    Costs are close to a fixed R-charge per round trip, so including them
    penalises LOW targets disproportionately. Comparing targets on gross
    numbers flatters the low ones and pushes the choice the wrong way.
    """
    entries = [
        {"outcome": "LOSS", "max_r_reached": 2.5, "r_at_exit": 2.0,
         "r_at_exit_net": 1.95, "cost_r": 0.05},
        {"outcome": "LOSS", "max_r_reached": 0.6, "r_at_exit": -1.0,
         "r_at_exit_net": -1.05, "cost_r": 0.05},
    ]
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: entries)
    by_r = {m["r"]: m for m in tt.rr_milestone_stats()["milestones"]}

    # At a 0.5R target both reach it, so both net 0.5 - 0.05 = 0.45.
    assert by_r[0.5]["expectancy_r"] == pytest.approx(0.45, abs=0.001)


# --------------------------------------------------------------------------
# 2. Side-correct fills
# --------------------------------------------------------------------------

def test_buy_crosses_the_ask_and_sell_hits_the_bid():
    q = _quote(ltp=76.7, bid=76.0, ask=77.0)
    assert q.has_book
    assert q.buy_price == 77.0
    assert q.sell_price == 76.0
    assert q.spread_pct == pytest.approx(1.31, abs=0.01)


def test_falls_back_to_ltp_without_a_book():
    q = _quote(ltp=76.7, bid=None, ask=None)
    assert not q.has_book
    assert q.buy_price == q.sell_price == 76.7
    assert q.spread_pct is None


def test_crossed_or_zero_book_is_rejected():
    """A crossed book is bad data; using it would invent free money."""
    assert not _quote(bid=78.0, ask=77.0).has_book
    assert not _quote(bid=0, ask=77.0).has_book


def test_plan_entry_uses_the_ask_not_ltp(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", True)
    q = _quote(ltp=76.7, bid=76.0, ask=77.5)
    snap = MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [q])
    setup = scanner.Setup("NIFTY", 24050.0, "PE", "2026-08-04", ["x"], 6.0)

    plan = build_plan(snap, setup)
    assert plan.entry == 77.5
    assert "ask" in plan.entry_basis


def test_plan_entry_falls_back_to_ltp_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", False)
    q = _quote(ltp=76.7, bid=76.0, ask=77.5)
    snap = MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [q])
    setup = scanner.Setup("NIFTY", 24050.0, "PE", "2026-08-04", ["x"], 6.0)
    assert build_plan(snap, setup).entry == 76.7


def test_exit_price_uses_the_bid(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", True)
    assert tt.exit_price_for(_quote(ltp=76.7, bid=76.0, ask=77.5)) == 76.0


def test_realistic_fills_are_strictly_worse_than_ltp_marking(monkeypatch):
    """
    The whole point: buying at the ask and selling at the bid must never
    look BETTER than marking both at LTP.
    """
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", True)
    entry_q = _quote(ltp=76.7, bid=76.0, ask=77.5)
    exit_q = _quote(ltp=92.0, bid=91.2, ask=92.6)

    realistic = tt.exit_price_for(exit_q) - entry_q.buy_price     # 91.2 - 77.5
    optimistic = exit_q.ltp - entry_q.ltp                          # 92.0 - 76.7
    assert realistic < optimistic


# --------------------------------------------------------------------------
# 3. Liquidity screen
# --------------------------------------------------------------------------

def test_illiquid_contracts_are_screened_out(monkeypatch):
    monkeypatch.setattr(config, "MIN_OI_TO_TRADE", 5000)
    monkeypatch.setattr(config, "MIN_VOLUME_TO_TRADE", 1000)
    monkeypatch.setattr(config, "MAX_SPREAD_PCT", 3.0)

    assert scanner._is_liquid_enough(_quote(oi=50000, volume=5000, bid=76.0, ask=77.0))
    assert not scanner._is_liquid_enough(_quote(oi=100, volume=5000))       # thin OI
    assert not scanner._is_liquid_enough(_quote(oi=50000, volume=50))       # thin volume
    assert not scanner._is_liquid_enough(                                   # wide spread
        _quote(oi=50000, volume=5000, bid=70.0, ask=80.0))


def test_missing_book_does_not_reject_on_spread(monkeypatch):
    """
    The CSV and TradingView tiers publish no book. A missing input must
    not silently reject the whole chain and leave the scanner blind.
    """
    monkeypatch.setattr(config, "MAX_SPREAD_PCT", 1.0)
    assert scanner._is_liquid_enough(_quote(oi=50000, volume=5000, bid=None, ask=None))


def test_liquidity_checks_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "MIN_OI_TO_TRADE", None)
    monkeypatch.setattr(config, "MIN_VOLUME_TO_TRADE", None)
    monkeypatch.setattr(config, "MAX_SPREAD_PCT", None)
    assert scanner._is_liquid_enough(_quote(oi=1, volume=1, bid=10.0, ask=90.0))


def test_scan_excludes_illiquid_strikes(monkeypatch):
    monkeypatch.setattr(config, "MIN_OI_TO_TRADE", 5000)
    monkeypatch.setattr(config, "MIN_VOLUME_TO_TRADE", 1000)
    liquid = _quote(strike=24050.0, oi=50000, volume=8000, buildup_type="long_buildup")
    illiquid = _quote(strike=24100.0, oi=10, volume=5, buildup_type="long_buildup")
    snap = MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [liquid, illiquid])

    strikes = {s.strike for s in scanner.scan(snap)}
    assert 24050.0 in strikes
    assert 24100.0 not in strikes


# --------------------------------------------------------------------------
# 4. Market-bias gating
# --------------------------------------------------------------------------

def test_bias_conflict_detects_fighting_a_strong_trend():
    assert scanner.bias_conflict("PE", "bullish", 2.0) is True
    assert scanner.bias_conflict("CE", "bearish", -2.0) is True
    assert scanner.bias_conflict("CE", "bullish", 2.0) is False
    assert scanner.bias_conflict("PE", "bearish", -2.0) is False


def test_weak_or_neutral_bias_gates_nothing():
    """In a range neither side is fighting the tape."""
    assert scanner.bias_conflict("PE", "bullish", 1.0) is False       # below threshold
    assert scanner.bias_conflict("PE", "neutral/range", 0.0) is False
    assert scanner.bias_conflict("PE", None, None) is False


def _setup(option_type="PE", score=6.0):
    return scanner.Setup("NIFTY", 24050.0, option_type, "2026-08-04", ["x"], score)


def test_bias_gate_penalises_by_default(monkeypatch):
    monkeypatch.setattr(config, "BIAS_GATING_MODE", "penalise")
    monkeypatch.setattr(config, "BIAS_CONFLICT_PENALTY", 1.0)
    blocked, penalty, note = scanner.apply_bias_gate(_setup("PE"), "bullish", 2.0)

    assert blocked is False
    assert penalty == 1.0
    assert "against a bullish market bias" in note


def test_bias_gate_can_block(monkeypatch):
    monkeypatch.setattr(config, "BIAS_GATING_MODE", "block")
    blocked, penalty, note = scanner.apply_bias_gate(_setup("PE"), "bullish", 2.0)
    assert blocked is True
    assert "blocked" in note


def test_bias_gate_off_restores_previous_behaviour(monkeypatch):
    monkeypatch.setattr(config, "BIAS_GATING_MODE", "off")
    assert scanner.apply_bias_gate(_setup("PE"), "bullish", 2.0) == (False, 0.0, None)


def test_aligned_candidate_is_never_penalised(monkeypatch):
    monkeypatch.setattr(config, "BIAS_GATING_MODE", "penalise")
    assert scanner.apply_bias_gate(_setup("CE"), "bullish", 2.0) == (False, 0.0, None)


def test_counter_bias_trade_can_be_gated_out_at_the_bar(monkeypatch):
    """
    End-to-end intent: a candidate that would otherwise have cleared the
    conviction bar is stopped by the bias penalty. Before this existed,
    bias was computed, logged, and then discarded entirely.
    """
    monkeypatch.setattr(config, "BIAS_GATING_MODE", "penalise")
    monkeypatch.setattr(config, "BIAS_CONFLICT_PENALTY", 1.0)
    monkeypatch.setattr(config, "MIN_CONVICTION_SCORE_TO_TRACK", 5.0)
    monkeypatch.setattr(tt, "apply_learned_adjustment", lambda score, reasons: (score, []))

    setup = _setup("PE", score=5.5)
    _blocked, penalty, _note = scanner.apply_bias_gate(setup, "bullish", 2.0)
    assert setup.score - penalty < config.MIN_CONVICTION_SCORE_TO_TRACK
