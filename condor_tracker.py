"""
Tracks one open CondorPosition across the week: mark-to-market P&L on
every poll, a breach-warning check that STAGES (doesn't auto-execute) an
early-close candidate via trade_staging.py when spot gets close to
either short strike, and expiry-day settlement.
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import asdict

import config_condor as ccfg
import trade_staging as staging
from models import CondorPosition
from atomic_state import atomic_write_json

log = logging.getLogger("condor")

STATE_PATH = Path(ccfg.STATE_PATH)
JOURNAL_PATH = Path(ccfg.JOURNAL_PATH)
STATE_PATH.parent.mkdir(exist_ok=True)
JOURNAL_PATH.parent.mkdir(exist_ok=True)


def load_position() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"position": None}


def save_position(state: dict):
    atomic_write_json(STATE_PATH, state, default=str, indent=2)


def _append_journal(position: dict):
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(position, default=str) + "\n")


def open_position(state: dict, plan) -> dict:
    position = CondorPosition(plan=plan, opened_at=datetime.now().isoformat(timespec="seconds"))
    state["position"] = asdict(position)
    save_position(state)
    return state["position"]


def _current_leg_prices(chain: list, plan_dict: dict) -> dict:
    """Look up each leg's current LTP in the snapshot chain. Missing legs come back None."""
    lookup = {(q.strike, q.option_type): q for q in chain}

    def _price(strike, opt_type):
        q = lookup.get((strike, opt_type))
        return q.ltp if q else None

    return {
        "short_ce": _price(plan_dict["short_ce_strike"], "CE"),
        "short_pe": _price(plan_dict["short_pe_strike"], "PE"),
        "hedge_ce": _price(plan_dict["hedge_ce_strike"], "CE"),
        "hedge_pe": _price(plan_dict["hedge_pe_strike"], "PE"),
    }


def _mark_to_market_pnl_inr(plan_dict: dict, current_prices: dict) -> float:
    """
    Current cost to close = buy back the two shorts, sell the two hedges,
    at CURRENT prices. P&L = credit originally received - current cost
    to close. Returns None if any leg's current price is unavailable.
    """
    if any(v is None for v in current_prices.values()):
        return None
    lot_size = ccfg.NIFTY_LOT_SIZE
    lots = plan_dict["lots"]
    cost_to_close_now = (current_prices["short_ce"] + current_prices["short_pe"]) - (
        current_prices["hedge_ce"] + current_prices["hedge_pe"]
    )
    return round((plan_dict["net_credit"] - cost_to_close_now) * lot_size * lots, 2)


def check_breach_warning(spot: float, plan_dict: dict) -> str:
    """
    Returns a human-readable warning string if spot is within
    BREACH_WARNING_BUFFER_POINTS of either short strike, else None.
    """
    buffer = ccfg.BREACH_WARNING_BUFFER_POINTS
    if spot >= plan_dict["short_ce_strike"] - buffer:
        return f"Spot ({spot}) is within {buffer} points of the short CE strike ({plan_dict['short_ce_strike']})"
    if spot <= plan_dict["short_pe_strike"] + buffer:
        return f"Spot ({spot}) is within {buffer} points of the short PE strike ({plan_dict['short_pe_strike']})"
    return None


