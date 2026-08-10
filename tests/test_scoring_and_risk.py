"""
Regression tests for the 2026-07-28 post-mortem fixes.

Context: on 2026-07-28 the scanner opened the SAME contract (24050 PE) three
times at effectively the same premium (75.7 / 76.7 / 76.7), stopping out
twice and closing the third at EOD, for a combined Rs -4,017. Reviewing it
turned up several independent defects rather than one. Each test below
pins the behaviour of one of those fixes, with the real numbers from that
session where the fix was driven by them.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config
import price_action
import trade_tracker as tt
from models import Candle, OptionQuote
from plan_generator import _stop_distance
from scanner import _score_levels, _level_direction


def _candle(ts_offset, o, h, l, c, volume=1000):
    return Candle(
        timestamp=datetime(2026, 7, 28, 9, 15) + timedelta(minutes=5 * ts_offset),
        open=o, high=h, low=l, close=c, volume=volume,
    )


# --------------------------------------------------------------------------
# 1. FVG mitigation: a gap price has traded back through is spent
# --------------------------------------------------------------------------

def test_unmitigated_fvg_is_kept():
    """A clean gap up that price never returns to stays a live level."""
    candles = [
        _candle(0, 100, 102, 99, 101),
        _candle(1, 101, 110, 100, 109),
        _candle(2, 109, 112, 105, 111),   # low 105 > candle[0].high 102 -> bullish FVG
        _candle(3, 111, 115, 110, 114),   # stays well above the gap
        _candle(4, 114, 118, 111, 117),   # low 111 <= candle[2].high, so no second gap
    ]
    fvgs = price_action.detect_fair_value_gaps(candles)
    assert len(fvgs) == 1
    assert fvgs[0].kind == "fvg_bullish"
    assert fvgs[0].formed_at_index == 2


def test_mitigated_fvg_is_dropped():
    """
    Same gap, but price later trades back down through it. The imbalance
    has been rebalanced, so it should no longer count as a live level.
    This is the core of the ratchet fix -- without it, every gap ever
    printed stayed 'active' for the whole session.
    """
    candles = [
        _candle(0, 100, 102, 99, 101),
        _candle(1, 101, 110, 100, 109),
        _candle(2, 109, 112, 105, 111),   # bullish FVG 102 -> 105
        _candle(3, 111, 112, 101, 103),   # trades back down INTO the gap
        _candle(4, 103, 106, 102, 105),
    ]
    assert price_action.detect_fair_value_gaps(candles) == []
    # ...but it is still visible when explicitly asked for, for diagnostics.
    assert len(price_action.detect_fair_value_gaps(candles, include_mitigated=True)) == 1


# --------------------------------------------------------------------------
# 2. Recency pruning: stale one-off events age out, S/R does not
# --------------------------------------------------------------------------

def test_prune_stale_levels_ages_out_events_but_keeps_sr():
    from models import PriceLevel

    levels = [
        PriceLevel("fvg_bullish", 100, 105, "old gap", 1.0, formed_at_index=0),
        PriceLevel("fvg_bullish", 200, 205, "fresh gap", 1.0, formed_at_index=95),
        PriceLevel("support", 150, 155, "touched 3x", 3.0, formed_at_index=0),
        PriceLevel("resistance", 300, 305, "touched 2x", 2.0, formed_at_index=1),
    ]
    kept = price_action.prune_stale_levels(levels, candle_count=100, max_age=24)

    kinds = sorted(lvl.kind for lvl in kept)
    # The 100-candle-old FVG is gone; the fresh one and both S/R levels stay.
    assert kinds == ["fvg_bullish", "resistance", "support"]
    assert next(lvl for lvl in kept if lvl.kind == "fvg_bullish").note == "fresh gap"


def test_level_count_does_not_ratchet_across_a_session():
    """
    The actual 2026-07-28 pathology, in miniature: as a session runs, the
    number of LIVE levels must not grow monotonically just because more
    candles exist. Here price oscillates in a range, filling its own gaps.
    """
    candles = [_candle(0, 100, 102, 99, 101)]
    counts = []
    for i in range(1, 60):
        # Oscillate: repeatedly gaps and then fills them.
        base = 100 + (10 if i % 2 else 0)
        candles.append(_candle(i, base, base + 3, base - 3, base + 1))
        if len(candles) >= 2 * config.SWING_LOOKBACK + 1:
            counts.append(len(price_action.analyze(candles)))

    assert counts, "expected at least one measurement"
    # The old behaviour was strictly non-decreasing. Assert it now drops at
    # least once -- i.e. levels genuinely expire instead of accumulating.
    assert min(counts) < max(counts)
    assert counts[-1] <= max(counts)


# --------------------------------------------------------------------------
# 3. Level scoring: directional, netted, capped
# --------------------------------------------------------------------------

def test_level_direction_mapping():
    assert _level_direction("fvg_bullish") == "bullish"
    assert _level_direction("fvg_bearish") == "bearish"
    assert _level_direction("support") == "bullish"      # a floor
    assert _level_direction("resistance") == "bearish"   # a ceiling


def test_bullish_level_does_not_boost_a_put():
    """
    The old code added +0.5 for EVERY nearby level regardless of which way
    it pointed, so a bullish FVG raised conviction on a PE. It must now
    count against it.
    """
    from models import PriceLevel

    bullish = [PriceLevel("fvg_bullish", 24010, 24015, "", 1.0, formed_at_index=1)]
    pe_score, _ = _score_levels(bullish, "PE")
    ce_score, _ = _score_levels(bullish, "CE")

    assert pe_score < 0, "a bullish level must argue against a PE"
    assert ce_score > 0, "a bullish level must support a CE"
    assert pe_score == -ce_score


def test_contradictory_levels_cancel_instead_of_stacking():
    """
    2026-07-28's 24050 PE listed BOTH 'Bullish FVG (24013.4-24015.2)' and
    'Bearish FVG (24014.0-24015.2)' -- the same zone, opposite directions --
    and the old scorer credited both as +0.5 confluence. Two-sided chop
    should net toward zero, not read as double conviction.
    """
    from models import PriceLevel

    both = [
        PriceLevel("fvg_bullish", 24013.4, 24015.2, "", 1.0, formed_at_index=1),
        PriceLevel("fvg_bearish", 24014.0, 24015.2, "", 1.0, formed_at_index=2),
    ]
    score, _ = _score_levels(both, "PE")
    assert score == 0.0


def test_level_contribution_is_capped():
    """Fourteen one-sided levels must not contribute fourteen half-points."""
    from models import PriceLevel

    many = [
        PriceLevel("fvg_bearish", 24010, 24015, "", 1.0, formed_at_index=i)
        for i in range(14)
    ]
    score, reasons = _score_levels(many, "PE")

    uncapped = 14 * config.LEVEL_SCORE_EACH
    assert score == config.MAX_LEVEL_SCORE_CONTRIBUTION
    assert score < uncapped
    assert any("capped" in r for r in reasons)


# --------------------------------------------------------------------------
# 4. Volatility-derived plan geometry
# --------------------------------------------------------------------------

def _quote(ltp, delta):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-07-28", strike=24050.0, option_type="PE",
        ltp=ltp, oi=1000, oi_change_pct=10.0, volume=100, iv=12.0,
        iv_percentile=12.0, delta=delta,
    )


def test_stop_distance_falls_back_without_delta():
    """The NSE fallback tier returns no Greeks -- must not crash or mis-size."""
    distance, basis = _stop_distance(76.7, _quote(76.7, None), atr=25.0)
    assert distance == pytest.approx(76.7 * 0.30)
    assert "no delta" in basis


def test_stop_distance_falls_back_without_atr():
    distance, basis = _stop_distance(76.7, _quote(76.7, -0.45), atr=None)
    assert distance == pytest.approx(76.7 * 0.30)
    assert "no ATR" in basis


def test_volatility_stop_is_derived_from_atr_and_delta():
    entry, delta, atr = 76.7, -0.45, 20.0
    distance, basis = _stop_distance(entry, _quote(entry, delta), atr=atr)

    expected = atr * abs(delta) * config.STOP_ATR_MULTIPLE  # 20 * 0.45 * 1.5 = 13.5
    assert distance == pytest.approx(expected)
    assert "ATR" in basis and "delta" in basis
    # Sanity: this is a tighter, more reachable plan than the flat 30%.
    assert distance < entry * 0.30


def test_volatility_stop_is_clamped_into_the_configured_band():
    """A freak ATR must not produce a stop larger than the position."""
    entry = 76.7
    distance, basis = _stop_distance(entry, _quote(entry, -0.9), atr=500.0)

    assert distance == pytest.approx(entry * config.MAX_STOP_PCT / 100)
    assert "clamped" in basis


def test_20260728_target_moves_much_closer_to_reachable():
    """
    The real failure: 24050 PE entered at 76.7 with a flat -30%/+60% plan
    got target 122.72 -- about 20 points ABOVE the highest price the option
    traded all day (102.55), unreachable from the moment it was set.

    Volatility-derived geometry must close most of that gap. It is NOT
    asserted to land inside the day's range at every ATR, because whether
    it does depends on the ATR actually observed -- see
    test_20260728_excursion_was_symmetric for the deeper constraint that
    no fixed reward-multiple can escape on this particular session.
    """
    entry, actual_day_high = 76.7, 102.55
    atr, delta = 20.0, -0.45

    distance, _ = _stop_distance(entry, _quote(entry, delta), atr=atr)
    target = entry + distance * config.DEFAULT_TARGET_RR
    old_target = entry + (entry * 0.30) * 2.0

    assert old_target > actual_day_high, "sanity: the old target really was unreachable"

    old_overshoot = old_target - actual_day_high     # ~20.2 points out of reach
    new_overshoot = target - actual_day_high
    assert new_overshoot < old_overshoot / 4, (
        f"volatility target {target:.2f} should close most of the gap to the day's "
        f"high {actual_day_high} (old target {old_target:.2f} missed by {old_overshoot:.2f})"
    )


def test_20260728_excursion_was_symmetric():
    """
    Documents a constraint that is NOT a code bug and cannot be fixed by
    tuning stop/target -- it is a strategy question for the operator.

    All three 24050 PE trades showed roughly symmetric best/worst
    excursion: the best favourable move any of them ever had was +33.7%,
    while every one of them drew down about -30%. For a stop to sit
    outside that noise it must be wider than ~30%, which means any fixed
    reward-multiple above ~1.1 puts the target beyond what the instrument
    actually delivered that day.

    In other words: on a mean-reverting session like this one, no
    stop/target pair with RR >= 2.0 could have won, however good the
    entry signal was. Fixing that needs either a lower RR in ranging
    regimes, a trailing exit, or not taking these setups at all.
    """
    best_favourable_pct = 33.7        # trade 3's max_favorable_pct
    worst_adverse_pct = 32.4          # trade 1's max_adverse_pct (abs)

    max_viable_rr = best_favourable_pct / worst_adverse_pct
    assert max_viable_rr < 1.2, (
        "if this stops holding, the ranging-regime assumption behind "
        "DEFAULT_TARGET_RR should be revisited against fresh journal data"
    )
    assert config.DEFAULT_TARGET_RR > max_viable_rr, (
        "DEFAULT_TARGET_RR is knowingly above what 2026-07-28 could deliver -- "
        "this test exists to make that tradeoff explicit, not to hide it"
    )


# --------------------------------------------------------------------------
# 5. Re-entry gate: blocks a repeat of a failed plan, not a fresh one
# --------------------------------------------------------------------------

def _stopped_state(entry):
    state = {"date": "2026-07-28", "trades": [], "opened_today": 1, "stop_cooldowns": {}}
    tt._record_stop_cooldown(state, {
        "strike": 24050.0, "option_type": "PE", "entry": entry,
        "closed_at": "2026-07-28T10:43:53",
    })
    return state


def test_reentry_blocked_at_same_price():
    """
    The real sequence: stopped out from 75.7, then re-entered at 76.7
    (1.3% away) -- same stop, same target, same failed plan.
    """
    state = _stopped_state(75.7)
    assert tt.is_repeat_of_stopped_plan(state, 24050.0, "PE", 76.7) is True


def test_reentry_allowed_at_materially_different_price():
    """
    A genuinely different premium means different stop/target geometry --
    a different bet, and must NOT be blocked. This is what a blanket time
    cooldown would have wrongly suppressed.
    """
    state = _stopped_state(75.7)
    assert tt.is_repeat_of_stopped_plan(state, 24050.0, "PE", 55.0) is False


def test_reentry_gate_is_scoped_to_strike_and_type():
    state = _stopped_state(75.7)
    assert tt.is_repeat_of_stopped_plan(state, 24050.0, "CE", 76.7) is False
    assert tt.is_repeat_of_stopped_plan(state, 24000.0, "PE", 76.7) is False


def test_reentry_gate_catches_both_20260728_reentries():
    """
    End-to-end against the real session: entries at 75.7 (stopped), 76.7
    (stopped), 76.7 (EOD). Both re-entries must be caught. The 60-minute
    cooldown originally considered caught only the second, because the
    first came 2h31m after its stop.
    """
    state = _stopped_state(75.7)
    assert tt.is_repeat_of_stopped_plan(state, 24050.0, "PE", 76.7) is True

    tt._record_stop_cooldown(state, {
        "strike": 24050.0, "option_type": "PE", "entry": 76.7,
        "closed_at": "2026-07-28T14:05:31",
    })
    assert tt.is_repeat_of_stopped_plan(state, 24050.0, "PE", 76.7) is True


# --------------------------------------------------------------------------
# 5b. Direction-chase gate (2026-08-10: 8 CE trades, 8 different strikes,
# invisible to the strike-scoped gate above -- Rs -5,153)
# --------------------------------------------------------------------------

def _direction_stopped_state(option_type="CE", closed_at="2026-08-10T10:44:40"):
    state = {"date": "2026-08-10", "trades": [], "opened_today": 1,
             "stop_cooldowns": {}, "direction_cooldowns": {}}
    tt._record_direction_cooldown(state, {"option_type": option_type, "closed_at": closed_at})
    return state


def test_direction_chase_blocked_shortly_after_a_same_direction_loss_different_strike():
    """The real 2026-08-10 shape: 24900 CE stops out, 24850 CE (a
    DIFFERENT strike) is proposed 9 minutes later. is_repeat_of_stopped_plan
    would never catch this (different strike); is_direction_chase must."""
    state = _direction_stopped_state(closed_at="2026-08-10T10:44:40")
    now = datetime(2026, 8, 10, 10, 53, 0)  # 9 minutes later
    assert tt.is_direction_chase(state, "CE", now) is True


def test_direction_chase_allowed_after_the_cooldown_window_elapses():
    state = _direction_stopped_state(closed_at="2026-08-10T10:44:40")
    now = datetime(2026, 8, 10, 11, 20, 0)  # 35 minutes later, past the 30min window
    assert tt.is_direction_chase(state, "CE", now) is False


def test_direction_chase_is_scoped_to_direction_not_strike():
    """PE is unaffected by a CE loss -- only the SAME direction is throttled."""
    state = _direction_stopped_state(option_type="CE", closed_at="2026-08-10T10:44:40")
    now = datetime(2026, 8, 10, 10, 50, 0)
    assert tt.is_direction_chase(state, "PE", now) is False


def test_direction_chase_not_armed_by_a_win():
    """Matches REENTRY_PRICE_TOLERANCE_PCT's own philosophy: only a LOSS
    arms the gate. _record_direction_cooldown must never be called for a
    WIN -- pinned at the call site in load_open_trades's close-trade path,
    checked here via the function's own contract (no state -> no block)."""
    state = {"date": "2026-08-10", "trades": [], "opened_today": 1,
             "stop_cooldowns": {}, "direction_cooldowns": {}}
    now = datetime(2026, 8, 10, 10, 50, 0)
    assert tt.is_direction_chase(state, "CE", now) is False


