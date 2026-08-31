"""
The test suite must never write to the project's REAL state/ directory.

Found 2026-08-31: running the suite appended records to the live
state/staged_orders.json (485 -> 488 in one run). Production had built up
99 PENDING "Notices", 64 of them `condor_*` -- from a strategy removed
from live automation on 2026-08-26, so nothing running could have made
them. The dashboard displayed that backlog as real trading activity.

The autouse fixture in conftest.py fixes the cause; this pins it.

Run: python -m pytest tests/ -q
"""

import json
from pathlib import Path

import pytest

import trade_staging as staging

REAL_STATE = Path(__file__).parent.parent / "state"


def test_staging_path_is_redirected_away_from_real_state():
    """The autouse fixture must have moved it out of state/ entirely."""
    assert REAL_STATE not in Path(staging.STAGED_ORDERS_PATH).parents, (
        f"trade_staging is still pointed at real state: {staging.STAGED_ORDERS_PATH}")


def test_staging_writes_do_not_touch_the_real_file():
    """Stage something and prove the real file is untouched."""
    real = REAL_STATE / "staged_orders.json"
    before = real.read_text(encoding="utf-8") if real.exists() else None

    staging.stage_advisory("test_kind", "a detail", note="from the test suite")

    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert after == before, "a staged advisory leaked into the real state file"
    # and it did land somewhere -- the fixture's tmp file
    assert any(r["kind"] == "test_kind" for r in staging.list_staged())


def test_each_test_gets_a_fresh_staging_file():
    """tmp_path is per-test, so no record from the test above survives."""
    assert not any(r["kind"] == "test_kind" for r in staging.list_staged())
