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

    position["status"] = "EXPIRED" if reason == "expiry_settlement" else "CLOSED"
    position["closed_at"] = datetime.now().isoformat(timespec="seconds")
    position["close_reason"] = reason
    position["pnl_inr"] = pnl_inr
    position["pnl_pct_of_max_profit"] = (
        round(pnl_inr / plan_dict["max_profit_inr"] * 100, 1) if plan_dict["max_profit_inr"] else None
    )

    _append_journal(position)
    state["position"] = None
    save_position(state)
    return position
