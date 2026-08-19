"""
Tests for research/stock_strategies.py and research/stock_costs.py.

Weighted toward the properties that would silently flatter results if
wrong: that scale-out fractions actually sum to the whole position,
that the runner's stop moves to breakeven after T1 (without which the
runner is unfairly worse than a fixed target), that an ambiguous bar
resolves to the STOP, and that the cost model charges STT on the sell
side only.

Run: python -m pytest tests/test_stock_strategies.py -q
"""

import pytest

from research import stock_costs as sc
from research import stock_strategies as ss


def _bar(t, o, h, l, c, v=1000):
    return [t, o, h, l, c, v]


def _day(or_bars, after_bars):
    """3 opening bars (09:15/20/25) then the rest from 09:30."""
    out = []
    mins = 9 * 60 + 15
    for b in or_bars:
        out.append(_bar(f"{mins//60:02d}:{mins%60:02d}", *b))
        mins += 5
    for b in after_bars:
        out.append(_bar(f"{mins//60:02d}:{mins%60:02d}", *b))
        mins += 5
    return out


# range 100-110, closes up -> long bias, height 10
UP_OR = [(100, 105, 100, 103), (103, 108, 102, 106), (106, 110, 105, 109)]


# --------------------------------------------------------------------------
# Opening range / entry
# --------------------------------------------------------------------------

def test_opening_range_uses_only_the_selection_window():
    bars = _day(UP_OR, [(109, 200, 108, 199)])
    orange = ss.opening_range(bars, 15)
    assert orange["high"] == 110      # not the 200 from the 09:30 bar
    assert orange["low"] == 100
    assert orange["end_min"] == 9 * 60 + 30


def test_momentum_enters_at_next_bar_open_in_the_range_direction():
    bars = _day(UP_OR, [(109, 111, 108, 110)] * 10)
    t = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="eod"))
    assert t["direction"] == "long"
    assert t["entry"] == 109          # the 09:30 bar's OPEN


def test_orb_waits_for_the_breakout_and_fills_at_the_level():
    bars = _day(UP_OR, [(105, 106, 104, 105), (105, 115, 104, 114)] + [(114, 115, 113, 114)] * 8)
    t = ss.simulate(bars, ss.StockVariant(name="t", entry="orb", exit="eod"))
    assert t["direction"] == "long"
    assert t["entry"] == 110          # the range high, not the bar's own high


def test_pullback_requires_a_retrace_before_entering():
    # Never comes back into the range -> pullback takes nothing, while
    # plain momentum would have entered.
    straight_up = _day(UP_OR, [(112, 120, 111, 119)] * 10)
    assert ss.simulate(straight_up, ss.StockVariant(name="t", entry="pullback")) is None
    assert ss.simulate(straight_up, ss.StockVariant(name="t", entry="momentum")) is not None


def test_doji_opening_range_gives_no_direction():
    flat = [(100, 105, 100, 100), (100, 105, 100, 100), (100, 110, 100, 100)]
    bars = _day(flat, [(100, 101, 99, 100)] * 5)
    assert ss.simulate(bars, ss.StockVariant(name="t", entry="momentum")) is None


# --------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------

def test_stop_gives_minus_one_r():
    bars = _day(UP_OR, [(109, 110, 98, 99)] * 5)   # entry 109, risk 10 -> stop 99
    t = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="eod"))
    assert t["outcome"] == "stop"
    assert t["r_multiple"] == pytest.approx(-1.0)


def test_fixed_target_gives_the_configured_r():
    bars = _day(UP_OR, [(109, 130, 108, 129)] * 5)   # 2R target = 129
    t = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="fixed_r", target_r=2.0))
    assert t["outcome"] == "target"
    assert t["r_multiple"] == pytest.approx(2.0)


def test_ambiguous_bar_resolves_to_the_stop():
    """A bar spanning both stop and target must be scored as the loss --
    OHLC cannot say which came first."""
    bars = _day(UP_OR, [(109, 130, 98, 120)] * 3)
    t = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="fixed_r", target_r=2.0))
    assert t["outcome"] == "stop"
    assert t["ambiguous_bars"] >= 1


def test_runner_scale_out_fractions_sum_to_the_whole_position():
    bars = _day(UP_OR, [(109, 120, 108, 119), (119, 130, 118, 129)] + [(129, 130, 128, 129)] * 5)
    t = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="runner"))
    assert sum(f for f, _, _ in t["exits"]) == pytest.approx(1.0)


