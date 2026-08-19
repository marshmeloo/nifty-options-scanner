"""
Tests for orb.py -- opening-range computation and per-day trade
simulation. RESEARCH ONLY; nothing here is wired into a live decision.

Bars are OPEN-STAMPED 5-minute bars (the 09:15 bar covers 09:15->09:20),
verified empirically -- see orb_candle_cache.py's docstring. Every
fixture below follows that convention.

Run: python -m pytest tests/test_orb.py -q
"""

import pytest

from research import orb


def _bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def _flat_day(start_price=24000.0, bars=None):
    """A full session of bars; caller supplies the interesting ones."""
    out = list(bars or [])
    have = {b["t"] for b in out}
    mins = 9 * 60 + 15
    while mins <= 15 * 60 + 25:
        t = f"{mins // 60:02d}:{mins % 60:02d}"
        if t not in have:
            out.append(_bar(t, start_price, start_price, start_price, start_price))
        mins += 5
    return sorted(out, key=lambda b: b["t"])


# --------------------------------------------------------------------------
# opening_range
# --------------------------------------------------------------------------

def test_5min_range_is_exactly_the_first_bar():
    bars = _flat_day(bars=[_bar("09:15", 24000, 24050, 23980, 24030),
                           _bar("09:20", 24030, 24100, 24000, 24090)])
    orange = orb.opening_range(bars, 5)
    assert orange["high"] == 24050
    assert orange["low"] == 23980
    assert orange["open"] == 24000
    assert orange["close"] == 24030
    assert orange["bars"] == 1
    assert orange["end_hhmm"] == "09:20"


def test_15min_range_spans_three_bars():
    bars = _flat_day(bars=[_bar("09:15", 24000, 24050, 23980, 24030),
                           _bar("09:20", 24030, 24120, 24010, 24090),
                           _bar("09:25", 24090, 24100, 23950, 24000),
                           _bar("09:30", 24000, 24500, 23900, 24400)])
    orange = orb.opening_range(bars, 15)
    assert orange["high"] == 24120   # from the 09:20 bar
    assert orange["low"] == 23950    # from the 09:25 bar
    assert orange["bars"] == 3
    assert orange["end_hhmm"] == "09:30"
    # The 09:30 bar must NOT leak into a 15-minute range.
    assert orange["high"] < 24500


def test_30min_range_spans_six_bars():
    bars = _flat_day()
    orange = orb.opening_range(bars, 30)
    assert orange["bars"] == 6
    assert orange["end_hhmm"] == "09:45"


def test_incomplete_range_returns_none_rather_than_guessing():
    """A day whose data starts late must not silently produce a range
    built from fewer bars than asked for."""
    bars = [_bar("09:15", 24000, 24050, 23980, 24030)]
    assert orb.opening_range(bars, 15) is None


def test_no_bars_returns_none():
    assert orb.opening_range([], 15) is None


# --------------------------------------------------------------------------
# Entry rules
# --------------------------------------------------------------------------

def test_or_direction_enters_at_next_bar_open_with_no_breakout_needed():
    """Zarattini/Aziz 2025 form: direction from the OR's own close vs
    open, entered at the first post-range bar's OPEN."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24040),   # closed UP -> long
        _bar("09:20", 24040, 24060, 24030, 24050),
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="or_direction")
    trade = orb.simulate_day(bars, v, day="2026-01-01")
    assert trade["direction"] == "long"
    assert trade["entry_time"] == "09:20"
    assert trade["entry"] == 24040    # the 09:20 bar's OPEN
    assert trade["stop"] == 23950     # OR low


def test_or_direction_goes_short_when_the_range_closed_down():
    bars = _flat_day(bars=[_bar("09:15", 24050, 24060, 23950, 23960),
                           _bar("09:20", 23960, 23980, 23940, 23950)])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="or_direction")
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["direction"] == "short"
    assert trade["stop"] == 24060     # OR high


def test_or_direction_skips_a_doji_range():
    """Open == close gives no direction; the paper explicitly takes no
    position in that case."""
    bars = _flat_day(bars=[_bar("09:15", 24000, 24050, 23950, 24000)])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="or_direction")
    assert orb.simulate_day(bars, v, day="d") is None


def test_breakout_fills_at_the_level_not_the_bar_extreme():
    """A stop order at the OR high fills at that level. Filling at the
    bar's high would be inventing a price the order never had."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24300, 23990, 24280),   # blows through the level
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout")
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["direction"] == "long"
    assert trade["entry"] == 24050   # the level, NOT 24300


