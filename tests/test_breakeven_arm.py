"""
Tests for the breakeven-arm exit rule (config.BREAKEVEN_ARM_R) and the
observational shadow tracking that follows a breakeven-closed contract
forward to its original target/stop.

See config.BREAKEVEN_ARM_R's own comment for the backtest evidence that
motivated this. These tests pin the live mechanics: precedence between
WIN / BREAKEVEN_STOP / LOSS, that a real close never re-touches state
once armed, and that the shadow record is purely observational -- it
must never feed back into real P&L, the real journal, or state["trades"].

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import pytest

import config
import trade_tracker as tt
from models import MarketSnapshot, OptionQuote
from json import loads


@pytest.fixture
def journals(tmp_path, monkeypatch):
    """Point both the real and shadow journals at throwaway files."""
    real = tmp_path / "trade_journal.jsonl"
    shadow = tmp_path / "breakeven_shadow_journal.jsonl"
    monkeypatch.setattr(tt, "JOURNAL_PATH", real)
    monkeypatch.setattr(tt, "BREAKEVEN_SHADOW_JOURNAL_PATH", shadow)
    return real, shadow


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    """Every test in this file runs with the rule enabled at 0.5R."""
    monkeypatch.setattr(config, "BREAKEVEN_ARM_R", 0.5)


def _read(path):
    if not path.exists():
        return []
    return [loads(line) for line in path.read_text().strip().split("\n") if line]


def _trade(entry=100.0, stop=70.0, **overrides):
    """1R = 30 premium points; 2R target = 160, matching config.DEFAULT_TARGET_RR."""
    base = {
        "id": "24050.0_PE_test",
        "strike": 24050.0,
        "option_type": "PE",
        "entry": entry,
        "stop": stop,
        "target": entry + (entry - stop) * config.DEFAULT_TARGET_RR,
        "lots": 1,
        "max_ltp_seen": entry,
        "min_ltp_seen": entry,
        "max_r_seen": 0.0,
        "rr_milestones_hit": {},
        "reason_tags": ["long_buildup"],
    }
    base.update(overrides)
    return base


def _quote(strike=24050.0, option_type="PE", ltp=100.0):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-04", strike=strike, option_type=option_type,
        ltp=ltp, oi=50000, oi_change_pct=20.0, volume=5000, iv=12.0, iv_percentile=12.0,
    )


def _snapshot(quote, ts):
    return MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [quote], timestamp=ts)


# --------------------------------------------------------------------------
# Precedence: WIN > BREAKEVEN_STOP > LOSS
# --------------------------------------------------------------------------

def test_unarmed_trade_falls_through_to_original_stop(journals):
    """Never reached 0.5R -- a return to entry is not even a breakeven
    candidate, and the trade rides down to its real stop like before."""
    state = {"trades": [_trade()]}
    snap = _snapshot(_quote(ltp=70.0), datetime(2026, 8, 14, 11, 0))

    closed = tt.update_open_trades(state, snap)

    assert len(closed) == 1
    assert closed[0]["outcome"] == "LOSS"
    assert state.get("breakeven_shadows", []) == []


def test_armed_trade_closes_at_breakeven_not_loss(journals):
    """Runs to +1R, gives it back to entry: must close BREAKEVEN_STOP,
    not ride the rest of the way down to the original -1R stop."""
    state = {"trades": [_trade()]}
    up = _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0))     # +1R, arms it
    tt.update_open_trades(state, up)
    assert state["trades"][0]["max_r_seen"] == pytest.approx(1.0)

    back = _snapshot(_quote(ltp=100.0), datetime(2026, 8, 14, 13, 0))   # back to entry
    closed = tt.update_open_trades(state, back)

    assert len(closed) == 1
    assert closed[0]["outcome"] == "BREAKEVEN_STOP"
    assert closed[0]["pnl_pct"] == pytest.approx(0.0)
    assert state["trades"] == []


def test_win_takes_priority_even_when_armed(journals):
    """An armed trade that goes on to actually hit target must win, not
    be intercepted by the breakeven check."""
    state = {"trades": [_trade()]}
    up = _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0))     # +1R, arms it
    tt.update_open_trades(state, up)

    hit_target = _snapshot(_quote(ltp=160.0), datetime(2026, 8, 14, 13, 0))  # 2R target
    closed = tt.update_open_trades(state, hit_target)

    assert closed[0]["outcome"] == "WIN"
    assert state.get("breakeven_shadows", []) == []


def test_disabling_the_rule_restores_original_behaviour(journals, monkeypatch):
    monkeypatch.setattr(config, "BREAKEVEN_ARM_R", None)
    state = {"trades": [_trade()]}
    up = _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0))     # would have armed
    tt.update_open_trades(state, up)

    back_past_entry = _snapshot(_quote(ltp=70.0), datetime(2026, 8, 14, 13, 0))  # all the way to stop
    closed = tt.update_open_trades(state, back_past_entry)

    assert closed[0]["outcome"] == "LOSS"
    assert state.get("breakeven_shadows", []) == []


def test_breakeven_close_journals_to_the_real_journal(journals):
    real, shadow = journals
    state = {"trades": [_trade()]}
    tt.update_open_trades(state, _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0)))
    tt.update_open_trades(state, _snapshot(_quote(ltp=100.0), datetime(2026, 8, 14, 13, 0)))

    entries = _read(real)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "BREAKEVEN_STOP"
    assert _read(shadow) == []   # seeding the shadow does not journal it yet


def test_lesson_names_the_breakeven_arm_not_eod(journals):
    """Regression pin for the bug caught during review: BREAKEVEN_STOP
    used to fall through to the EOD_CLOSE lesson text ("neither target
    nor stop hit"), which is factually wrong for an armed stop."""
    state = {"trades": [_trade()]}
    tt.update_open_trades(state, _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0)))
    closed = tt.update_open_trades(state, _snapshot(_quote(ltp=100.0), datetime(2026, 8, 14, 13, 0)))

    lesson = closed[0]["lesson"]
    assert "breakeven" in lesson.lower()
    assert "neither target nor stop hit" not in lesson


