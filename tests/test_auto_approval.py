"""
Tests for trade_staging.stage_and_maybe_auto_open() -- shared by
main_condor.py and main_directional_spread.py.

Confirms both branches: auto_approve=True opens immediately (still via
the SAME staged audit record, now EXECUTED); auto_approve=False leaves
it PENDING for approve_orders.py, exactly as before. In neither branch
does anything resembling a broker order get placed -- open_fn is always
the calling strategy's own tracker.open_position, a purely local state
write.

Run: python -m pytest tests/ -q
"""

import json

import pytest

import trade_staging as staging


@pytest.fixture
def staged_path(tmp_path, monkeypatch):
    p = tmp_path / "staged_orders.json"
    monkeypatch.setattr(staging, "STAGED_ORDERS_PATH", p)
    return p


def test_auto_approve_true_opens_immediately(staged_path):
    opened = []
    record = staging.stage_and_maybe_auto_open(
        kind="condor_open_candidate", detail="sell 24000CE/23800PE", note="expiry 2026-08-04",
        data={"short_ce_strike": 24000.0}, auto_approve=True,
        open_fn=lambda a, b: opened.append((a, b)), open_args=("state", "plan"),
    )
    assert record["status"] == "EXECUTED"
    assert opened == [("state", "plan")]


def test_auto_approve_false_leaves_pending_and_does_not_open(staged_path):
    opened = []
    record = staging.stage_and_maybe_auto_open(
        kind="condor_open_candidate", detail="sell 24000CE/23800PE", note="expiry 2026-08-04",
        data={"short_ce_strike": 24000.0}, auto_approve=False,
        open_fn=lambda a, b: opened.append((a, b)), open_args=("state", "plan"),
    )
    assert record["status"] == "PENDING"
    assert opened == []


def test_auto_approve_still_writes_the_audit_record(staged_path):
    """Both branches must be visible in the same staged-orders log."""
    staging.stage_and_maybe_auto_open(
        kind="directional_spread_open_candidate", detail="x", note="y",
        data={}, auto_approve=True, open_fn=lambda: None,
    )
    records = json.loads(staged_path.read_text())
    assert len(records) == 1
    assert records[0]["kind"] == "directional_spread_open_candidate"
    assert records[0]["status"] == "EXECUTED"


def test_auto_approve_broker_order_id_marked_auto_not_a_real_order(staged_path):
    """
    broker_order_id="auto" is a marker, not a real broker interaction --
    nothing in this path calls Dhan's order-placement API.
    """
    record = staging.stage_and_maybe_auto_open(
        kind="condor_open_candidate", detail="x", note="y", data={},
        auto_approve=True, open_fn=lambda: None,
    )
    assert record["broker_order_id"] == "auto"


def test_manual_flow_still_works_after_auto_approve_false(staged_path):
    """Falling back to the old manual approve_orders.py flow must still work unchanged."""
    record = staging.stage_and_maybe_auto_open(
        kind="condor_open_candidate", detail="x", note="y", data={},
        auto_approve=False, open_fn=lambda: None,
    )
    approved = staging.approve(record["id"], note="manually approved")
    assert approved["status"] == "APPROVED"
    executed = staging.mark_executed(record["id"], broker_order_id="manual")
    assert executed["status"] == "EXECUTED"