def test_breakout_takes_whichever_side_breaks_regardless_of_or_direction():
    """Classic ORB: the range closed UP but price broke DOWN first ->
    a short is still taken."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24040),   # closed up
        _bar("09:20", 24000, 24010, 23900, 23920),   # breaks the LOW
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout")
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["direction"] == "short"
    assert trade["entry"] == 23950


def test_breakout_or_direction_refuses_the_against_direction_break():
    """Zarattini 2024 form: range closed UP, so a downside break is NOT
    taken -- unlike plain `breakout` above, on the identical bars."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24040),   # closed up -> long only
        _bar("09:20", 24000, 24010, 23900, 23920),   # breaks the LOW
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout_or_direction")
    trade = orb.simulate_day(bars, v, day="d")
    assert trade is None


def test_close_confirm_ignores_a_wick_through_the_level():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24200, 23990, 24010),   # wicks above, closes back inside
    ])
    wick = orb.simulate_day(bars, orb.ORBVariant(name="t", or_minutes=5, entry="close_confirm"), day="d")
    plain = orb.simulate_day(bars, orb.ORBVariant(name="t", or_minutes=5, entry="breakout"), day="d")
    assert wick is None, "close_confirm must not trigger on a wick"
    assert plain is not None, "plain breakout SHOULD trigger on the same wick"


def test_buffer_suppresses_a_marginal_breach():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24051, 23990, 24040),   # 1pt over the level
    ])
    no_buf = orb.simulate_day(bars, orb.ORBVariant(name="t", or_minutes=5, entry="breakout"), day="d")
    with_buf = orb.simulate_day(
        bars, orb.ORBVariant(name="t", or_minutes=5, entry="breakout", buffer_pct=0.1), day="d")
    assert no_buf is not None
    assert with_buf is None


# --------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------

def test_stop_hit_gives_exactly_minus_one_r():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24060, 23990, 24055),   # long entry at 24050
        _bar("09:25", 24055, 24060, 23900, 23940),   # drops through the 23950 stop
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout")
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["exit_reason"] == "stop"
    assert trade["r_multiple"] == pytest.approx(-1.0)


def test_target_hit_gives_the_configured_r():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),   # risk = 24050-23950 = 100
        _bar("09:20", 24000, 24060, 24000, 24055),   # entry 24050
        _bar("09:25", 24055, 24260, 24050, 24250),   # 2R target = 24250
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout", target_r=2.0)
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["exit_reason"] == "target"
    assert trade["r_multiple"] == pytest.approx(2.0)


def test_stop_wins_when_both_stop_and_target_are_reachable_in_one_bar():
    """Conservative intrabar assumption, stated in the module docstring
    -- OHLC cannot say which came first, so the loss is assumed."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24060, 24000, 24055),
        _bar("09:25", 24055, 24300, 23900, 24100),   # spans both stop and 2R target
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout", target_r=2.0)
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["exit_reason"] == "stop"
    assert trade["ambiguous_bars"] >= 1


def test_eod_exit_when_neither_level_is_touched():
    bars = _flat_day(start_price=24055, bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24060, 24000, 24055),
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout")
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["exit_reason"] == "eod"
    assert trade["exit_time"] == "15:25"


def test_stop_can_trigger_on_the_entry_bar_itself():
    """A position opened mid-bar can be stopped out in that same bar --
    not doing this would systematically flatter results."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24060, 23940, 23945),   # breaks up THEN collapses through the stop
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout")
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["exit_reason"] == "stop"
    assert trade["entry_time"] == trade["exit_time"] == "09:20"


# --------------------------------------------------------------------------
# Filters and guards
# --------------------------------------------------------------------------

def test_min_or_width_filter_skips_a_narrow_range():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24005, 23995, 24000),   # 10pt range, ~0.04%
        _bar("09:20", 24000, 24300, 23990, 24280),
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout", min_or_width_pct=0.1)
    assert orb.simulate_day(bars, v, day="d") is None


def test_max_or_width_filter_skips_an_already_moved_range():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24500, 23500, 24400),   # 1000pt range, ~4%
        _bar("09:20", 24400, 24800, 24390, 24700),
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout", max_or_width_pct=1.0)
    assert orb.simulate_day(bars, v, day="d") is None


def test_long_only_rejects_a_short_signal():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23950, 24000),
        _bar("09:20", 24000, 24010, 23900, 23920),
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout", allow_short=False)
    assert orb.simulate_day(bars, v, day="d") is None


