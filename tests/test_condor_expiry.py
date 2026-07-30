"""
Tests for main_condor.choose_expiry_to_open().

This REPLACES the old is_day_after_expiry() approach (see the
2026-07-29 README entry for that bug) with something simpler: instead of
only allowing a new condor in a narrow 1-3 day window right after the
previous expiry, it can open on ANY day the position is flat, against
whichever expiry has at least MIN_DAYS_TO_EXPIRY_TO_OPEN days left --
rolling straight to next week's expiry if the nearest one is today or
too close. No state tracking needed; every cycle just asks the expiry
list directly.

Run: python -m pytest tests/ -q
"""

import datetime

import main_condor as mc


def _dates(*strs):
    return list(strs)


def test_picks_nearest_expiry_when_it_qualifies():
    today = datetime.date(2026, 7, 20)
    expiries = _dates("2026-07-23", "2026-07-30")   # 3 days out
    assert mc.choose_expiry_to_open(expiries, min_days_out=1, today=today) == "2026-07-23"


def test_rolls_to_next_expiry_when_nearest_is_today():
    """The exact scenario reported: running the tool ON expiry day itself."""
    today = datetime.date(2026, 7, 23)
    expiries = _dates("2026-07-23", "2026-07-30")   # nearest is TODAY, 0 days out
    assert mc.choose_expiry_to_open(expiries, min_days_out=1, today=today) == "2026-07-30"


def test_rolls_to_next_expiry_when_nearest_is_too_close():
    """Nearest expiry is only 1 day away but min_days_out requires 2."""
    today = datetime.date(2026, 7, 22)
    expiries = _dates("2026-07-23", "2026-07-30")   # 1 day out
    assert mc.choose_expiry_to_open(expiries, min_days_out=2, today=today) == "2026-07-30"


def test_can_open_mid_week_not_just_right_after_previous_expiry():
    """
    The other half of the report: opening should not be restricted to a
    narrow window right after the prior expiry. Any day with enough
    runway on the nearest expiry should qualify.
    """
    today = datetime.date(2026, 7, 21)   # Tuesday, mid-cycle
    expiries = _dates("2026-07-23", "2026-07-30")   # 2 days out
    assert mc.choose_expiry_to_open(expiries, min_days_out=1, today=today) == "2026-07-23"


def test_returns_none_when_nothing_qualifies():
    today = datetime.date(2026, 7, 23)
    expiries = _dates("2026-07-23")   # only option is today itself, 0 days out
    assert mc.choose_expiry_to_open(expiries, min_days_out=1, today=today) is None


def test_holiday_shifted_expiry_needs_no_special_handling():
    """
    Never assumes a fixed 7-day cycle -- just reacts to whatever the
    expiry list actually says, so an irregular gap (holiday-shifted
    expiry) works identically to a normal one.
    """
    today = datetime.date(2026, 7, 20)
    expiries = _dates("2026-07-24", "2026-07-31")   # unusual Friday expiry, 4 days out
    assert mc.choose_expiry_to_open(expiries, min_days_out=1, today=today) == "2026-07-24"


def test_uses_real_today_when_not_supplied():
    """Sanity check that the default `today` parameter actually works."""
    far_future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    assert mc.choose_expiry_to_open([far_future], min_days_out=1) == far_future
