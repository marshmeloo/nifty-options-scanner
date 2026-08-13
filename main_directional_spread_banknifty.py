"""
Standalone live loop for the directional credit-spread strategy on Bank
Nifty -- paper-trading only, same "NOTHING HERE PLACES A REAL BROKER
ORDER" guarantee as main_directional_spread.py. Own process, own
state/journal/log, same "one process per Bank Nifty variant" pattern as
main_price_action_banknifty.py, main_live_banknifty.py, and
main_condor_banknifty.py.

Validated 2026-08-13 and added to start_trading.ps1 -- see CALIBRATION
STATUS below for the numbers and honest caveats.

STRUCTURAL NOTE -- SAME EXPIRY-CYCLE FACT AS THE CONDOR
-----------------------------------------------------------
Bank Nifty has been MONTHLY-expiry-only since SEBI's Nov 2024 rules
(verified live 2026-08-12 -- see main_condor_banknifty.py's own
docstring for the exact expiry lists compared). This matters LESS here
than for the condor: this strategy is actively managed with a profit
target / stop loss and typically closes well before expiry rather than
being held to it, so the monthly-vs-weekly underlying cycle mostly
affects strike/hedge selection and gap-risk sizing, not the fundamental
shape of the strategy the way it does for the condor.

CALIBRATION STATUS -- VALIDATED 2026-08-13
------------------------------------------------------------------------
SHORT_PREMIUM_MIN/MAX=250/450, HEDGE_DISTANCE_POINTS=350 (proportionally
reasoned from the NIFTY spread's own swept values, then checked against
real chain data -- 20/20 samples found a valid short leg, 65% also found
the hedge within the reconstructed data's coverage window) were tested
against the full 5-year Bank Nifty history, real p90 costs, split into 5
INDEPENDENT ~1-year periods (not just an aggregate or a single walk-
forward split): 4 of 5 years positive (net +Rs 132,462 total). The
single best trade in the whole run (+Rs 36,705, inside the strongest
year) accounts for 28% of total profit -- removing it entirely still
leaves +Rs 95,757 and 4/5 years positive, so the edge does not depend on
that one trade. The one negative year (Y4, 2024-08..2025-07, -Rs 5,189)
traces to 4 stop-loss trades, but a full STOP_LOSS_PCT_OF_MAX_LOSS sweep
(30/40/50/60/70%/disabled) showed Y4 stays negative at EVERY setting,
including with the stop disabled -- real period variance in that year's
directional calls, not a miscalibrated stop. The inherited 50% stop
(unchanged from NIFTY) turned out to already be near-optimal among
everything tested. Full numbers in BACKLOG.md's 2026-08-13 entry.

Standing caveat, same as every backtest against this reconstructed data:
LTP-fallback fills (no bid/ask in historical_source.py's data), an
OPTIMISTIC bias relative to live. This is meaningfully less clean
evidence than momentum's 5/5-period result, but far stronger than the
condor's outright failure.

DATA SOURCE: dhan_source directly with BANKNIFTY_UNDERLYING_SCRIP/_SEG
-- NOT resilient_source.py (hardcoded to NIFTY). Dhan-only, no NSE/
TradingView fallback, same limitation every Bank Nifty variant here
already accepts. Market bias (scanner.compute_market_bias) is computed
from BANK NIFTY's own candles, not Nifty's -- this strategy trades
whichever direction Bank Nifty itself is leaning, not Nifty's.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_directional_spread_banknifty.py
"""

import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from dataclasses import asdict

import dhan_source
import config_directional_spread as dcfg

BANKNIFTY_SECURITY_ID = dhan_source.BANKNIFTY_UNDERLYING_SCRIP
BANKNIFTY_SEG = dhan_source.BANKNIFTY_UNDERLYING_SEG

# --- Bank Nifty overrides -- see module docstring's CALIBRATION STATUS.
# Patched BEFORE directional_spread_tracker is imported (it snapshots
# dcfg.STATE_PATH/JOURNAL_PATH at import time -- same hazard as
# condor_tracker.py, see main_condor_banknifty.py's own comment).
dcfg.NIFTY_LOT_SIZE = 30
dcfg.SHORT_PREMIUM_MIN = 250.0
dcfg.SHORT_PREMIUM_MAX = 450.0
dcfg.HEDGE_DISTANCE_POINTS = 350
dcfg.STATE_PATH = "state/directional_spread_position_banknifty.json"
dcfg.JOURNAL_PATH = "logs/directional_spread_journal_banknifty.jsonl"

from scanner import compute_market_bias
from price_action import analyze_with_context
import directional_spread_scanner as dss
import directional_spread_plan_generator as dpg
import directional_spread_risk_checker as drc
import directional_spread_tracker as dst
import trade_staging as staging