def _update_excursion(position: dict, mtm_pnl: float, plan_dict: dict, timestamp=None):
    """
    Track the running best/worst mark-to-market P&L seen this position,
    and which PROFIT_MILESTONES_PCT levels it has touched and WHEN. Purely
    for analysis -- the condor has NO active profit-target exit (held to
    expiry unless manually closed on a breach warning), so nothing here
    gates a live decision. It exists to build the evidence for whether it
    should: "reached 70% of max profit by Tuesday, then gave it back to a
    Thursday breach" is exactly the pattern that would argue for adding
    one, and there is currently no data trail to see it in. Same
    reasoning and shape as directional_spread_tracker's version.

    Called every cycle a position is evaluated, including the cycle that
    ends up closing it. A cycle with no priceable mtm_pnl (a leg's quote
    missing) is skipped entirely rather than treated as flat -- the exact
    distinction behind the 2026-07-30 condor MTM incident this project
    keeps citing: missing information is not a zero.
    """
    if mtm_pnl is None:
        return

    max_seen = position.get("max_pnl_inr_seen")
    position["max_pnl_inr_seen"] = mtm_pnl if max_seen is None else max(max_seen, mtm_pnl)
    min_seen = position.get("min_pnl_inr_seen")
    position["min_pnl_inr_seen"] = mtm_pnl if min_seen is None else min(min_seen, mtm_pnl)

    max_profit = plan_dict.get("max_profit_inr")
    if not max_profit:
        return

    pct_of_max_profit = mtm_pnl / max_profit * 100
    hits = position.setdefault("profit_milestones_hit", {})
    stamp = (timestamp.isoformat() if hasattr(timestamp, "isoformat")
             else (timestamp or datetime.now().isoformat(timespec="seconds")))
    for milestone in ccfg.PROFIT_MILESTONES_PCT:
        key = str(milestone)
        if key not in hits and pct_of_max_profit >= milestone:
            hits[key] = stamp


def update_position(state: dict, snapshot) -> dict:
    """
    Call every poll while a position is open. Marks P&L to market and,
    on a breach warning, stages an early-close candidate for human review
    (does NOT close automatically -- see config_condor.py's note on why).
    Returns the (possibly updated) position dict.
    """
    position = state.get("position")
    if not position or position["status"] != "OPEN":
        return position

    plan_dict = position["plan"]
    current_prices = _current_leg_prices(snapshot.chain, plan_dict)
    mtm_pnl = _mark_to_market_pnl_inr(plan_dict, current_prices)
    position["current_mtm_pnl_inr"] = mtm_pnl
    _update_excursion(position, mtm_pnl, plan_dict, timestamp=snapshot.timestamp)

    warning = check_breach_warning(snapshot.spot, plan_dict)
    if warning and not position.get("breach_staged_this_week"):
        note = f"Current mark-to-market P&L: Rs {mtm_pnl}" if mtm_pnl is not None else "Mark-to-market P&L unavailable this cycle"
        log.info(f"  CONDOR BREACH WARNING: {warning}. {note}")
        staging.stage_advisory(
            kind="condor_breach_warning",
            detail=warning,
            note=note,
        )
        position["breach_staged_this_week"] = True  # don't re-stage every single poll once flagged

    save_position(state)
    return position


def close_position(state: dict, snapshot, reason: str) -> dict:
    """
    Close (or settle at expiry) the current position. At expiry, chain
    quotes may be gone/zero for OTM legs -- fall back to intrinsic value
    (the only thing that's actually true at settlement) when a live quote
    isn't available.
    """
    position = state["position"]
    plan_dict = position["plan"]
    spot = snapshot.spot

    def _intrinsic(strike, opt_type):
        if opt_type == "CE":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)

    current_prices = _current_leg_prices(snapshot.chain, plan_dict)
    for key, (strike_key, opt_type) in {
        "short_ce": ("short_ce_strike", "CE"),
        "short_pe": ("short_pe_strike", "PE"),
        "hedge_ce": ("hedge_ce_strike", "CE"),
        "hedge_pe": ("hedge_pe_strike", "PE"),
    }.items():
        if current_prices[key] is None:
            current_prices[key] = _intrinsic(plan_dict[strike_key], opt_type)
            position.setdefault("legs_settled_at_intrinsic", []).append(key)

    pnl_inr = _mark_to_market_pnl_inr(plan_dict, current_prices)
    # One last excursion update at the settlement price, so a milestone
    # touched only on the closing cycle isn't missed because the position
    # closes in the same call that would have recorded it.
    _update_excursion(position, pnl_inr, plan_dict, timestamp=snapshot.timestamp)

    position["status"] = "EXPIRED" if reason == "expiry_settlement" else "CLOSED"
    position["closed_at"] = datetime.now().isoformat(timespec="seconds")
    position["close_reason"] = reason
    position["pnl_inr"] = pnl_inr
    position["pnl_pct_of_max_profit"] = (
        round(pnl_inr / plan_dict["max_profit_inr"] * 100, 1) if plan_dict["max_profit_inr"] else None
    )
    position.update(_excursion_summary(position, plan_dict))

    _append_journal(position)
    state["position"] = None
    save_position(state)
    return position


