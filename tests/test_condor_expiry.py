"""
Tests for main_condor.is_day_after_expiry().

Real bug, reported by the user: the condor strategy NEVER opened a
position because this check could structurally never return True. It
compared today's date against `nearest_upcoming_expiry` -- but
get_nearest_expiry() only ever returns an ACTIVE/UPCOMING expiry (Dhan's
API doesn't list past ones), so that date is always today-or-future.
`days_since_expiry = today - expiry_date` was therefore always <= 0, and
the intended `1 <= days_since_expiry <= 3` window could never be hit.

The fix derives the actual PREVIOUS (already-settled) expiry from state:
whenever get_nearest_expiry()'s return value changes, that change itself
means the old value just settled.

Run: python -m pytest tests/ -q
"""

import datetime
import json

import pytest

import main_condor as mc


@pytest.fixture
def tracking_path(tmp_path, monkeypatch):
    p = tmp_path / "condor_expiry_tracking.json"
    monkeypatch.setattr(mc, "EXPIRY_TRACKING_PATH", p)
    return p


def _freeze_today(monkeypatch, y, m, d):
    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, m, d, 10, 0, 0)
    monkeypatch.setattr(mc, "datetime", FrozenDateTime)


def test_old_logic_could_never_return_true():
    """
    Documents the bug directly: nearest_upcoming_expiry is always
    today-or-future, so comparing it straight against today can never
    land in the 1-3 day "day after expiry" window. This is WHY the fix
    exists, not a test of current behaviour.
    """
    today = datetime.date(2026, 7, 29)
    for expiry in ("2026-07-29", "2026-08-04", "2026-08-11"):
        expiry_date = datetime.datetime.strptime(expiry, "%Y-%m-%d").date()
        days_since_expiry = (today - expiry_date).days
        assert not (1 <= days_since_expiry <= 3), (
            "an upcoming expiry can never satisfy the day-after-expiry window "
            "-- this is the exact bug that made it always False"
        )


def test_first_ever_run_returns_false_and_records_state(tracking_path):
    """No prior expiry on record yet -- can't know, so don't misfire."""
    assert mc.is_day_after_expiry("2026-08-04") is False
    state = json.loads(tracking_path.read_text())
    assert state["current_expiry"] == "2026-08-04"
    assert state["previous_expiry"] is None


def test_same_expiry_repeated_stays_false_mid_cycle(tracking_path, monkeypatch):
    """Still in the same weekly cycle -- no rollover, nothing to detect."""
    mc.is_day_after_expiry("2026-08-04")  # first observation
    _freeze_today(monkeypatch, 2026, 8, 1)
    assert mc.is_day_after_expiry("2026-08-04") is False  # unchanged


def test_rollover_detected_the_day_after(tracking_path, monkeypatch):
    """
    The actual scenario the bug was supposed to catch: 2026-08-04
    expired, 2026-08-11 is now the nearest upcoming expiry, and today
    is one day after the old expiry.
    """
    _freeze_today(monkeypatch, 2026, 7, 28)
    mc.is_day_after_expiry("2026-08-04")   # establishes current_expiry

    _freeze_today(monkeypatch, 2026, 8, 5)  # day after 08-04 expired
    assert mc.is_day_after_expiry("2026-08-11") is True

    state = json.loads(tracking_path.read_text())
    assert state["previous_expiry"] == "2026-08-04"
    assert state["current_expiry"] == "2026-08-11"


def test_grace_window_persists_across_multiple_days(tracking_path, monkeypatch):
    """
    If staging fails on the rollover day itself, subsequent days within
    the grace window must still be able to retry -- previous_expiry must
    not be cleared or overwritten until the NEXT actual rollover.
    """
    _freeze_today(monkeypatch, 2026, 7, 28)
    mc.is_day_after_expiry("2026-08-04")

    _freeze_today(monkeypatch, 2026, 8, 5)   # day 1 after expiry
    assert mc.is_day_after_expiry("2026-08-11") is True

    _freeze_today(monkeypatch, 2026, 8, 6)   # day 2, nearest unchanged
    assert mc.is_day_after_expiry("2026-08-11") is True

    _freeze_today(monkeypatch, 2026, 8, 7)   # day 3, still within window
    assert mc.is_day_after_expiry("2026-08-11") is True


def test_window_closes_after_three_days(tracking_path, monkeypatch):
    _freeze_today(monkeypatch, 2026, 7, 28)
    mc.is_day_after_expiry("2026-08-04")

    _freeze_today(monkeypatch, 2026, 8, 5)
    mc.is_day_after_expiry("2026-08-11")

    _freeze_today(monkeypatch, 2026, 8, 8)   # 4 days after 08-04 -- outside window
    assert mc.is_day_after_expiry("2026-08-11") is False


def test_survives_a_long_outage_without_false_positive(tracking_path, monkeypatch):
    """
    If the loop was down for weeks, the reconstructed "previous_expiry"
    on resuming is however-many-weeks-old. days_since_expiry will be
    large, and the window check must correctly stay False rather than
    misfiring just because the observed nearest-expiry value changed.
    """
    _freeze_today(monkeypatch, 2026, 7, 1)
    mc.is_day_after_expiry("2026-07-07")   # last thing observed before the outage

    _freeze_today(monkeypatch, 2026, 7, 29)   # resumes weeks later
    assert mc.is_day_after_expiry("2026-08-04") is False

    state = json.loads(tracking_path.read_text())
    assert state["previous_expiry"] == "2026-07-07"  # stale, but harmless


def test_holiday_shifted_expiry_needs_no_special_handling(tracking_path, monkeypatch):
    """
    The fix never assumes a fixed 7-day cycle -- it just reacts to
    whatever value Dhan's API actually returns, so an irregular gap
    (e.g. a holiday-shifted expiry) works identically to a normal one.
    """
    _freeze_today(monkeypatch, 2026, 7, 28)
    mc.is_day_after_expiry("2026-08-06")   # an unusual, non-Tuesday expiry

    _freeze_today(monkeypatch, 2026, 8, 7)   # day after
    assert mc.is_day_after_expiry("2026-08-13") is True