def test_entry_cutoff_blocks_a_late_breakout():
    late = _bar("15:05", 24000, 24500, 23990, 24400)
    bars = _flat_day(bars=[_bar("09:15", 24000, 24050, 23950, 24000), late])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout", entry_cutoff="15:00")
    assert orb.simulate_day(bars, v, day="d") is None


def test_min_risk_widens_a_too_tight_stop_instead_of_skipping():
    """The stop sits 2pts from entry; the floor pushes it out to 0.1% of
    price (~24pts) and the trade is STILL TAKEN. Skipping instead would
    bias which days each direction trades -- see ORBVariant.min_risk_pct."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23998, 24040),   # OR low 23998
        _bar("09:20", 24000, 24010, 23999, 24005),   # entry at 24000 -> only 2pts to the stop
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="or_direction", min_risk_pct=0.1)
    trade = orb.simulate_day(bars, v, day="d")
    assert trade is not None, "must widen, not skip"
    assert trade["stop_widened"] is True
    assert trade["risk_points"] == pytest.approx(24.0, abs=0.5)   # 0.1% of ~24000
    assert trade["stop"] == pytest.approx(24000 - 24.0, abs=0.5)


def test_min_risk_leaves_an_already_wide_stop_untouched():
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23800, 24040),   # OR low 23800, far away
        _bar("09:20", 24000, 24010, 23990, 24005),
    ])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="or_direction", min_risk_pct=0.1)
    trade = orb.simulate_day(bars, v, day="d")
    assert trade["stop_widened"] is False
    assert trade["stop"] == 23800   # the real OR low, not a floored value


def test_min_risk_floor_does_not_change_participation():
    """The whole reason for widening over skipping: every day that
    traded without the floor must still trade with it."""
    bars = _flat_day(bars=[
        _bar("09:15", 24000, 24050, 23998, 24040),
        _bar("09:20", 24000, 24010, 23999, 24005),
    ])
    without = orb.simulate_day(bars, orb.ORBVariant(name="t", or_minutes=5, entry="or_direction"), day="d")
    with_floor = orb.simulate_day(
        bars, orb.ORBVariant(name="t", or_minutes=5, entry="or_direction", min_risk_pct=0.1), day="d")
    assert (without is None) == (with_floor is None)


def test_zero_width_range_is_skipped_not_divided_by_zero():
    bars = _flat_day(bars=[_bar("09:15", 24000, 24000, 24000, 24000)])
    v = orb.ORBVariant(name="t", or_minutes=5, entry="breakout")
    assert orb.simulate_day(bars, v, day="d") is None


# --------------------------------------------------------------------------
# Random benchmark
# --------------------------------------------------------------------------

def test_random_entry_is_deterministic_per_day_and_seed():
    """The benchmark must be reproducible: same day + seed -> same
    direction, or results aren't comparable between runs."""
    bars = _flat_day(bars=[_bar("09:15", 24000, 24050, 23950, 24000)])
    v = orb.ORBVariant(name="rand", or_minutes=5, entry="random", seed=7)
    a = orb.simulate_day(bars, v, day="2026-03-04")
    b = orb.simulate_day(bars, v, day="2026-03-04")
    assert a["direction"] == b["direction"]


def test_random_entry_varies_across_days():
    bars = _flat_day(bars=[_bar("09:15", 24000, 24050, 23950, 24000)])
    v = orb.ORBVariant(name="rand", or_minutes=5, entry="random", seed=7)
    dirs = {orb.simulate_day(bars, v, day=f"2026-03-{d:02d}")["direction"] for d in range(1, 20)}
    assert dirs == {"long", "short"}, "a coin-flip benchmark must produce both directions"


def test_random_entry_uses_the_same_geometry_as_a_real_signal():
    """The benchmark is only meaningful if its stop/entry are built the
    same way -- same entry bar, same OR-based stop. The 09:15 bar closes
    ABOVE its open here so `or_direction` has a direction to take; a
    doji would (correctly) produce no trade and make the comparison
    vacuous."""
    bars = _flat_day(bars=[_bar("09:15", 24000, 24050, 23950, 24030),
                           _bar("09:20", 24020, 24060, 24010, 24050)])
    rand = orb.simulate_day(bars, orb.ORBVariant(name="r", or_minutes=5, entry="random", seed=1), day="d")
    real = orb.simulate_day(bars, orb.ORBVariant(name="t", or_minutes=5, entry="or_direction"), day="d")

    assert real["direction"] == "long"
    assert rand["entry"] == real["entry"] == 24020   # both at the 09:20 open
    assert rand["entry_time"] == real["entry_time"] == "09:20"
    assert rand["stop"] in (23950, 24050)            # OR low if long, OR high if short
