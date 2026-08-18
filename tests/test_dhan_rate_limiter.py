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
    monkeypatch.setattr(drl, "LTP_LOCK_PATH", tmp_path / "dhan_rate_limiter_ltp.lock")
    monkeypatch.setattr(drl, "LTP_STATE_PATH", tmp_path / "dhan_rate_limiter_ltp.json")
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

    assert drl._try_acquire_lock(drl.LOCK_PATH) is False


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

    assert drl._try_acquire_lock(drl.LOCK_PATH) is False


# --------------------------------------------------------------------------
# Contention fallback: the 2026-08-17 thundering-herd fix
# --------------------------------------------------------------------------

def test_contended_lock_still_spaces_the_request(monkeypatch):
    """The core fix. Before 2026-08-17 a lock timeout meant `return` --
    rate limiting abandoned entirely, so every process fired at once. It
    must now still space the request, even without the lock."""
    monkeypatch.setattr(drl, "MAX_ACQUIRE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(drl, "MIN_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(drl, "_try_acquire_lock", lambda *a, **kw: False)   # permanently contended
    drl.STATE_PATH.write_text(json.dumps({"last_request_at": time.time()}))

    started = time.monotonic()
    drl.wait_for_slot()
    elapsed = time.monotonic() - started

    # Waited out the interval rather than returning immediately. Lower
    # bound only: jitter adds an unpredictable amount on top.
    assert elapsed >= 0.2


def test_contended_fallback_records_its_request(monkeypatch):
    """A request that goes out via the fallback path must still update
    last_request_at -- otherwise the NEXT caller thinks the account has
    been idle and fires immediately."""
    monkeypatch.setattr(drl, "MAX_ACQUIRE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(drl, "MIN_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(drl, "_try_acquire_lock", lambda *a, **kw: False)
    drl.STATE_PATH.write_text(json.dumps({"last_request_at": 0.0}))

    drl.wait_for_slot()

    recorded = json.loads(drl.STATE_PATH.read_text())["last_request_at"]
    assert recorded > 0.0


def test_contended_fallback_survives_an_unwritable_state_file(monkeypatch):
    """Recording is observability -- it must never block the request."""
    monkeypatch.setattr(drl, "MAX_ACQUIRE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(drl, "MIN_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(drl, "_try_acquire_lock", lambda *a, **kw: False)

    class _Unwritable(type(drl.STATE_PATH)):
        def write_text(self, *a, **kw):
            raise OSError("disk full")

    monkeypatch.setattr(drl, "STATE_PATH", _Unwritable(drl.STATE_PATH))
    drl.wait_for_slot()   # must not raise


def test_real_concurrent_processes_are_actually_spaced(monkeypatch):
    """No mocking of the lock: run many threads through the real
    acquire/fallback paths at once -- the shape of 11 live processes
    competing -- and measure the ACTUAL spacing between the request times
    they record. The old code produced a stampede here; this asserts the
    result is spread out, not simultaneous.
    """
    import threading

    monkeypatch.setattr(drl, "MIN_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(drl, "MAX_ACQUIRE_WAIT_SECONDS", 0.3)
    drl.STATE_PATH.write_text(json.dumps({"last_request_at": 0.0}))

    fire_times = []
    lock = threading.Lock()

    def _one_request():
        drl.wait_for_slot()
        with lock:
            fire_times.append(time.monotonic())

    threads = [threading.Thread(target=_one_request) for _ in range(11)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    span = time.monotonic() - start

    assert len(fire_times) == 11
    # 11 requests at a 0.1s floor cannot legitimately complete instantly.
    # The old "proceed without it" path finished all 11 in ~0.3s (just the
    # acquire timeout); real spacing has to take meaningfully longer.
    assert span >= 0.5, f"11 requests completed in {span:.2f}s -- not actually spaced"


# --------------------------------------------------------------------------
# Two independent budgets: /optionchain vs /marketfeed/ltp (2026-08-18)
# --------------------------------------------------------------------------

def test_wait_for_slot_defaults_are_unaffected_by_the_new_parameters():
    """Every existing caller (option-chain fetches) invokes wait_for_slot()
    with zero or one (min_interval) argument. Confirms the new lock_path/
    state_path parameters default to the original files rather than
    silently requiring callers to be updated."""
    drl.wait_for_slot(min_interval=0.05)
    assert drl.STATE_PATH.exists()
    assert not drl.LTP_STATE_PATH.exists()


def test_wait_for_ltp_slot_uses_the_separate_ltp_files():
    drl.wait_for_ltp_slot()
    assert drl.LTP_STATE_PATH.exists()
    assert not drl.STATE_PATH.exists()


def test_chain_and_ltp_budgets_do_not_block_each_other(monkeypatch):
    """The whole point of splitting the budgets: a slow /optionchain
    interval must not force /marketfeed/ltp calls to wait it out, and
    vice versa. Set MIN_INTERVAL_SECONDS (chain) far above
    LTP_MIN_INTERVAL_SECONDS and confirm an LTP call right after a chain
    call doesn't inherit the chain's wait."""
    monkeypatch.setattr(drl, "MIN_INTERVAL_SECONDS", 5.0)
    monkeypatch.setattr(drl, "LTP_MIN_INTERVAL_SECONDS", 0.05)

    drl.wait_for_slot()  # chain call, records into STATE_PATH

    start = time.time()
    drl.wait_for_ltp_slot()  # must not wait out the chain's 5s interval
    assert time.time() - start < 1.0


def test_ltp_lock_contention_falls_back_independently_of_chain_lock(monkeypatch):
    """Confirms the LTP path exercises the same hardened contention
    handling (OSError-tolerant acquire, unlocked fallback spacing) as the
    chain path, coordinated through its own lock file and state file."""
    monkeypatch.setattr(drl, "MAX_ACQUIRE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(drl, "LTP_MIN_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(drl, "_try_acquire_lock", lambda *a, **kw: False)   # permanently contended
    drl.LTP_STATE_PATH.write_text(json.dumps({"last_request_at": time.time()}))

    started = time.monotonic()
    drl.wait_for_ltp_slot()   # must proceed via the unlocked fallback, not hang or raise
    elapsed = time.monotonic() - started

    assert elapsed >= 0.2
    assert json.loads(drl.LTP_STATE_PATH.read_text())["last_request_at"] > 0.0
