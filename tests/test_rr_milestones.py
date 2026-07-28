"""
Tests for R-multiple milestone tracking.

"R" is a trade's own risk unit: 1R = entry - stop. These record which
multiples a trade actually touched while open, and when. Purely
evidence-gathering -- no trading behaviour depends on them.

The point is to be able to answer "what should DEFAULT_TARGET_RR be?"
from data. Notably, the first real answer was counterintuitive: lowering
the target raises hit-rate but LOWERS expectancy, so the tests below pin
the expectancy simulation as carefully as the milestone counting.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import pytest

import config
import trade_tracker as tt


def _trade(entry=100.0, stop=70.0, **overrides):
    """1R = 30 premium points by default."""
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
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# The R arithmetic
# --------------------------------------------------------------------------

def test_risk_unit_is_entry_minus_stop():
    assert tt.risk_unit(_trade(entry=100.0, stop=70.0)) == pytest.approx(30.0)


def test_risk_unit_is_none_when_degenerate():
    """A zero or inverted stop must not cause a divide-by-zero downstream."""
    assert tt.risk_unit(_trade(entry=100.0, stop=100.0)) is None
    assert tt.risk_unit(_trade(entry=100.0, stop=120.0)) is None
    assert tt.risk_unit({"entry": 100.0}) is None


def test_r_multiple_at_key_points():
    t = _trade(entry=100.0, stop=70.0)   # 1R = 30
    assert tt.r_multiple(t, 100.0) == 0.0     # entry
    assert tt.r_multiple(t, 130.0) == 1.0     # +1R
    assert tt.r_multiple(t, 160.0) == 2.0     # +2R, the default target
    assert tt.r_multiple(t, 70.0) == -1.0     # stop is by definition -1R
    assert tt.r_multiple(t, 85.0) == -0.5


def test_r_multiple_none_when_price_unknown():
    assert tt.r_multiple(_trade(), None) is None


# --------------------------------------------------------------------------
# Milestone capture while a trade is open
# --------------------------------------------------------------------------

def test_milestones_record_the_time_they_were_first_hit():
    t = _trade(entry=100.0, stop=70.0)
    ts = datetime(2026, 7, 28, 11, 4, 0)

    tt._update_excursion(t, 137.0, ts)   # +1.23R

    hits = t["rr_milestones_hit"]
    assert set(hits) == {"0.5", "0.8", "1.0", "1.2"}
    assert all(v == ts.isoformat() for v in hits.values())
    assert t["max_r_seen"] == pytest.approx(1.23)


def test_milestone_timestamp_is_not_overwritten_by_later_touches():
    """
    First touch is the interesting moment. Re-touching 1R an hour later
    must not rewrite when it was first reached.
    """
    t = _trade(entry=100.0, stop=70.0)
    first = datetime(2026, 7, 28, 11, 4, 0)
    later = datetime(2026, 7, 28, 14, 30, 0)

    tt._update_excursion(t, 130.0, first)    # +1.0R
    tt._update_excursion(t, 100.0, later)    # back to entry
    tt._update_excursion(t, 131.0, later)    # +1.03R again

    assert t["rr_milestones_hit"]["1.0"] == first.isoformat()


def test_peak_r_survives_a_full_round_trip():
    """
    The exact 2026-07-28 shape: ran up, gave it all back, stopped out.
    The peak must persist -- it's the whole reason this exists.
    """
    t = _trade(entry=100.0, stop=70.0)
    tt._update_excursion(t, 136.0, datetime(2026, 7, 28, 11, 0))   # +1.2R
    tt._update_excursion(t, 69.0, datetime(2026, 7, 28, 14, 5))    # stopped

    assert t["max_r_seen"] == pytest.approx(1.2)
    assert "1.2" in t["rr_milestones_hit"]
    assert t["min_ltp_seen"] == 69.0


def test_no_milestones_for_a_trade_that_never_goes_green():
    t = _trade(entry=100.0, stop=70.0)
    tt._update_excursion(t, 95.0, datetime(2026, 7, 28, 10, 30))
    assert t["rr_milestones_hit"] == {}


def test_degenerate_stop_does_not_crash_excursion_tracking():
    t = _trade(entry=100.0, stop=100.0)
    tt._update_excursion(t, 150.0, datetime(2026, 7, 28, 11, 0))
    assert t["max_ltp_seen"] == 150.0          # still tracked
    assert t["rr_milestones_hit"] == {}        # but no R math attempted


# --------------------------------------------------------------------------
# Close-time summary
# --------------------------------------------------------------------------

def test_summary_reports_targets_that_would_have_won():
    t = _trade(entry=100.0, stop=70.0, max_ltp_seen=136.0, min_ltp_seen=69.0,
               exit_ltp=69.0, pnl_pct=-31.0)
    summary = tt._excursion_summary(t)

    assert summary["max_r_reached"] == pytest.approx(1.2)
    assert summary["r_at_exit"] == pytest.approx(-1.03)
    assert summary["rr_would_have_won_at"] == [0.5, 0.8, 1.0, 1.2]
    assert 2.0 not in summary["rr_would_have_won_at"]


def test_lesson_names_the_targets_that_would_have_won():
    t = _trade(entry=100.0, stop=70.0, max_ltp_seen=136.0, min_ltp_seen=69.0,
               exit_ltp=69.0, pnl_pct=-31.0, reason_tags=["long_buildup"])
    t.update(tt._excursion_summary(t))
    lesson = tt._build_lesson(t, "LOSS")

    assert "+1.20R" in lesson
    assert "would have hit target at" in lesson
    assert "1.2R" in lesson


def test_lesson_says_so_when_no_target_was_ever_in_reach():
    t = _trade(entry=100.0, stop=70.0, max_ltp_seen=101.0, min_ltp_seen=69.0,
               exit_ltp=69.0, pnl_pct=-31.0, reason_tags=["long_buildup"])
    t.update(tt._excursion_summary(t))
    lesson = tt._build_lesson(t, "LOSS")

    assert "no configured target was ever in reach" in lesson


# --------------------------------------------------------------------------
# Aggregate stats -- the actual decision input
# --------------------------------------------------------------------------

def _closed(max_r, exit_r):
    return {"outcome": "LOSS", "max_r_reached": max_r, "r_at_exit": exit_r}


def test_stats_are_empty_before_any_tracked_trades(monkeypatch):
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [])
    assert tt.rr_milestone_stats()["sample"] == 0


def test_untracked_legacy_entries_are_excluded_not_counted_as_zero(monkeypatch):
    """
    Journal entries predating R-tracking have no max_r_reached. Treating
    those as 0R would fabricate a pile of trades that "never went green".
    """
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [
        {"outcome": "LOSS"},                      # legacy, no R data
        _closed(max_r=1.5, exit_r=1.5),
    ])
    assert tt.rr_milestone_stats()["sample"] == 1


def test_milestone_counts_and_expectancy(monkeypatch):
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [
        _closed(max_r=2.5, exit_r=2.0),    # reaches everything up to 2.5
        _closed(max_r=1.0, exit_r=-1.0),   # touched 1R then round-tripped
        _closed(max_r=0.1, exit_r=-1.0),   # never went anywhere
        _closed(max_r=0.6, exit_r=-1.0),
    ])
    stats = tt.rr_milestone_stats()
    by_r = {m["r"]: m for m in stats["milestones"]}

    assert stats["sample"] == 4
    assert by_r[0.5]["reached"] == 3
    assert by_r[1.0]["reached"] == 2
    assert by_r[2.0]["reached"] == 1
    assert by_r[3.0]["reached"] == 0

    # At a 1R target: two trades exit at +1.0, two keep their -1.0 exit.
    assert by_r[1.0]["expectancy_r"] == pytest.approx((1.0 + 1.0 - 1.0 - 1.0) / 4)


def test_expectancy_can_favour_a_HIGHER_target(monkeypatch):
    """
    The counterintuitive result that came out of the real journal, pinned
    so it isn't "fixed" by someone optimising hit-rate.

    The mechanism is truncation: a low target converts near-misses into
    small wins, but it also CAPS the big winners that would otherwise have
    run. Below, the two 3R trades get cut to 0.5R while only one extra
    trade is rescued -- so hit-rate goes up and expectancy goes down.
    """
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [
        _closed(max_r=3.0, exit_r=3.0),
        _closed(max_r=3.0, exit_r=3.0),
        _closed(max_r=0.6, exit_r=-1.0),
    ])
    by_r = {m["r"]: m for m in tt.rr_milestone_stats()["milestones"]}

    assert by_r[0.5]["pct"] > by_r[2.0]["pct"]                      # hits more often...
    assert by_r[2.0]["expectancy_r"] > by_r[0.5]["expectancy_r"]    # ...but earns less


def test_summary_flags_an_all_negative_sample(monkeypatch):
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [
        _closed(max_r=0.2, exit_r=-1.0),
        _closed(max_r=0.3, exit_r=-1.0),
    ])
    text = tt.summarize_rr_milestones()
    assert "points at entry quality" in text
