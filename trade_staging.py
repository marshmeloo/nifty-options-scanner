"""
Staged-order approval gate -- a PLACEHOLDER for if/when this project ever
moves from decision-support to actual order placement.

Nothing in this file talks to a broker. There is no execution wiring here
at all, on purpose. What it gives you instead is the pattern popularized
as "Trading as Git" (stage -> review -> approve -> push, the same shape
as a code change): every proposed order gets written as a PENDING record
that a human has to explicitly approve or reject before anything could
act on it. If you add real execution later, the execution layer should
only ever read APPROVED records and mark them EXECUTED -- it should never
have a path that skips staging.

Why this exists as its own file, not folded into trade_tracker.py:
trade_tracker.py journals trades that are ALREADY being tracked (post
decision). This module is upstream of that -- it's the gate between "the
pipeline recommends this" and "a human has agreed to act on it." Keeping
it separate means the gate can't accidentally get bypassed by something
that only imports trade_tracker.

Currently NOT wired into main_live.py. main_live.py still only prints
recommendations, same as today -- wiring this in is a deliberate future
step, not something that should silently change what the live loop does
during the current training/evaluation phase.

Usage (once you decide to wire it in somewhere):
    import trade_staging as staging
    record = staging.stage_order(plan, setup, verdict, note="flagged by scanner")

Then, separately, a human reviews and approves/rejects via approve_orders.py
(or by calling staging.approve(order_id) / staging.reject(order_id, reason)
directly). Nothing here ever calls a broker API.
"""

import json
import uuid
from pathlib import Path
from datetime import datetime

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
STAGED_ORDERS_PATH = STATE_DIR / "staged_orders.json"

VALID_STATUSES = ("PENDING", "APPROVED", "REJECTED", "EXECUTED")


def _load() -> list:
    if STAGED_ORDERS_PATH.exists():
        return json.loads(STAGED_ORDERS_PATH.read_text())
    return []


def _save(records: list):
    STAGED_ORDERS_PATH.write_text(json.dumps(records, indent=2))


def stage_order(plan, setup, verdict, note: str = "") -> dict:
    """
    Write a new PENDING staged order from a TradePlan + Setup + RiskVerdict
    (the same objects main_live.py already builds every cycle). Does NOT
    require verdict.decision == "APPROVED" -- staging is just "here's a
    candidate for a human to look at," the risk verdict is one input a
    reviewer should weigh, not a bypass.
    """
    record = {
        "id": uuid.uuid4().hex[:8],
        "staged_at": datetime.now().isoformat(timespec="seconds"),
        "status": "PENDING",
        "symbol": setup.symbol,
        "strike": setup.strike,
        "option_type": setup.option_type,
        "expiry": setup.expiry,
        "entry": plan.entry,
        "target": plan.target,
        "stop": plan.stop,
        "lots": plan.lots,
        "risk_pct_of_capital": plan.risk_pct_of_capital,
        "risk_level": plan.risk_level,
        "score": setup.score,
        "reasons": list(setup.reasons),
        "risk_verdict": verdict.decision,
        "risk_verdict_reasons": list(verdict.reasons),
        "note": note,
        "decided_at": None,
        "decided_by_note": None,
        "broker_order_id": None,   # only ever populated by a FUTURE execution layer, never by this module
    }
    records = _load()
    records.append(record)
    _save(records)
    return record


def list_staged(status: str = None) -> list:
    """List staged orders, optionally filtered to one status (e.g. 'PENDING')."""
    records = _load()
    if status:
        return [r for r in records if r["status"] == status]
    return records


def render_diff(record: dict) -> str:
    """
    Git-diff-style human-readable rendering of one staged order, meant to
    be read before approving -- the whole point of staging is that a
    human actually looks at this, not that it exists as a formality.
    """
    lines = [
        f"  order {record['id']}  [{record['status']}]  staged {record['staged_at']}",
        f"+ BUY {record['lots']} lot(s)  {record['symbol']} {record['strike']} {record['option_type']}  (expiry {record['expiry']})",
        f"+   entry {record['entry']}   target {record['target']}   stop {record['stop']}",
        f"+   risk {record['risk_pct_of_capital']}% of capital ({record['risk_level']})   score {record['score']}",
        f"+   reasons: {', '.join(record['reasons']) if record['reasons'] else '(none)'}",
        f"+   risk check: {record['risk_verdict']} -- {'; '.join(record['risk_verdict_reasons']) if record['risk_verdict_reasons'] else '(no notes)'}",
    ]
    if record.get("note"):
        lines.append(f"+   staging note: {record['note']}")
    if record["status"] != "PENDING":
        lines.append(f"    decided {record['decided_at']}: {record['decided_by_note'] or '(no note)'}")
    return "\n".join(lines)


def approve(order_id: str, note: str = "") -> dict:
    """Mark a staged order APPROVED. Still does not execute anything."""
    return _set_status(order_id, "APPROVED", note)


def reject(order_id: str, note: str = "") -> dict:
    """Mark a staged order REJECTED."""
    return _set_status(order_id, "REJECTED", note)


def mark_executed(order_id: str, broker_order_id: str, note: str = "") -> dict:
    """
    Reserved for a FUTURE execution layer to call after it has actually
    placed an order for an APPROVED record -- and only an APPROVED
    record; this raises if the order isn't APPROVED first, so an
    execution layer can't accidentally fire on something nobody signed
    off on. No such execution layer exists in this project yet.
    """
    records = _load()
    for r in records:
        if r["id"] == order_id:
            if r["status"] != "APPROVED":
                raise ValueError(
                    f"Order {order_id} is {r['status']}, not APPROVED -- refusing to mark executed."
                )
            r["status"] = "EXECUTED"
            r["broker_order_id"] = broker_order_id
            r["decided_by_note"] = note or r["decided_by_note"]
            _save(records)
            return r
    raise KeyError(f"No staged order with id {order_id}")


def _set_status(order_id: str, status: str, note: str) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    records = _load()
    for r in records:
        if r["id"] == order_id:
            r["status"] = status
            r["decided_at"] = datetime.now().isoformat(timespec="seconds")
            r["decided_by_note"] = note
            _save(records)
            return r
    raise KeyError(f"No staged order with id {order_id}")