def _excursion_summary(position: dict, plan_dict: dict) -> dict:
    """
    Derived stats computed at close time from the running excursion
    _update_excursion tracked all along. `capture_efficiency_pct` answers
    "did we give back a real move before closing": of the best mark-to-
    market P&L this position ever showed, what fraction did the ACTUAL
    exit capture? Meaningful here even without an active target, since
    the condor is held to expiry and can still be closed early on a
    manually-approved breach.
    """
    max_profit = plan_dict.get("max_profit_inr")
    max_loss = plan_dict.get("max_loss_inr")
    max_seen = position.get("max_pnl_inr_seen")
    min_seen = position.get("min_pnl_inr_seen")
    pnl_inr = position.get("pnl_inr")

    summary = {
        "max_pnl_inr_seen": max_seen,
        "min_pnl_inr_seen": min_seen,
        "profit_milestones_hit": position.get("profit_milestones_hit", {}),
    }
    if max_profit and max_seen is not None:
        max_pct = round(max_seen / max_profit * 100, 1)
        summary["max_pct_of_max_profit"] = max_pct
        summary["would_have_won_at"] = [m for m in ccfg.PROFIT_MILESTONES_PCT if max_pct >= m]
        if pnl_inr is not None and max_seen > 0:
            summary["capture_efficiency_pct"] = round(pnl_inr / max_seen * 100, 1)
    if max_loss and min_seen is not None:
        summary["max_pct_of_max_loss"] = round(-min_seen / max_loss * 100, 1)
    return summary


def _load_recent_journal(limit=None) -> list:
    limit = limit or 500
    if not JOURNAL_PATH.exists():
        return []
    lines = [l for l in JOURNAL_PATH.read_text().strip().split("\n") if l]
    return [json.loads(l) for l in lines[-limit:]]


def profit_milestone_stats(limit=None) -> dict:
    """
    Across closed journal entries: how many condors ever reached each
    configured % of max profit, and what the actual outcomes looked
    like. No live decision reads this yet -- see _update_excursion's
    docstring -- this is the evidence for whether one should exist.
    """
    entries = [
        e for e in _load_recent_journal(limit)
        if e.get("max_pct_of_max_profit") is not None
    ]
    if not entries:
        return {"sample": 0, "milestones": []}

    milestones = []
    for m in ccfg.PROFIT_MILESTONES_PCT:
        reached = sum(1 for e in entries if e["max_pct_of_max_profit"] >= m)
        simulated = []
        for e in entries:
            if e["max_pct_of_max_profit"] >= m:
                simulated.append(m)
            else:
                simulated.append(e.get("pnl_pct_of_max_profit") or 0.0)
        milestones.append({
            "pct": m,
            "reached": reached,
            "hit_rate_pct": round(reached / len(entries) * 100, 1),
            "simulated_avg_pct_of_max_profit": round(sum(simulated) / len(simulated), 1),
        })

    return {"sample": len(entries), "milestones": milestones}


def summarize_profit_milestones(limit=None) -> str:
    """Human-readable version of profit_milestone_stats(), for session startup."""
    stats = profit_milestone_stats(limit)
    if not stats["sample"]:
        return ("Condor profit-milestone history: none yet (tracking starts with "
                "positions opened from now on).")

    lines = [
        f"Condor profit-milestone history ({stats['sample']} closed positions, "
        f"no active target -- held to expiry unless manually closed):"
    ]
    best = max(m["simulated_avg_pct_of_max_profit"] for m in stats["milestones"])
    for m in stats["milestones"]:
        marker = "  [best simulated]" if m["simulated_avg_pct_of_max_profit"] == best else ""
        lines.append(
            f"  {m['pct']:>4}% reached by {m['reached']:>3} ({m['hit_rate_pct']:>5.1f}%), "
            f"simulated avg {m['simulated_avg_pct_of_max_profit']:+.1f}% of max profit{marker}"
        )
    return "\n".join(lines)