# --------------------------------------------------------------------------
# Shadow seeding
# --------------------------------------------------------------------------

def test_seeded_shadow_watches_the_original_target_and_stop(journals):
    """The shadow must track the ORIGINAL 2R target/stop, not the
    breakeven exit price -- that's the whole point of tracking it."""
    trade = _trade()
    state = {"trades": [trade]}
    tt.update_open_trades(state, _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0)))
    tt.update_open_trades(state, _snapshot(_quote(ltp=100.0), datetime(2026, 8, 14, 13, 0)))

    shadows = state["breakeven_shadows"]
    assert len(shadows) == 1
    shadow = shadows[0]
    assert shadow["target"] == trade["target"]   # 160.0
    assert shadow["stop"] == trade["stop"]        # 70.0
    assert shadow["strike"] == trade["strike"]
    assert shadow["option_type"] == trade["option_type"]
    assert shadow["status"] == "WATCHING"
    assert shadow["id"] == f"{trade['id']}_shadow"


def test_seeding_a_shadow_does_not_touch_real_pnl_fields(journals):
    """Purely observational: seeding must not mutate the trade that was
    just closed and journaled."""
    state = {"trades": [_trade()]}
    tt.update_open_trades(state, _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0)))
    closed = tt.update_open_trades(state, _snapshot(_quote(ltp=100.0), datetime(2026, 8, 14, 13, 0)))

    assert closed[0]["pnl_inr"] == pytest.approx(0.0, abs=0.01)


# --------------------------------------------------------------------------
# Shadow resolution
# --------------------------------------------------------------------------

def _seeded_state():
    """A state with one already-armed-and-closed trade's shadow in it."""
    state = {"trades": [_trade()]}
    tt.update_open_trades(state, _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 11, 0)))
    tt.update_open_trades(state, _snapshot(_quote(ltp=100.0), datetime(2026, 8, 14, 13, 0)))
    return state


def test_shadow_resolves_would_have_hit_target(journals):
    real, shadow_path = journals
    state = _seeded_state()

    resolved = tt.update_breakeven_shadows(
        state, _snapshot(_quote(ltp=160.0), datetime(2026, 8, 14, 14, 0)))

    assert len(resolved) == 1
    assert resolved[0]["resolution"] == "WOULD_HAVE_HIT_TARGET"
    assert state["breakeven_shadows"] == []
    assert len(_read(shadow_path)) == 1
    assert _read(shadow_path)[0]["resolution"] == "WOULD_HAVE_HIT_TARGET"
    assert len(_read(real)) == 1   # the real journal is untouched by shadow resolution


