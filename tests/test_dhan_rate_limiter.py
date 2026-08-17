"""
Tests for dhan_rate_limiter.py, the cross-process rate limiter built after
429 storms hit all three live processes on 2026-07-30/07-31 -- see that
module's docstring for the full incident history.

Run: python -m pytest tests/ -q
"""

import json
import time

import pytest

import dhan_rate_limiter as drl


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point the limiter at throwaway lock/state files so tests never touch
    the real shared state used by live processes."""
    monkeypatch.setattr(drl, "LOCK_PATH", tmp_path / "dhan_rate_limiter.lock")
    monkeypatch.setattr(drl, "STATE_PATH", tmp_path / "dhan_rate_limiter.json")
    return tmp_path


def test_first_call_does_not_wait():
    start = time.time()
    drl.wait_for_slot(min_interval=0.2)
    assert time.time() - start < 0.1


def test_second_call_waits_out_remaining_interval():
    drl.wait_for_slot(min_interval=0.2)
    start = time.time()
    drl.wait_for_slot(min_interval=0.2)
    elapsed = time.time() - start
    assert elapsed >= 0.18  # allow a hair of scheduling slack


def test_call_after_interval_has_elapsed_does_not_wait():
    drl.wait_for_slot(min_interval=0.1)
    time.sleep(0.15)
    start = time.time()
    drl.wait_for_slot(min_interval=0.1)
    assert time.time() - start < 0.1


def test_records_last_request_time_across_calls():
    drl.wait_for_slot(min_interval=0.05)
    recorded = json.loads(drl.STATE_PATH.read_text())["last_request_at"]
    assert abs(recorded - time.time()) < 1.0


def test_stale_lock_is_cleared_not_deadlocked():
    drl.LOCK_PATH.write_text("")
    stale_time = time.time() - (drl.STALE_LOCK_SECONDS + 5)
    import os
    os.utime(drl.LOCK_PATH, (stale_time, stale_time))

    start = time.time()
    drl.wait_for_slot(min_interval=0.05)
    # Should clear the stale lock and proceed quickly, not hang for
    # MAX_ACQUIRE_WAIT_SECONDS.
    assert time.time() - start < drl.MAX_ACQUIRE_WAIT_SECONDS


def test_fresh_lock_held_by_another_process_forces_proceed_without_blocking_forever(monkeypatch):
    drl.LOCK_PATH.write_text("")  # fresh lock, not stale
    monkeypatch.setattr(drl, "MAX_ACQUIRE_WAIT_SECONDS", 0.2)

    start = time.time()
    drl.wait_for_slot(min_interval=0.05)
    elapsed = time.time() - start
    assert elapsed < 0.5  # gave up around MAX_ACQUIRE_WAIT_SECONDS, didn't hang


def test_permission_error_on_acquire_is_treated_as_contention_not_a_crash(monkeypatch):
    """The 2026-08-17 live failure, pinned.

    On Windows, O_CREAT|O_EXCL against a path another process is
    concurrently creating/deleting raises PermissionError (WinError 5),
    not FileExistsError. That escaped _try_acquire_lock()'s handler,
    propagated out of wait_for_slot(), and killed
    orderflow_feed_banknifty.py outright at startup -- the Bank Nifty
    order-flow feed never subscribed to a single contract all session.
    "Lock is contended" must never crash a caller.
    """
    def _raise_permission_error(*a, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(drl.os, "open", _raise_permission_error)

    assert drl._try_acquire_lock() is False


def test_wait_for_slot_survives_permission_error_and_proceeds(monkeypatch):
    """End-to-end version of the above: a caller must come out the other
    side, not raise. Proceeding without the lock is the documented
    fallback (see MAX_ACQUIRE_WAIT_SECONDS)."""
    def _raise_permission_error(*a, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(drl.os, "open", _raise_permission_error)
    monkeypatch.setattr(drl, "MAX_ACQUIRE_WAIT_SECONDS", 0.1)

    drl.wait_for_slot()   # must return, not raise


def test_file_exists_error_still_treated_as_contention(monkeypatch):
    """The original POSIX path must keep working unchanged."""
    def _raise_file_exists(*a, **kw):
        raise FileExistsError(17, "File exists")
    monkeypatch.setattr(drl.os, "open", _raise_file_exists)

    assert drl._try_acquire_lock() is False