def test_runner_moves_stop_to_breakeven_after_t1():
    """
    After T1 pays, the remainder must not be able to lose. Without this
    the runner is strictly worse than a fixed target and every
    comparison against it is unfair.
    """
    # Hits T1 (119) then collapses well below the original stop (99).
    bars = _day(UP_OR, [(109, 120, 108, 119), (119, 120, 90, 91)] + [(91, 92, 90, 91)] * 5)
    t = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="runner"))
    reasons = [why for _, _, why in t["exits"]]
    assert "t1" in reasons
    # The surviving portion exits at entry, not at the original stop.
    assert t["r_multiple"] > -1.0
    assert any(px == pytest.approx(109) for _, px, why in t["exits"] if why == "stop")


def test_runner_beats_fixed_target_on_a_big_trend_day():
    """The whole point of a runner: it should capture more than a capped
    2R target when the move keeps going."""
    bars = _day(UP_OR, [(109, 120, 108, 119), (119, 135, 118, 134)] + [(134, 200, 133, 199)] * 5)
    runner = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="runner"))
    fixed = ss.simulate(bars, ss.StockVariant(name="t", entry="momentum", exit="fixed_r", target_r=2.0))
    assert runner["r_multiple"] > fixed["r_multiple"]


def test_entry_cutoff_blocks_a_late_breakout():
    """
    The quiet bars must stay strictly INSIDE the 100-110 opening range.
    An earlier version of this fixture used lows of 99, which breached
    the range low on the very first post-range bar and entered a short
    at 09:30 -- the test passed judgement on the cutoff while actually
    exercising an immediate entry.
    """
    quiet = [(105, 106, 104, 105)] * 60 + [(105, 200, 104, 199)]
    bars = _day(UP_OR, quiet)
    assert ss.simulate(bars, ss.StockVariant(name="t", entry="orb", entry_cutoff="14:30")) is None
    # Same bars without a cutoff DO produce the late entry, proving the
    # fixture reaches the breakout at all.
    assert ss.simulate(bars, ss.StockVariant(name="t", entry="orb", entry_cutoff="15:30")) is not None


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------

def test_random_control_is_deterministic_per_symbol_day_seed():
    bars = _day(UP_OR, [(109, 111, 108, 110)] * 5)
    v = ss.StockVariant(name="r", entry="random", seed=3)
    a = ss.simulate(bars, v, day="2026-01-01", symbol="X")
    b = ss.simulate(bars, v, day="2026-01-01", symbol="X")
    assert a["direction"] == b["direction"]


def test_random_control_produces_both_directions_across_days():
    bars = _day(UP_OR, [(109, 111, 108, 110)] * 5)
    v = ss.StockVariant(name="r", entry="random", seed=3)
    dirs = {ss.simulate(bars, v, day=f"2026-01-{d:02d}", symbol="X")["direction"] for d in range(1, 20)}
    assert dirs == {"long", "short"}


# --------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------

def test_stt_is_charged_on_the_sell_side_only():
    """Intraday equity STT is sell-side. A round trip at a HIGHER exit
    therefore pays more STT than one at a lower exit."""
    hi = sc.statutory_costs(100.0, 110.0, 100)
    lo = sc.statutory_costs(100.0, 90.0, 100)
    assert hi["stt"] > lo["stt"]
    assert hi["stt"] == pytest.approx(110.0 * 100 * sc.STT_RATE_SELL)


def test_brokerage_is_capped_per_order():
    """0.03% would be huge on a large notional; the Rs 20/order cap must bind."""
    c = sc.statutory_costs(10_000.0, 10_000.0, 100)   # Rs 10L per leg
    assert c["brokerage"] == pytest.approx(2 * sc.BROKERAGE_CAP_PER_ORDER)


def test_slippage_scales_with_bps_and_both_legs():
    a = sc.slippage_cost(100.0, 100.0, 10, slippage_bps=10)
    b = sc.slippage_cost(100.0, 100.0, 10, slippage_bps=20)
    assert b == pytest.approx(2 * a)
    assert a == pytest.approx((100 + 100) * 10 * 0.001)


def test_break_even_slippage_is_zero_when_already_unprofitable():
    trades = [{"gross_inr": -100.0, "turnover_inr": 10_000.0}]
    assert sc.break_even_slippage_bps(trades) == 0.0


def test_break_even_slippage_matches_the_hand_calculation():
    """Rs 100 of gross on Rs 200,000 of turnover -> 5 bps."""
    trades = [{"gross_inr": 100.0, "turnover_inr": 200_000.0}]
    assert sc.break_even_slippage_bps(trades) == pytest.approx(5.0)


def test_slippage_is_material_next_to_statutory_costs():
    """Pins the reason this track is cost-gated: at a modest 5 bps,
    slippage is already comparable to every statutory charge combined."""
    stat = sc.statutory_costs(1000.0, 1010.0, 50)["total"]
    slip = sc.slippage_cost(1000.0, 1010.0, 50, slippage_bps=5)
    assert slip > 0.5 * stat