def test_shadow_resolves_would_have_hit_stop(journals):
    state = _seeded_state()

    resolved = tt.update_breakeven_shadows(
        state, _snapshot(_quote(ltp=70.0), datetime(2026, 8, 14, 14, 0)))

    assert resolved[0]["resolution"] == "WOULD_HAVE_HIT_STOP"
    assert state["breakeven_shadows"] == []


def test_shadow_keeps_watching_between_stop_and_target(journals):
    state = _seeded_state()

    resolved = tt.update_breakeven_shadows(
        state, _snapshot(_quote(ltp=120.0), datetime(2026, 8, 14, 14, 0)))

    assert resolved == []
    assert len(state["breakeven_shadows"]) == 1
    assert state["breakeven_shadows"][0]["current_ltp_after_breakeven"] == 120.0


def test_shadow_survives_a_cycle_with_no_quote(journals):
    """Same reasoning as update_open_trades: a missing quote must not
    silently drop the shadow."""
    state = _seeded_state()
    other_strike_quote = _quote(strike=24100.0, ltp=50.0)   # different contract

    resolved = tt.update_breakeven_shadows(
        state, _snapshot(other_strike_quote, datetime(2026, 8, 14, 14, 0)))

    assert resolved == []
    assert len(state["breakeven_shadows"]) == 1


def test_shadow_tracks_peak_r_after_breakeven(journals):
    state = _seeded_state()
    tt.update_breakeven_shadows(state, _snapshot(_quote(ltp=145.0), datetime(2026, 8, 14, 14, 0)))  # +1.5R
    tt.update_breakeven_shadows(state, _snapshot(_quote(ltp=130.0), datetime(2026, 8, 14, 14, 30)))  # pulls back

    assert state["breakeven_shadows"][0]["max_r_seen_after_breakeven"] == pytest.approx(1.5)


# --------------------------------------------------------------------------
# End-of-day finalization
# --------------------------------------------------------------------------

def test_force_close_breakeven_shadows_finalizes_still_watching(journals):
    real, shadow_path = journals
    state = _seeded_state()

    closed = tt.force_close_breakeven_shadows(
        state, _snapshot(_quote(ltp=120.0), datetime(2026, 8, 14, 15, 30)))

    assert len(closed) == 1
    assert closed[0]["resolution"] == "EOD_STILL_WATCHING"
    assert state["breakeven_shadows"] == []
    assert _read(shadow_path)[0]["resolution"] == "EOD_STILL_WATCHING"


def test_force_close_breakeven_shadows_handles_missing_quote(journals):
    state = _seeded_state()
    other_strike_quote = _quote(strike=24100.0, ltp=50.0)

    closed = tt.force_close_breakeven_shadows(
        state, _snapshot(other_strike_quote, datetime(2026, 8, 14, 15, 30)))

    assert closed[0]["resolved_ltp"] is None
    assert closed[0]["resolution"] == "EOD_STILL_WATCHING"


def test_force_close_is_a_noop_with_no_shadows(journals):
    state = {"trades": [], "breakeven_shadows": []}
    closed = tt.force_close_breakeven_shadows(
        state, _snapshot(_quote(ltp=100.0), datetime(2026, 8, 14, 15, 30)))
    assert closed == []


# --------------------------------------------------------------------------
# load_open_trades() defaulting
# --------------------------------------------------------------------------

def test_load_open_trades_defaults_shadows_key(tmp_path, monkeypatch):
    """A state file written before this feature existed has no
    breakeven_shadows key at all -- must not KeyError downstream."""
    from datetime import date
    path = tmp_path / "open_trades.json"
    path.write_text(f'{{"date": "{date.today().isoformat()}", "trades": [], "opened_today": 0}}')
    monkeypatch.setattr(tt, "OPEN_TRADES_PATH", path)

    state = tt.load_open_trades()
    assert state["breakeven_shadows"] == []
