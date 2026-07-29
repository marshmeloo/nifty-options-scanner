"""
Standalone live loop for the directional credit-spread strategy. Runs as
its own process, completely independent of main_live.py and
main_condor.py -- separate state (config_directional_spread.STATE_PATH),
separate journal, separate log file, separate risk rules. Shares only
the data-fetching layer (resilient_source.py), same as the condor does.

What it does each cycle (every POLL_INTERVAL_SECONDS, matching the
momentum scanner's cadence -- this strategy can open on any day the
market bias reads strongly enough, not just once a week like the
condor):
  - No position open, daily cap not reached, and the market bias is
    strong enough to pick a side -> scan for a 2-leg directional
    spread, risk-check it, and STAGE it for approval. Does NOT open a
    position automatically -- same reasoning as main_condor.py: this
    ties up real capital with defined-but-real risk, and the strike
    selection logic hasn't been battle-tested yet.
  - Position open -> mark it to market, auto-close on hitting the
    profit target or stop loss (mechanical exits, no approval needed --
    same as the momentum scanner's own target/stop), stage a breach
    warning for human review if spot nears the short strike, and settle
    at expiry if reached.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_directional_spread.py
"""

import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from dataclasses import asdict

import config_directional_spread as dcfg
from resilient_source import get_nifty_snapshot, get_nearest_expiry, get_nifty_intraday_candles
from scanner import compute_market_bias
from price_action import analyze_with_context
import directional_spread_scanner as dss
import directional_spread_plan_generator as dpg
import directional_spread_risk_checker as drc
import directional_spread_tracker as dst
import trade_staging as staging

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"directional_spread_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("directional_spread")


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def run_once(state: dict):
    expiry = get_nearest_expiry()
    snapshot = get_nifty_snapshot(expiry=expiry)
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"[{ts}] ({snapshot.source}) NIFTY spot {snapshot.spot}")

    position = state.get("position")
    if position and position.get("status") == "OPEN":
        position = dst.update_position(state, snapshot)
        if position and position.get("status") == "OPEN":
            mtm = position.get("current_mtm_pnl_inr")
            plan = position["plan"]
            log.info(
                f"  Open spread: {plan['direction']} short {plan['short_strike']} / "
                f"hedge {plan['hedge_strike']}  "
                f"MTM P&L: {'Rs ' + format(mtm, ',.0f') if mtm is not None else 'unavailable this cycle'}"
            )
        elif position:
            log.info(f"  [SPREAD CLOSED: {position['close_reason']}] pnl Rs {position['pnl_inr']:,.0f} "
                     f"({position['pnl_pct_of_max_profit']}% of max profit)")

        # Settle at expiry regardless of whether the managed-exit check
        # above already closed it this same cycle (it won't have, but a
        # position that reaches expiry day without hitting either exit
        # still needs a mechanical settlement, same as the condor).
        position = state.get("position")
        if position and position.get("status") == "OPEN":
            expiry_ctx_date = datetime.strptime(position["plan"]["expiry"], "%Y-%m-%d").date()
            if datetime.now().date() >= expiry_ctx_date and not market_is_open():
                log.info("  Expiry reached and market closed -- settling position.")
                closed = dst.close_position(state, snapshot, reason="expiry_settlement")
                log.info(f"  [SPREAD SETTLED] pnl Rs {closed['pnl_inr']:,.0f} ({closed['pnl_pct_of_max_profit']}% of max profit)")
        return

    try:
        candles = get_nifty_intraday_candles()
        _levels, context = analyze_with_context(candles)
    except Exception as e:
        log.info(f"  Could not fetch price-action context this cycle, skipping: {e}")
        return

    bias_label, bias_score, bias_reasons = compute_market_bias(snapshot, context)
    log.info(f"  Market bias: {bias_label} (score {bias_score})  [{', '.join(bias_reasons) if bias_reasons else 'no strong signal'}]")

    legs = dss.find_directional_spread_legs(snapshot.chain, bias_label, bias_score)
    if legs["direction"] is None:
        log.info("  Bias not strong enough to pick a side this cycle -- no spread considered.")
        return

    plan = dpg.build_directional_spread_plan(legs, expiry, bias_label, bias_score)
    verdict = drc.check(plan, currently_open_positions=0, opened_today=state.get("opened_today", 0))

    if plan is None:
        log.info(f"  No complete {legs['direction']} spread available this cycle (missing a leg in the chain).")
        return

    log.info(
        f"  Candidate {plan.direction} spread: sell {plan.short_strike} @ {plan.short_premium}, "
        f"hedge {plan.hedge_strike} @ {plan.hedge_premium}  "
        f"net credit Rs {plan.net_credit_inr:,.0f}  max loss Rs {plan.max_loss_inr:,.0f}"
    )
    log.info(f"  Risk check: {verdict.decision} -- {'; '.join(verdict.reasons) if verdict.reasons else 'all checks passed'}")

    if verdict.decision == "APPROVED":
        detail = (
            f"Open {plan.direction} spread ({'bull put' if plan.direction == 'PE' else 'bear call'}): "
            f"sell {plan.short_strike} @ {plan.short_premium}, hedge with {plan.hedge_strike} @ {plan.hedge_premium}. "
            f"Net credit Rs {plan.net_credit_inr:,.0f}, max loss Rs {plan.max_loss_inr:,.0f}, "
            f"breakeven {plan.breakeven}. Bias: {plan.bias_label} (score {plan.bias_score})."
        )
        staging.stage_advisory(
            kind="directional_spread_open_candidate",
            detail=detail,
            note=f"expiry {expiry}",
            data=asdict(plan),
        )
        log.info("  Staged for approval -- run approve_orders.py, then open_approved_directional_spread.py once approved.")


def run_forever():
    state = dst.load_state()
    log.info("Directional spread strategy loop started. Ctrl+C to stop.")
    while True:
        if market_is_open():
            try:
                run_once(state)
            except Exception as e:
                log.info(f"  Error this cycle (will retry next cycle): {e}")
        else:
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] Market closed, sleeping...")
        time.sleep(dcfg.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
