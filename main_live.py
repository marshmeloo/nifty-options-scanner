"""
Live polling loop. Fetches a real Nifty option chain from Dhan on an
interval, runs it through the same scan -> plan -> risk pipeline as
main.py, and prints recommendations. Places zero orders.

Every session's output is also saved to logs/nifty_scan_YYYYMMDD.log
(created automatically), so you can look back or share it without
needing terminal scrollback.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_live.py
"""

import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path

import config
from dhan_source import get_nifty_snapshot, get_nearest_expiry, get_nifty_intraday_candles
from scanner import scan, compute_market_bias, tag_bias_conflicts
from plan_generator import build_plan
from risk_checker import check
from price_action import analyze_with_context
import trade_tracker as tt

POLL_INTERVAL_SECONDS = 30   # OI/IV don't move meaningfully faster than this
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"nifty_scan_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("nifty_scanner")


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def run_once(expiry: str, state: dict, current_open_exposure_pct: float, current_daily_loss_pct: float):
    snapshot = get_nifty_snapshot(expiry=expiry)
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"\n[{ts}] NIFTY spot {snapshot.spot}, VWAP-proxy {snapshot.vwap}, PCR {snapshot.pcr}")

    try:
        candles = get_nifty_intraday_candles()
        price_levels, context = analyze_with_context(candles)
        if price_levels:
            log.info(f"  Structure: {len(price_levels)} OB/FVG/S-R/sweep/breakout/pullback levels detected")
        log.info(
            f"  Trend: {context.trend} | RSI: {context.rsi} ({context.rsi_state}) | "
            f"ROC: {context.roc_pct}% | Volume: {context.volume_ratio}x avg"
        )
    except Exception as e:
        log.info(f"  Price-action fetch failed this cycle, scanning without it: {e}")
        price_levels, context = [], None

    # --- Step 1: update trades already being tracked, BEFORE looking for new ones ---
    closed = tt.update_open_trades(state, snapshot)
    for trade in closed:
        log.info(
            f"  [TRADE CLOSED: {trade['outcome']}] {trade['strike']} {trade['option_type']}  "
            f"entry {trade['entry']} -> exit {trade['exit_ltp']}  pnl {trade['pnl_pct']:+.1f}%"
        )
        log.info(f"    lesson: {trade['lesson']}")

    if state["trades"]:
        log.info(f"  Currently tracking {len(state['trades'])} open trade(s):")
        for trade in state["trades"]:
            log.info(
                f"    {trade['strike']} {trade['option_type']}  entry {trade['entry']} "
                f"target {trade['target']} stop {trade['stop']}  "
                f"current {trade.get('current_ltp', '?')}  running pnl {trade.get('running_pnl_pct', 0):+.1f}%"
            )

    setups = scan(snapshot, price_levels=price_levels, context=context)
    if not setups:
        log.info("  No setups flagged this cycle.")
        tt.save_open_trades(state)
        return

    bias_label, bias_score, bias_reasons = compute_market_bias(snapshot, context)
    log.info(f"  Market bias: {bias_label} (score {bias_score})  [{', '.join(bias_reasons) if bias_reasons else 'no strong signal'}]")

    results = []
    for setup in setups:
        plan = build_plan(snapshot, setup)
        verdict = check(
            plan,
            current_open_exposure_pct=current_open_exposure_pct,
            current_daily_loss_pct=current_daily_loss_pct,
        )
        if verdict.decision == "REJECTED" and plan.lots == 0:
            continue
        results.append((setup, plan, verdict))

    tag_bias_conflicts(results)
    results.sort(key=lambda r: r[0].score, reverse=True)

    # --- Step 2: only consider opening ONE new trade this cycle, and only ---
    # if the daily cap isn't reached and conviction (after the learned
    # adjustment) clears the raised bar. This replaces printing the whole
    # noisy chain every cycle.
    if state["opened_today"] >= config.MAX_NEW_TRADES_PER_DAY:
        log.info(f"  Daily trade cap reached ({state['opened_today']}/{config.MAX_NEW_TRADES_PER_DAY}). Not opening new trades today.")
    else:
        new_trade = tt.try_open_new_trade(results, state, snapshot)
        if new_trade:
            log.info(
                f"  [NEW TRADE OPENED] {new_trade['strike']} {new_trade['option_type']}  "
                f"entry {new_trade['entry']} target {new_trade['target']} stop {new_trade['stop']}  "
                f"lots {new_trade['lots']}  score {new_trade['score_at_entry']} "
                f"(adjusted {new_trade['adjusted_score_at_entry']})"
            )
            log.info(f"    reasons: {', '.join(new_trade['reasons_at_entry'])}")
            if new_trade["learned_adjustment_notes"]:
                log.info(f"    learned adjustment: {'; '.join(new_trade['learned_adjustment_notes'])}")
            log.info(f"    trades opened today: {state['opened_today']}/{config.MAX_NEW_TRADES_PER_DAY}")
        elif results:
            best = results[0]
            log.info(
                f"  No new trade this cycle ({state['opened_today']}/{config.MAX_NEW_TRADES_PER_DAY} used today). "
                f"Highest candidate: {best[0].strike} {best[0].option_type} score {best[0].score} "
                f"(bar is {config.MIN_CONVICTION_SCORE_TO_TRACK})"
            )

    tt.save_open_trades(state)


def force_close_all(state: dict, expiry: str):
    """Called once at/after market close to settle any trades still open."""
    if not state["trades"]:
        return
    snapshot = get_nifty_snapshot(expiry=expiry)
    closed = tt.force_close_end_of_day(state, snapshot)
    for trade in closed:
        log.info(
            f"  [EOD CLOSE] {trade['strike']} {trade['option_type']}  "
            f"entry {trade['entry']} -> exit {trade['exit_ltp']}  pnl {trade['pnl_pct']:+.1f}%"
        )
        log.info(f"    lesson: {trade['lesson']}")
    tt.save_open_trades(state)


def run_forever(current_open_exposure_pct: float = 0.0, current_daily_loss_pct: float = 0.0):
    log.info("Fetching nearest expiry...")
    expiry = get_nearest_expiry()
    log.info(f"Tracking expiry: {expiry}. Polling every {POLL_INTERVAL_SECONDS}s during market hours.")
    log.info(f"Max {config.MAX_NEW_TRADES_PER_DAY} new trades/day, conviction bar {config.MIN_CONVICTION_SCORE_TO_TRACK}.")
    log.info(tt.summarize_recent_lessons())

    state = tt.load_open_trades()
    was_open_last_cycle = False

    while True:
        is_open = market_is_open()

        if is_open:
            try:
                run_once(expiry, state, current_open_exposure_pct, current_daily_loss_pct)
            except Exception as e:
                log.info(f"  Error this cycle (will retry next cycle): {e}")
            was_open_last_cycle = True
        else:
            if was_open_last_cycle and state["trades"]:
                log.info("  Market just closed — settling any still-open trades.")
                try:
                    force_close_all(state, expiry)
                except Exception as e:
                    log.info(f"  Could not settle open trades cleanly: {e}")
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] Market closed, sleeping...")
            was_open_last_cycle = False

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    # NOTE: current_open_exposure_pct and current_daily_loss_pct are still
    # manual inputs here. Wire these to Dhan's positions/funds endpoints
    # (https://dhanhq.co/docs/v2/portfolio/, https://dhanhq.co/docs/v2/funds/)
    # once you want the risk gate to reflect your real live account state.
    run_forever(current_open_exposure_pct=0.0, current_daily_loss_pct=0.0)
