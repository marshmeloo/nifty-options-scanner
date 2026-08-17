"""
atomic_state.atomic_write_json -- the shared state-write helper.

Hardened 2026-08-17 after a real session logged 15 "WinError 5: Access
is denied" failures replacing state/orderflow.json while strategy
processes were reading it. See that module's own docstring for the full
Windows-contention explanation.

Run: python -m pytest tests/test_atomic_state.py -q
"""
import json
import os
from pathlib import Path

import pytest

import atomic_state


def test_writes_and_reads_back(tmp_path):
    path = tmp_path / "state.json"
    atomic_state.atomic_write_json(path, {"a": 1, "b": [2, 3]})
    assert json.loads(path.read_text()) == {"a": 1, "b": [2, 3]}


def test_passes_json_kwargs_through(tmp_path):
    path = tmp_path / "state.json"
    atomic_state.atomic_write_json(path, {"a": 1}, indent=2)
    assert "\n" in path.read_text()   # indent actually applied


def test_overwrites_existing_file_completely(tmp_path):
    path = tmp_path / "state.json"
    atomic_state.atomic_write_json(path, {"old": "much longer content here"})
    atomic_state.atomic_write_json(path, {"new": 1})
    assert json.loads(path.read_text()) == {"new": 1}


def test_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "state.json"
    atomic_state.atomic_write_json(path, {"a": 1})
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_temp_filename_is_process_unique(tmp_path, monkeypatch):
    """Two writers of the same path must never share a temp file. Verified
    by capturing the temp name the writer actually uses."""
    seen = {}
    real_replace = os.replace

    def _capture(src, dst):
        seen["tmp"] = str(src)
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_state.os, "replace", _capture)
    atomic_state.atomic_write_json(tmp_path / "state.json", {"a": 1})

    assert str(os.getpid()) in seen["tmp"]


def test_retries_a_blocked_replace_then_succeeds(tmp_path, monkeypatch):
    """The real Windows case: the destination is briefly open in another
    process, so the first replace raises PermissionError and a moment
    later it succeeds. The write must land, not be silently dropped."""
    path = tmp_path / "state.json"
    real_replace = os.replace
    calls = {"n": 0}

    def _blocked_twice(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_state.os, "replace", _blocked_twice)
    atomic_state.atomic_write_json(path, {"a": 1})

    assert calls["n"] == 3
    assert json.loads(path.read_text()) == {"a": 1}


def test_gives_up_after_the_deadline_and_cleans_up(tmp_path, monkeypatch):
    """A permanently blocked replace must raise (callers that treat state
    writes as observability catch it and continue) and must NOT leave a
    temp file accumulating on every retry."""
    path = tmp_path / "state.json"

    def _always_blocked(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(atomic_state.os, "replace", _always_blocked)
    monkeypatch.setattr(atomic_state, "REPLACE_RETRY_SECONDS", 0.05)

    with pytest.raises(PermissionError):
        atomic_state.atomic_write_json(path, {"a": 1})

    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_original_file_survives_a_failed_write(tmp_path, monkeypatch):
    """The core atomicity guarantee, unchanged by the retry logic: a write
    that never lands leaves the PREVIOUS content fully intact, never a
    half-written file."""
    path = tmp_path / "state.json"
    atomic_state.atomic_write_json(path, {"original": True})

    def _always_blocked(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(atomic_state.os, "replace", _always_blocked)
    monkeypatch.setattr(atomic_state, "REPLACE_RETRY_SECONDS", 0.05)

    with pytest.raises(PermissionError):
        atomic_state.atomic_write_json(path, {"new": True})

    assert json.loads(path.read_text()) == {"original": True}


def test_real_transient_readers_do_not_lose_the_write(tmp_path):
    """No mocking, real threads, modelling the ACTUAL reader pattern.

    Every reader of these files uses `path.read_text()` (see
    orderflow.load_state, dashboard_server._read_json, etc.) -- open,
    read, close, all within microseconds. That's what the retry loop is
    sized for: each individual handle is gone almost immediately, so a
    blocked replace clears on the next attempt. Hammering the file with
    transient reads while writing must not lose the write.
    """
    import threading
    import time as _time

    path = tmp_path / "state.json"
    atomic_state.atomic_write_json(path, {"v": 1})

    stop = threading.Event()
    reads = {"n": 0}

    def _poll_reads():
        # Sleep between reads, like a real reader on a poll cycle. Without
        # this the file is open ~100% of the time and NO retry-based
        # approach can ever win -- that pathological case is covered by
        # test_a_reader_holding_the_file_open_longer_than_the_deadline.
        while not stop.is_set():
            try:
                path.read_text()
                reads["n"] += 1
            except OSError:
                pass   # mid-replace, exactly what real readers tolerate
            _time.sleep(0.005)

    readers = [threading.Thread(target=_poll_reads) for _ in range(3)]
    for r in readers:
        r.start()
    try:
        for i in range(2, 12):
            atomic_state.atomic_write_json(path, {"v": i})
            _time.sleep(0.005)
    finally:
        stop.set()
        for r in readers:
            r.join()

    assert reads["n"] > 0, "readers never actually read -- test proves nothing"
    assert json.loads(path.read_text()) == {"v": 11}


def test_a_reader_holding_the_file_open_longer_than_the_deadline_still_fails(tmp_path, monkeypatch):
    """The honest limit of this fix, pinned so it isn't mistaken for a
    full guarantee.

    On Windows, os.replace is blocked for as long as ANY handle is open
    on the destination -- retrying cannot beat a reader that holds it
    open longer than REPLACE_RETRY_SECONDS. No reader in this project
    does that today (all use read_text()), but if one is ever added,
    this is the failure it will produce, and the caller must handle it.
    Skipped on POSIX, where rename over an open file is legal and this
    contention doesn't exist at all.
    """
    if os.name != "nt":
        pytest.skip("Windows-specific: POSIX allows rename over an open file")

    path = tmp_path / "state.json"
    atomic_state.atomic_write_json(path, {"original": True})
    monkeypatch.setattr(atomic_state, "REPLACE_RETRY_SECONDS", 0.05)

    with open(path, "r") as held_open_throughout:
        held_open_throughout.read()
        with pytest.raises(PermissionError):
            atomic_state.atomic_write_json(path, {"new": True})

    # Previous content still intact -- the atomicity guarantee holds even
    # when the write cannot land.
    assert json.loads(path.read_text()) == {"original": True}
