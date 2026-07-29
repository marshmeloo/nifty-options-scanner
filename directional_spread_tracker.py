"""
Tracks one open DirectionalSpreadPosition: mark-to-market P&L every
poll, a profit-target check and a stop-loss check (this position is
ACTIVELY managed, unlike the condor which runs to expiry unless
breached), a breach-warning check that STAGES (doesn't auto-execute) an
early-close candidate, and expiry-day settlement as a last resort.

Also owns the daily "how many new positions opened today" counter, reset
at the start of each calendar day, since config_directional_spread.
MAX_NEW_POSITIONS_PER_DAY gates entries by day, not just by concurrency.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, date
from dataclasses import asdict

import config_directional_spread as dcfg
import trade_staging as staging
from models import DirectionalSpreadPosition
from atomic_state import atomic_write_json

log = logging.getLogger("directional_spread")

STATE_PATH = Path(dcfg.STATE_PATH)
JOURNAL_PATH = Path(dcfg.JOURNAL_PATH)
STATE_PATH.parent.mkdir(exist_ok=True)
JOURNAL_PATH.parent.mkdir(exist_ok=True)


def load_state() -> dict:
    """
    {"position": dict|None, "opened_today": int, "opened_today_date": "YYYY-MM-DD"}
    opened_today resets whenever the stored date isn't today -- same
    daily-reset pattern as trade_tracker.load_open_trades(), just without
    the stale-trade recovery machinery the momentum scanner needs (a
    directional spread left OPEN across a restart is still perfectly
    valid state; there's nothing to "recover", it just keeps being
    marked to market next cycle).
    """
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text())
    else:
        data = {"position": None, "opened_today": 0, "opened_today_date": None}

    today = date.today().isoformat()
    if data.get("opened_today_date") != today:
        data["opened_today"] = 0
        data["opened_today_date"] = today
    return data


def save_state(state: dict):
    atomic_write_json(STATE_PATH, state, default=str, indent=2)


def _append_journal(position: dict):
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(position, default=str) + "\n")


def open_position(state: dict, plan) -> dict:
    position = DirectionalSpreadPosition(plan=plan, opened_at=datetime.now().isoformat(timespec="seconds"))
    state["position"] = asdict(position)
    state["opened_today"] = state.get("opened_today", 0) + 1
    save_state(state)
    return state["position"]


def _current_leg_prices(chain: list, plan_dict: dict) -> dict:
    lookup = {(q.strike, q.option_type): q for q in chain}
    direction = plan_dict["direction"]

    def _price(strike):
        q = lookup.get((strike, direction))
        return q.ltp if q else None

    return {"short": _price(plan_dict["short_strike"]), "hedge": _price(plan_dict["hedge_strike"])}


def _mark_to_market_pnl_inr(plan_dict: dict, current_prices: dict) -> float:
    """
    Current cost to close = buy back the short, sell the hedge, at
    CURRENT prices. P&L = credit originally received - current cost to
    close. Returns None if either leg's current price is unavailable.
    """
    if current_prices["short"] is None or current_prices["hedge"] is None:
        return None
    lot_size = dcfg.NIFTY_LOT_SIZE
    lots = plan_dict["lots"]
    cost_to_close_now = current_prices["short"] - current_prices["hedge"]
    return round((plan_dict["net_credit"] - cost_to_close_now) * lot_size * lots, 2)


def check_breach_warning(spot: float, plan_dict: dict) -> str:
    """Returns a warning string if spot is within the buffer of the short strike, else None."""
    buffer = dcfg.BREACH_WARNING_BUFFER_POINTS
    short_strike = plan_dict["short_strike"]
    if plan_dict["direction"] == "PE" and spot <= short_strike + buffer:
        return f"Spot ({spot}) is within {buffer} points of the short PE strike ({short_strike})"
    if plan_dict["direction"] == "CE" and spot >= short_strike - buffer:
        return f"Spot ({spot}) is within {buffer} points of the short CE strike ({short_strike})"
    return None


def check_managed_exit(mtm_pnl: float, plan_dict: dict) -> str:
    """
    Returns "profit_target", "stop_loss", or None. Unlike the condor
    (held to expiry unless breached), this position is meant to be
    closed early once it's captured most of its available credit, or
    once it's given back too much of the max possible loss -- holding
    a credit spread for its last few points disproportionately extends
    overnight gap-risk exposure for little extra reward.
    """
    if mtm_pnl is None:
        return None
    max_profit = plan_dict["max_profit_inr"]
    max_loss = plan_dict["max_loss_inr"]
    if max_profit and mtm_pnl >= max_profit * (dcfg.PROFIT_TARGET_PCT_OF_MAX_PROFIT / 100):
        return "profit_target"
    if max_loss and mtm_pnl <= -max_loss * (dcfg.STOP_LOSS_PCT_OF_MAX_LOSS / 100):
        return "stop_loss"
    return None


def update_position(state: dict, snapshot) -> dict:
    """
    Call every poll while a position is open. Marks P&L to market,
    checks the managed-exit rule (auto-closes on hitting it -- unlike
    the breach warning, profit-target/stop-loss are mechanical exits a
    human doesn't need to approve each time, matching how the momentum
    scanner's own target/stop works), and stages a breach warning for
    human review if spot gets close to the short strike.
    Returns the (possibly updated, possibly now-closed) position dict.
    """
    position = state.get("position")
    if not position or position["status"] != "OPEN":
        return position

    plan_dict = position["plan"]
    current_prices = _current_leg_prices(snapshot.chain, plan_dict)
    mtm_pnl = _mark_to_market_pnl_inr(plan_dict, current_prices)
    position["current_mtm_pnl_inr"] = mtm_pnl

    exit_reason = check_managed_exit(mtm_pnl, plan_dict)
    if exit_reason:
        log.info(f"  Managed exit triggered: {exit_reason} (MTM P&L Rs {mtm_pnl:,.0f})")
        return close_position(state, snapshot, reason=exit_reason)

    warning = check_breach_warning(snapshot.spot, plan_dict)
    if warning and not position.get("breach_staged"):
        note = f"Current mark-to-market P&L: Rs {mtm_pnl}" if mtm_pnl is not None else "Mark-to-market P&L unavailable this cycle"
        log.info(f"  DIRECTIONAL SPREAD BREACH WARNING: {warning}. {note}")
        staging.stage_advisory(kind="directional_spread_breach_warning", detail=warning, note=note)
        position["breach_staged"] = True

    save_state(state)
    return position


def close_position(state: dict, snapshot, reason: str) -> dict:
    """
    Close (or settle at expiry) the current position. At expiry, chain
    quotes may be gone/zero for OTM legs -- fall back to intrinsic value
    (the only thing that's actually true at settlement), same reasoning
    as condor_tracker.close_position.
    """
    position = state["position"]
    plan_dict = position["plan"]
    spot = snapshot.spot
    direction = plan_dict["direction"]

    def _intrinsic(strike):
        if direction == "CE":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)

    current_prices = _current_leg_prices(snapshot.chain, plan_dict)
    for key, strike_key in (("short", "short_strike"), ("hedge", "hedge_strike")):
        if current_prices[key] is None:
            current_prices[key] = _intrinsic(plan_dict[strike_key])
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
    save_state(state)
    return position