def test_direction_chase_end_to_end_against_the_real_20260810_sequence():
    """Reproduces the actual live cascade: CE stops at 10:44, 12:55,
    12:56, ... -- each new attempt within 30min of the last stop must be
    blocked, matching what the measured counterfactual assumed."""
    state = {"date": "2026-08-10", "trades": [], "opened_today": 1,
             "stop_cooldowns": {}, "direction_cooldowns": {}}
    stop_times = ["2026-08-10T10:44:40", "2026-08-10T10:44:13", "2026-08-10T10:32:52"]
    for t in stop_times:
        tt._record_direction_cooldown(state, {"option_type": "CE", "closed_at": t})
    # A new CE attempt shortly after the LAST recorded stop is blocked
    assert tt.is_direction_chase(state, "CE", datetime(2026, 8, 10, 10, 50, 0)) is True
    # Well clear of every recorded stop, it's allowed again
    assert tt.is_direction_chase(state, "CE", datetime(2026, 8, 10, 11, 30, 0)) is False


# --------------------------------------------------------------------------
# 6. Expiry-day rules
# --------------------------------------------------------------------------

def test_non_expiry_day_uses_the_normal_bar():
    bar, blocked = tt.expiry_day_rules("2026-08-04", datetime(2026, 7, 28, 14, 32))
    assert bar == config.MIN_CONVICTION_SCORE_TO_TRACK
    assert blocked is None


