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
import india_vix_source as ivs

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# NOT 15:15. First guess (2026-08-04) was that quotes go dead at 15:15,
# based on NIFTY SPOT freezing solid at that time in the recorded data
# -- but checking actual OPTION premiums in that same window (24450PE:
# 33.5 -> 5.05, 24400CE: 108.85 -> 159.9, both between 15:15 and 15:26)
# showed they kept moving, often violently, well past 15:15. The real
# structure (per NSE's published schedule): continuous CASH trading
# ends 15:15 and the underlying goes into a closing auction (why spot
# froze -- it's not a dead feed, the underlying just isn't in
# continuous trading anymore), options get their own reactive window
# from roughly 15:35-15:40, and 15:40 is the official F&O close.
# Settling at 15:15 would grab an arbitrary, still-volatile mid-window
# price instead of something close to the real final one.
#
# This does NOT change when a condor can be OPENED (still governed by
# MARKET_CLOSE/MARKET_OPEN above) -- only when its OWN expiry-day
# settlement fires. On 2026-08-04 the live process also stopped polling
# before 15:30 ever arrived, leaving the position unsettled entirely
# regardless of price quality; triggering at 15:40 instead of waiting
# for market_is_open() to go False at 15:30 fixes that too.
#
# STILL UNVERIFIED: whether Dhan's option-chain response carries a
# dedicated settlement/closing-price field distinct from last-traded-
# price -- the live endpoint only responds during market hours, so this
# needs checking next session (see BACKLOG.md). Until then, 15:40 with
# the last available LTP is the best available proxy, not a final answer.
EXPIRY_SETTLEMENT_CUTOFF = dtime(15, 40)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"condor_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("condor")


def should_settle_on_expiry(expiry_date, now: datetime = None) -> bool:
    """
    Past the expiry date entirely (process was down over the actual
    expiry), OR it's expiry day and past the true F&O close -- see
    EXPIRY_SETTLEMENT_CUTOFF's own comment for why this doesn't wait
    for the nominal 15:30 close, and isn't 15:15 either.
    """
    now = now or datetime.now()
    if now.date() > expiry_date:
        return True
    return now.date() == expiry_date and now.time() >= EXPIRY_SETTLEMENT_CUTOFF


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


def _tracked_strikes(position: dict) -> set:
    """
    Strikes an open condor position needs to keep seeing in every chain
    fetch, regardless of how far they drift from spot. See
    dhan_source.get_nifty_snapshot's docstring for the incident this
    prevents: a hedge leg silently vanishing from the chain as spot
    drifts, with MTM P&L going "unavailable" and no error logged.
    """
    if not position or position.get("status") != "OPEN":
        return set()
    plan = position["plan"]
    return {plan["short_ce_strike"], plan["short_pe_strike"],
            plan["hedge_ce_strike"], plan["hedge_pe_strike"]}


def run_once(state: dict):
    protect = _tracked_strikes(state.get("position"))
    expiry_list = get_expiry_list()
    nearest_expiry = expiry_list[0]
    snapshot = get_nifty_snapshot(expiry=nearest_expiry, must_include_strikes=protect)
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"[{ts}] ({snapshot.source}) NIFTY spot {snapshot.spot}")

    position = state.get("position")

    if position and position.get("status") == "OPEN":
        position = condor_tracker.update_position(state, snapshot)
        if position.get("status") != "OPEN":
            # update_position closed it this cycle (profit target reached)
            # -- nothing left to mark-to-market or check expiry against.
            log.info(
                f"  [CONDOR CLOSED] reason={position.get('close_reason')}  "
                f"pnl Rs {position['pnl_inr']:,.0f} ({position.get('pnl_pct_of_max_profit')}% of max profit)"
            )
            return
        mtm = position.get("current_mtm_pnl_inr")
        log.info(
            f"  Open condor: short CE {position['plan']['short_ce_strike']} / "
            f"short PE {position['plan']['short_pe_strike']}  "
            f"MTM P&L: {'Rs ' + format(mtm, ',.0f') if mtm is not None else 'unavailable this cycle'}"
        )
        max_profit = position["plan"].get("max_profit_inr")
        max_seen = position.get("max_pnl_inr_seen")
        if max_profit and max_seen is not None:
            hits = sorted(int(m) for m in position.get("profit_milestones_hit", {}))
            peak_pct = max_seen / max_profit * 100
            milestone_note = f", milestone {max(hits)}%" if hits else ", no milestone yet"
            log.info(f"    peak MTM Rs {max_seen:,.0f} ({peak_pct:.0f}% of max profit){milestone_note}")

        expiry_ctx_date = datetime.strptime(position["plan"]["expiry"], "%Y-%m-%d").date()
        if should_settle_on_expiry(expiry_ctx_date):
            log.info("  Expiry reached and past the retail-tradable window -- settling position.")
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

    if ccfg.MIN_IV_RANK_TO_OPEN is not None:
        try:
            vix_history = ivs.load_or_refresh_history()
            current_vix = ivs.fetch_live_vix()
            iv_info = ivs.live_iv_rank_and_percentile(vix_history, current_vix)
        except Exception as e:
            log.info(f"  Could not fetch India VIX for the IV-rank gate this cycle ({e}) -- skipping open.")
            return
        iv_rank = iv_info["iv_rank"]
        if iv_rank is None:
            log.info("  IV rank not computable yet (insufficient VIX history) -- skipping open this cycle.")
            return
        if iv_rank < ccfg.MIN_IV_RANK_TO_OPEN:
            log.info(f"  IV rank {iv_rank:.1f} is below the entry threshold ({ccfg.MIN_IV_RANK_TO_OPEN:.0f}) "
                      f"-- skipping open this cycle (VIX {iv_info['vix']}).")
            return
        log.info(f"  IV rank {iv_rank:.1f} clears the entry threshold ({ccfg.MIN_IV_RANK_TO_OPEN:.0f}, VIX {iv_info['vix']}).")

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
