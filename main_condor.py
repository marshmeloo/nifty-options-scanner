"""
Standalone live loop for the iron condor strategy. Runs as its own
process, completely independent of main_live.py -- separate state
(config_condor.STATE_PATH), separate journal (config_condor.JOURNAL_PATH),
separate log file, separate risk rules. You can run this alongside
main_live.py in another terminal; they share the data-fetching layer
(resilient_source.py) but nothing else, and neither can affect the
other's state.

What it does each cycle:
  - No position open -> pick the nearest expiry with at least
    MIN_DAYS_TO_EXPIRY_TO_OPEN days left (skipping past the nearest one
    if it's today or too close -- see choose_expiry_to_open), scan for a
    condor, risk-check it, and open it. AUTO_APPROVE_NEW_POSITIONS
    (config_condor.py) controls whether that happens immediately or is
    staged for manual review via approve_orders.py first.
  - Position open -> mark it to market, check for a breach warning
    (staged for review, not auto-closed -- see config_condor.py), and on
    expiry day, settle it.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_condor.py
"""

import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from dataclasses import asdict

import config_condor as ccfg
from resilient_source import get_nifty_snapshot, get_expiry_list
import condor_scanner
import condor_plan_generator
import condor_risk_checker
import condor_tracker
import trade_staging as staging

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"condor_{datetime.now().strftime('%Y%m%d')}.log"

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


def choose_expiry_to_open(expiry_list: list, min_days_out: int, today=None) -> str:
    """
    First expiry in `expiry_list` (nearest-first, as every source returns
    it) with at least `min_days_out` days remaining. Returns None if
    nothing in the list qualifies (shouldn't normally happen -- Dhan
    lists expiries months out).

    REPLACES the old is_day_after_expiry() approach entirely. That one
    only allowed opening in a narrow 1-3 day window right after the
    PREVIOUS expiry passed (and had a bug that meant it could never fire
    at all -- see the 2026-07-29 README entry). This is simpler and does
    more: it allows opening on ANY day the position is flat, and
    correctly rolls straight to next week's expiry if run on expiry day
    itself, when the "nearest" expiry has ~0 days of theta left to sell.
    No state tracking needed -- every cycle just asks the list directly.
    """
    today = today or datetime.now().date()
    for expiry_str in expiry_list:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        if (expiry_date - today).days >= min_days_out:
            return expiry_str
    return None


def run_once(state: dict):
    expiry_list = get_expiry_list()
    nearest_expiry = expiry_list[0]
    snapshot = get_nifty_snapshot(expiry=nearest_expiry)
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

    expiry = choose_expiry_to_open(expiry_list, ccfg.MIN_DAYS_TO_EXPIRY_TO_OPEN)
    if expiry is None:
        log.info("  No expiry far enough out to open against this cycle.")
        return
    if expiry != nearest_expiry:
        log.info(f"  Nearest expiry {nearest_expiry} is too close (< {ccfg.MIN_DAYS_TO_EXPIRY_TO_OPEN}d) -- "
                 f"rolling to {expiry} instead.")
        snapshot = get_nifty_snapshot(expiry=expiry)

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
        record = staging.stage_and_maybe_auto_open(
            kind="condor_open_candidate", detail=detail, note=f"expiry {expiry}",
            data=asdict(plan), auto_approve=ccfg.AUTO_APPROVE_NEW_POSITIONS,
            open_fn=condor_tracker.open_position, open_args=(state, plan),
        )
        if record["status"] == "EXECUTED":
            log.info("  AUTO-APPROVED and opened immediately.")
        else:
            log.info("  Staged for approval -- run approve_orders.py, then open_approved_condor.py once approved.")


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