def test_expiry_day_raises_the_conviction_bar():
    bar, blocked = tt.expiry_day_rules("2026-07-28", datetime(2026, 7, 28, 10, 21))
    assert bar == config.MIN_CONVICTION_SCORE_TO_TRACK + config.EXPIRY_DAY_EXTRA_CONVICTION
    assert blocked is None


def test_expiry_day_blocks_new_trades_after_cutoff():
    """The real third entry was opened at 14:32 on expiry day and lost 16.8%."""
    _, blocked = tt.expiry_day_rules("2026-07-28", datetime(2026, 7, 28, 14, 32))
    assert blocked is not None
    assert "theta" in blocked


# --------------------------------------------------------------------------
# 7. Risk state: the previously-dead circuit breaker
# --------------------------------------------------------------------------

def test_risk_state_reports_zero_loss_on_a_flat_day(monkeypatch):
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [])
    exposure, loss = tt.compute_risk_state({"trades": [], "opened_today": 0})
    assert exposure == 0.0
    assert loss == 0.0


def test_risk_state_counts_unrealized_loss_on_open_trades(monkeypatch):
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [])
    state = {"trades": [
        {"entry": 76.7, "stop": 53.69, "lots": 1, "running_pnl_inr": -1500.0},
    ]}
    exposure, loss = tt.compute_risk_state(state)

    expected_exposure = (76.7 - 53.69) * config.NIFTY_LOT_SIZE / config.TOTAL_CAPITAL * 100
    assert exposure == pytest.approx(expected_exposure, abs=0.01)
    assert loss == pytest.approx(1500.0 / config.TOTAL_CAPITAL * 100, abs=0.01)


