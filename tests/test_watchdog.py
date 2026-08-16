"""
watchdog.py's decision_log staleness check -- added 2026-08-16 after
discovering decision_log.jsonl had silently stopped growing for over two
weeks while nifty_scan_*.log (what the original scan-log check watches)
kept looking completely healthy. See watchdog.py's own module docstring
near DECISION_LOG_PATHS for the full incident.

Run: python -m pytest tests/test_watchdog.py -q
"""
import json
from datetime import datetime, timedelta

import watchdog as wd


def _write_lines(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_last_line_timestamp_missing_file(tmp_path):
    assert wd._last_line_timestamp(tmp_path / "nope.jsonl") is None


def test_last_line_timestamp_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert wd._last_line_timestamp(path) is None


def test_last_line_timestamp_reads_last_record(tmp_path):
    path = tmp_path / "d.jsonl"
    now = datetime(2026, 8, 16, 12, 0, 0)
    _write_lines(path, [
        {"timestamp": (now - timedelta(minutes=5)).isoformat()},
        {"timestamp": now.isoformat()},
    ])
    assert wd._last_line_timestamp(path) == now


def test_last_line_timestamp_handles_lines_larger_than_initial_chunk(tmp_path):
    """A single cycle record (5 candidates, full plan/context) can run past
    64KB. The tail-read must grow past its initial chunk rather than
    returning a truncated/malformed partial line."""
    path = tmp_path / "d.jsonl"
    now = datetime(2026, 8, 16, 12, 0, 0)
    padding = "x" * 200_000
    _write_lines(path, [
        {"timestamp": (now - timedelta(minutes=1)).isoformat(), "pad": padding},
        {"timestamp": now.isoformat(), "pad": padding},
    ])
    assert wd._last_line_timestamp(path) == now


def test_last_line_timestamp_malformed_last_line_returns_none(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text('{"timestamp": "2026-08-16T12:00:00"}\nnot valid json\n')
    assert wd._last_line_timestamp(path) is None


def test_decision_log_check_market_closed(monkeypatch):
    monkeypatch.setattr(wd, "market_is_open", lambda now=None: False)
    assert "market closed" in wd.decision_log_check_once()


def test_decision_log_check_fresh_reports_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "market_is_open", lambda now=None: True)
    fresh = tmp_path / "fresh.jsonl"
    _write_lines(fresh, [{"timestamp": datetime.now().isoformat()}])
    monkeypatch.setattr(wd, "DECISION_LOG_PATHS", (("NIFTY", fresh),))
    result = wd.decision_log_check_once()
    assert "WARNING" not in result
    assert "OK" in result


def test_decision_log_check_stale_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "market_is_open", lambda now=None: True)
    stale = tmp_path / "stale.jsonl"
    old_ts = datetime.now() - timedelta(seconds=wd.STALE_THRESHOLD_SECONDS + 60)
    _write_lines(stale, [{"timestamp": old_ts.isoformat()}])
    monkeypatch.setattr(wd, "DECISION_LOG_PATHS", (("NIFTY", stale),))
    result = wd.decision_log_check_once()
    assert "WARNING" in result
    assert "NIFTY" in result


def test_decision_log_check_missing_file_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "market_is_open", lambda now=None: True)
    monkeypatch.setattr(wd, "DECISION_LOG_PATHS", (("Bank Nifty", tmp_path / "absent.jsonl"),))
    result = wd.decision_log_check_once()
    assert "WARNING" in result
    assert "Bank Nifty" in result


def test_decision_log_check_covers_both_indices_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "market_is_open", lambda now=None: True)
    fresh = tmp_path / "fresh.jsonl"
    stale = tmp_path / "stale.jsonl"
    _write_lines(fresh, [{"timestamp": datetime.now().isoformat()}])
    old_ts = datetime.now() - timedelta(seconds=wd.STALE_THRESHOLD_SECONDS + 60)
    _write_lines(stale, [{"timestamp": old_ts.isoformat()}])
    monkeypatch.setattr(wd, "DECISION_LOG_PATHS", (("NIFTY", fresh), ("Bank Nifty", stale)))
    result = wd.decision_log_check_once()
    assert "NIFTY decision_log OK" in result
    assert "WARNING" in result and "Bank Nifty" in result
