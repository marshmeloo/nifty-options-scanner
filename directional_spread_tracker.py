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


def _update_excursion(position: dict, mtm_pnl: float, plan_dict: dict, timestamp=None):
    """
    Track the running best/worst mark-to-market P&L seen this position,
    and which PROFIT_MILESTONES_PCT levels it has touched and WHEN --
    same purpose as trade_tracker._update_excursion's R-multiple tracking
    for the momentum scanner, in this strategy's own unit (% of
    max_profit_inr, since a credit spread has no symmetric "R" the way a
    fixed-stop long option does).

    Called every cycle a position is evaluated, including the cycle that
    ends up closing it -- "reached 70% of max profit, then gave it back
    and stopped out" is a different lesson from "never got going", and
    that timing can't be reconstructed afterwards from the close alone.

    A cycle with no priceable mtm_pnl (a leg's quote missing) is skipped
    entirely rather than treated as flat -- same reasoning as
    check_managed_exit and the 2026-07-30 condor MTM incident this
    project keeps citing: missing information is not a zero.
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
    for milestone in dcfg.PROFIT_MILESTONES_PCT:
        key = str(milestone)
        if key not in hits and pct_of_max_profit >= milestone:
            hits[key] = stamp


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
    # Persisted so the dashboard can show live leg prices and a freshness
    # indicator, not just the aggregate MTM -- mirrors condor_tracker's
    # equivalent addition, same reasoning: "is this live" was previously
    # only answerable by reading the log, not the state file.
    position["current_leg_prices"] = current_prices
    position["mtm_updated_at"] = snapshot.timestamp.isoformat()
    _update_excursion(position, mtm_pnl, plan_dict, timestamp=snapshot.timestamp)

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
    # One last excursion update at the settlement price itself, so a
    # milestone touched only on the closing cycle (e.g. expiry settling
    # right at max profit) is still captured rather than missed because
    # the position closes in the same call that would have recorded it.
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
    save_state(state)
    return position


def _excursion_summary(position: dict, plan_dict: dict) -> dict:
    """
    Derived stats computed at close time from the running excursion
    _update_excursion tracked all along. `capture_efficiency_pct` answers
    "did we give back a real move before closing": of the best mark-to-
    market P&L this position ever showed, what fraction did the ACTUAL
    exit capture? 100% means it closed at or beyond its own peak.
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
        summary["would_have_won_at"] = [m for m in dcfg.PROFIT_MILESTONES_PCT if max_pct >= m]
        if pnl_inr is not None and max_seen > 0:
            summary["capture_efficiency_pct"] = round(pnl_inr / max_seen * 100, 1)
    if max_loss and min_seen is not None:
        summary["max_pct_of_max_loss"] = round(-min_seen / max_loss * 100, 1)
    return summary


def profit_milestone_stats(limit=None) -> dict:
    """
    Across closed journal entries: how many positions ever reached each
    configured % of max profit, and what the actual exits looked like.

    This is the dataset for answering "is 60% the right profit target?"
    (config.PROFIT_TARGET_PCT_OF_MAX_PROFIT) with evidence instead of
    intuition -- same purpose as trade_tracker.rr_milestone_stats() for
    the momentum scanner's DEFAULT_TARGET_RR. `reached` counts positions
    whose BEST mark-to-market P&L touched that % at any point while open,
    i.e. the positions a target set there would have converted into wins
    at that level.

    Entries from before this tracking existed have no max_pct_of_max_profit
    and are excluded from `sample` rather than counted as zero -- an
    unmeasured position is not a failed one.
    """
    entries = [
        e for e in _load_recent_journal(limit)
        if e.get("max_pct_of_max_profit") is not None
    ]
    if not entries:
        return {"sample": 0, "milestones": []}

    milestones = []
    for m in dcfg.PROFIT_MILESTONES_PCT:
        reached = sum(1 for e in entries if e["max_pct_of_max_profit"] >= m)
        # Simulated P&L if the target had been set here: a position whose
        # peak reached this level exits AT it (as a fraction of its own
        # max_profit_inr); every other position keeps the % it actually
        # closed at. Same "expectancy, not hit-rate" reasoning as the
        # momentum version -- a low target hits more often but captures
        # less each time, and can still be worse overall.
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

    return {
        "sample": len(entries),
        "milestones": milestones,
        "current_target_pct": dcfg.PROFIT_TARGET_PCT_OF_MAX_PROFIT,
    }


def _load_recent_journal(limit=None) -> list:
    limit = limit or 500
    if not JOURNAL_PATH.exists():
        return []
    lines = [l for l in JOURNAL_PATH.read_text().strip().split("\n") if l]
    return [json.loads(l) for l in lines[-limit:]]


def summarize_profit_milestones(limit=None) -> str:
    """Human-readable version of profit_milestone_stats(), for session startup."""
    stats = profit_milestone_stats(limit)
    if not stats["sample"]:
        return ("Directional spread profit-milestone history: none yet (tracking "
                "starts with positions opened from now on).")

    lines = [
        f"Directional spread profit-milestone history ({stats['sample']} closed "
        f"positions, current target {stats['current_target_pct']:g}% of max profit):"
    ]
    best = max(m["simulated_avg_pct_of_max_profit"] for m in stats["milestones"])
    for m in stats["milestones"]:
        marker = "  <-- current" if m["pct"] == stats["current_target_pct"] else ""
        if m["simulated_avg_pct_of_max_profit"] == best:
            marker += "  [best simulated]"
        lines.append(
            f"  {m['pct']:>4}% reached by {m['reached']:>3} ({m['hit_rate_pct']:>5.1f}%), "
            f"simulated avg {m['simulated_avg_pct_of_max_profit']:+.1f}% of max profit{marker}"
        )
    return "\n".join(lines)