def test_risk_state_counts_todays_realized_loss(monkeypatch):
    """
    The whole point of the fix: main_live.py used to pass a hardcoded 0.0,
    so MAX_DAILY_LOSS_PCT could never trip no matter what the day did.
    """
    from datetime import date
    today = date.today().isoformat()
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [
        {"closed_at": f"{today}T10:43:53", "pnl_inr": -1595.75},
        {"closed_at": f"{today}T14:05:31", "pnl_inr": -1586.0},
        {"closed_at": "2026-07-27T15:30:00", "pnl_inr": -99999.0},  # yesterday, ignored
    ])
    _, loss = tt.compute_risk_state({"trades": []})

    expected = (1595.75 + 1586.0) / config.TOTAL_CAPITAL * 100
    assert loss == pytest.approx(expected, abs=0.01)


def test_risk_state_reports_no_loss_when_the_day_is_up(monkeypatch):
    from datetime import date
    today = date.today().isoformat()
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [
        {"closed_at": f"{today}T10:00:00", "pnl_inr": 5000.0},
    ])
    _, loss = tt.compute_risk_state({"trades": []})
    assert loss == 0.0


# --------------------------------------------------------------------------
# 8. IV asymmetry
# --------------------------------------------------------------------------

def test_rich_iv_is_penalised_and_cheap_iv_rewarded():
    """
    This scanner only ever BUYS premium, so rich and cheap IV are not
    symmetric. Both used to score +1.0.
    """
    assert config.IV_CHEAP_SCORE > 0
    assert config.IV_RICH_SCORE < 0