assert "banknifty" in str(dst.STATE_PATH).lower(), (
    "directional_spread_tracker captured Nifty's STATE_PATH -- import order regressed, "
    "dcfg must be patched (done above) BEFORE `import directional_spread_tracker`"
)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
# Same NOT-independently-re-verified-for-Bank-Nifty caveat as
# main_condor_banknifty.py's own EXPIRY_SETTLEMENT_CUTOFF comment.
EXPIRY_SETTLEMENT_CUTOFF = dtime(15, 40)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"directional_spread_banknifty_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("directional_spread_banknifty")


def get_banknifty_expiry_list() -> list:
    return dhan_source.get_expiry_list(BANKNIFTY_SECURITY_ID, BANKNIFTY_SEG)


def get_banknifty_nearest_expiry() -> str:
    return dhan_source.get_nearest_expiry(BANKNIFTY_SECURITY_ID, BANKNIFTY_SEG)


def get_banknifty_snapshot(expiry: str = None, must_include_strikes: set = None):
    return dhan_source.get_nifty_snapshot(
        expiry=expiry, must_include_strikes=must_include_strikes,
        symbol="BANKNIFTY", underlying_scrip=BANKNIFTY_SECURITY_ID, underlying_seg=BANKNIFTY_SEG,
    )


def get_banknifty_intraday_candles(interval: str = None, from_date: str = None, to_date: str = None):
    return dhan_source.get_nifty_intraday_candles(
        interval=interval, from_date=from_date, to_date=to_date,
        security_id=BANKNIFTY_SECURITY_ID, exchange_seg=BANKNIFTY_SEG,
    )


def should_settle_on_expiry(expiry_date, now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.date() > expiry_date:
        return True
    return now.date() == expiry_date and now.time() >= EXPIRY_SETTLEMENT_CUTOFF


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _tracked_strikes(position: dict) -> set:
    if not position or position.get("status") != "OPEN":
        return set()
    plan = position["plan"]
    return {plan["short_strike"], plan["hedge_strike"]}


def run_once(state: dict):
    protect = _tracked_strikes(state.get("position"))
    expiry = get_banknifty_nearest_expiry()
    snapshot = get_banknifty_snapshot(expiry=expiry, must_include_strikes=protect)
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"[{ts}] ({snapshot.source}) BANKNIFTY spot {snapshot.spot}")

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
            max_profit = plan.get("max_profit_inr")
            max_seen = position.get("max_pnl_inr_seen")
            if max_profit and max_seen is not None:
                hits = sorted(int(m) for m in position.get("profit_milestones_hit", {}))
                peak_pct = max_seen / max_profit * 100
                milestone_note = f", milestone {max(hits)}%" if hits else ", no milestone yet"
                log.info(f"    peak MTM Rs {max_seen:,.0f} ({peak_pct:.0f}% of max profit){milestone_note}")
        elif position:
            log.info(f"  [SPREAD CLOSED: {position['close_reason']}] pnl Rs {position['pnl_inr']:,.0f} "
                     f"({position['pnl_pct_of_max_profit']}% of max profit)")

        position = state.get("position")
        if position and position.get("status") == "OPEN":
            expiry_ctx_date = datetime.strptime(position["plan"]["expiry"], "%Y-%m-%d").date()
            if should_settle_on_expiry(expiry_ctx_date):
                log.info("  Expiry reached and past the retail-tradable window -- settling position.")
                closed = dst.close_position(state, snapshot, reason="expiry_settlement")
                log.info(f"  [SPREAD SETTLED] pnl Rs {closed['pnl_inr']:,.0f} ({closed['pnl_pct_of_max_profit']}% of max profit)")
        return

    try:
        candles = get_banknifty_intraday_candles()
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
            f"Open Bank Nifty {plan.direction} spread ({'bull put' if plan.direction == 'PE' else 'bear call'}): "
            f"sell {plan.short_strike} @ {plan.short_premium}, hedge with {plan.hedge_strike} @ {plan.hedge_premium}. "
            f"Net credit Rs {plan.net_credit_inr:,.0f}, max loss Rs {plan.max_loss_inr:,.0f}, "
            f"breakeven {plan.breakeven}. Bias: {plan.bias_label} (score {plan.bias_score})."
        )
        record = staging.stage_and_maybe_auto_open(
            kind="directional_spread_banknifty_open_candidate", detail=detail, note=f"expiry {expiry}",
            data=asdict(plan), auto_approve=dcfg.AUTO_APPROVE_NEW_POSITIONS,
            open_fn=dst.open_position, open_args=(state, plan),
        )
        if record["status"] == "EXECUTED":
            log.info("  AUTO-APPROVED and opened immediately.")
        else:
            log.info("  Staged for approval -- run approve_orders.py, then open_approved_directional_spread.py once approved.")


def run_forever():
    state = dst.load_state()
    log.info("Bank Nifty directional spread strategy loop started. Ctrl+C to stop.")
    log.info("  CALIBRATION NOTE: strike-selection numbers are placeholders, not independently "
             "swept -- see this file's own module docstring before trusting any result.")
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
