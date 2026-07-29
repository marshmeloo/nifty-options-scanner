"""
Standalone live loop for the iron condor strategy. Runs as its own
process, completely independent of main_live.py -- separate state
(config_condor.STATE_PATH), separate journal (config_condor.JOURNAL_PATH),
separate log file, separate risk rules. You can run this alongside
main_live.py in another terminal; they share the data-fetching layer
(resilient_source.py) but nothing else, and neither can affect the
other's state.

What it does each cycle:
  - No position open, and today looks like the right day to open one
    (the trading day right after the previous expiry) -> scan for a
    condor, risk-check it, and STAGE it (via trade_staging.py) for you
    to approve. Does NOT open a position automatically -- this strategy
    ties up real capital with defined-but-real risk, and the strike
    selection logic hasn't been battle-tested yet.
  - Position open -> mark it to market, check for a breach warning
    (staged for review, not auto-closed -- see config_condor.py), and on
    expiry day, settle it.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_condor.py
"""

import json
import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from dataclasses import asdict

import config_condor as ccfg
from resilient_source import get_nifty_snapshot, get_nearest_expiry
import condor_scanner
import condor_plan_generator
import condor_risk_checker
import condor_tracker
import trade_staging as staging
from atomic_state import atomic_write_json

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"condor_{datetime.now().strftime('%Y%m%d')}.log"

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
EXPIRY_TRACKING_PATH = STATE_DIR / "condor_expiry_tracking.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("condor")


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _load_expiry_tracking() -> dict:
    if EXPIRY_TRACKING_PATH.exists():
        try:
            return json.loads(EXPIRY_TRACKING_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"current_expiry": None, "previous_expiry": None}


def is_day_after_expiry(nearest_upcoming_expiry: str) -> bool:
    """
    True if the most recently PASSED expiry was within the last 1-3 days
    -- i.e. today is (close to) the first day of a fresh weekly cycle.

    BUG FIXED 2026-07-29: this used to receive the same value as
    `nearest_upcoming_expiry` and compare it directly against today. But
    Dhan's expirylist endpoint (get_nearest_expiry) only ever returns
    ACTIVE/UPCOMING expiries -- so that date is always today-or-future,
    days_since_expiry = today - expiry_date was therefore always <= 0,
    and `1 <= days_since_expiry <= 3` could structurally never be True.
    The condor strategy could never open a position; every cycle logged
    "Not the day after expiry."

    There is no API that returns the PREVIOUS (already-settled) expiry
    directly, so it's derived from state: every time the value
    get_nearest_expiry() returns CHANGES from what was last observed,
    that change itself is the signal that the old value just settled.
    The old "current" becomes the new "previous", and stays frozen there
    (not overwritten again) until the NEXT rollover, which is what keeps
    the 1-3 day grace window working across multiple cycles/days -- e.g.
    if staging fails on the day of rollover itself, the next day's check
    still has access to the same previous_expiry to retry against.

    Self-corrects to whatever NSE's actual expiry calendar is (including
    holiday-shifted or monthly-special expiries) since it never assumes
    a fixed cycle length -- same philosophy as premarket.py's expiry
    handling. Also naturally safe after a long outage: if the loop was
    down for weeks, the "previous_expiry" it reconstructs on resuming
    will be however-many-weeks-old, `days_since_expiry` will be large,
    and the 1-3 day check correctly returns False rather than misfiring.
    """
    state = _load_expiry_tracking()

    if state["current_expiry"] != nearest_upcoming_expiry:
        state["previous_expiry"] = state["current_expiry"]
        state["current_expiry"] = nearest_upcoming_expiry
        atomic_write_json(EXPIRY_TRACKING_PATH, state)

    if not state["previous_expiry"]:
        return False  # no prior expiry on record yet (e.g. first-ever run)

    previous_expiry_date = datetime.strptime(state["previous_expiry"], "%Y-%m-%d").date()
    today = datetime.now().date()
    days_since_expiry = (today - previous_expiry_date).days
    return 1 <= days_since_expiry <= 3  # covers a weekend sitting between Tue expiry and Wed/Mon open


def run_once(state: dict):
    expiry = get_nearest_expiry()
    snapshot = get_nifty_snapshot(expiry=expiry)
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"[{ts}] ({snapshot.source}) NIFTY spot {snapshot.spot}")

    position = state.get("position")

    if position and position.get("status") == "OPEN":
        position = condor_tracker.update_position(state, snapshot)
        mtm = position.get("current_mtm_pnl_inr")
        log.info(
            f"  Open condor: short CE {position['plan']['short_ce_strike']} / "
            f"short PE {position['plan']['short_pe_strike']}  "
            f"MTM P&L: {'Rs ' + format(mtm, ',.0f') if mtm is not None else 'unavailable this cycle'}"
        )

        expiry_ctx_date = datetime.strptime(position["plan"]["expiry"], "%Y-%m-%d").date()
        if datetime.now().date() >= expiry_ctx_date and not market_is_open():
            log.info("  Expiry reached and market closed -- settling position.")
            closed = condor_tracker.close_position(state, snapshot, reason="expiry_settlement")
            log.info(f"  [CONDOR SETTLED] pnl Rs {closed['pnl_inr']:,.0f} ({closed['pnl_pct_of_max_profit']}% of max profit)")
        return

    if is_day_after_expiry(expiry):
        legs = condor_scanner.find_condor_legs(snapshot.chain)
        plan = condor_plan_generator.build_condor_plan(legs, expiry)
        verdict = condor_risk_checker.check(plan, currently_open_positions=0)

        if plan is None:
            log.info("  No complete condor available this cycle (missing a leg in the chain).")
            return

        log.info(
            f"  Candidate condor: sell {plan.short_ce_strike}CE/{plan.short_pe_strike}PE, "
            f"hedge {plan.hedge_ce_strike}CE/{plan.hedge_pe_strike}PE  "
            f"net credit Rs {plan.net_credit_inr:,.0f}  max loss Rs {plan.max_loss_inr:,.0f}"
        )
        log.info(f"  Risk check: {verdict.decision} -- {'; '.join(verdict.reasons) if verdict.reasons else 'all checks passed'}")

        if verdict.decision == "APPROVED":
            detail = (
                f"Open condor: sell {plan.short_ce_strike}CE @ {plan.short_ce_premium} / "
                f"sell {plan.short_pe_strike}PE @ {plan.short_pe_premium}, hedge with "
                f"{plan.hedge_ce_strike}CE @ {plan.hedge_ce_premium} / {plan.hedge_pe_strike}PE @ {plan.hedge_pe_premium}. "
                f"Net credit Rs {plan.net_credit_inr:,.0f}, max loss Rs {plan.max_loss_inr:,.0f}, "
                f"breakevens {plan.breakeven_lower}/{plan.breakeven_upper}."
            )
            staging.stage_advisory(
                kind="condor_open_candidate",
                detail=detail,
                note=f"expiry {expiry}",
                data=asdict(plan),
            )
            log.info("  Staged for approval -- run approve_orders.py, then open_approved_condor.py once approved.")
    else:
        log.info("  Not the day after expiry -- no new condor considered this cycle.")


def run_forever():
    state = condor_tracker.load_position()
    log.info("Condor strategy loop started. Ctrl+C to stop.")
    while True:
        if market_is_open():
            try:
                run_once(state)
            except Exception as e:
                log.info(f"  Error this cycle (will retry next cycle): {e}")
        else:
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] Market closed, sleeping...")
        time.sleep(ccfg.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
