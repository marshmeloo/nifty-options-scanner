"""
Standalone live loop for the iron condor strategy on Bank Nifty --
paper-trading only, same "NOTHING HERE PLACES A REAL BROKER ORDER"
guarantee as main_condor.py. Own process, own state/journal/log, same
"one process per Bank Nifty variant" pattern as
main_price_action_banknifty.py and main_live_banknifty.py.

DO NOT ADD TO start_trading.ps1 -- see CALIBRATION STATUS below. This
strategy was tested and found NET NEGATIVE even with the profit-target
exit that fixed NIFTY's condor; see BACKLOG.md's 2026-08-13 "closed, not
adopted" entry before spending more time tuning this file's numbers.

STRUCTURAL DIFFERENCE FROM THE NIFTY CONDOR (read this first)
---------------------------------------------------------------
Verified live 2026-08-12 (dhan_source.get_expiry_list against both
underlyings): Bank Nifty has been MONTHLY-expiry-only since SEBI's
Nov 2024 rules capped weekly expiries to one benchmark index per
exchange -- NSE kept NIFTY weekly and moved Bank Nifty to monthly.
NIFTY:     2026-08-18, 08-25, 09-01, 09-08, ... (weekly)
BANKNIFTY: 2026-08-25, 09-29, 10-27, 12-29, ... (monthly)

This is not a re-tuned copy of the same strategy -- a monthly condor is
~12 cycles/year instead of ~52, with a much wider expected move per
cycle. `choose_expiry_to_open` below still works mechanically (it just
asks the expiry list for "the first one with enough days left", and
Bank Nifty's list happens to be monthly), but the strike-selection
numbers a weekly condor was tuned around do NOT transfer.

CALIBRATION STATUS -- TESTED 2026-08-13, NET NEGATIVE, CLOSED
------------------------------------------------------------------------
The ORIGINAL placeholder here (SHORT_PREMIUM_MIN/MAX=150/250,
HEDGE_DISTANCE_POINTS=1000) was badly miscalibrated, not just imprecise
-- a direct chain inspection showed the reconstructed data's own
ATM+/-10-offset window edge still carries Rs 2-640+ of premium
depending on the point in the monthly cycle, nowhere near 150-250, so
it found almost no candidates (2 opens in a 90-day probe).

Grid-probed real chain data and found a workable calibration --
SHORT_PREMIUM_MIN/MAX=100/400, HEDGE_DISTANCE_POINTS=400 (the values
now set below) -- then ran the full 5-year baseline with real p90
costs. Result: NET NEGATIVE at every profit-target level tried
(baseline -Rs 133,835; PT=40% -Rs 118,479; PT=50% -Rs 86,094, the best
cell but still a loss; PT=60% -Rs 109,800). The exact fix that flipped
NIFTY's condor positive (profit-target + IV-rank gate together) only
has half its ingredient here -- there is no valid IV-rank equivalent
for Bank Nifty (see below) -- and a severe hedge-coverage gap (22,291
skips vs 149 real opens) is a real structural headwind NIFTY's condor
never faced. Full numbers in BACKLOG.md's 2026-08-13 entry.

CLOSED, not left open for more parameter tuning: the missing piece is a
genuinely new build (a Bank-Nifty-specific ATM-IV regime filter), not a
config sweep. The values below are kept at the real, workable-but-
losing calibration (not the original unreachable placeholder) so any
future work starts from an honest number.

MIN_IV_RANK_TO_OPEN is NOT carried over from the NIFTY condor's
validated IV-rank gate: India VIX (^INDIAVIX) measures NIFTY's OWN
implied volatility specifically, not Bank Nifty's -- reusing it here
would be gating Bank Nifty entries on the wrong instrument's IV. Left
disabled (None) until a Bank-Nifty-specific IV-rank measure exists
(built from Bank Nifty's own ATM IV history, not borrowed from NIFTY's
index).

DATA SOURCE: dhan_source directly with BANKNIFTY_UNDERLYING_SCRIP/_SEG
-- NOT resilient_source.py (hardcoded to NIFTY, no Bank Nifty path).
Dhan-only, no NSE/TradingView fallback, same limitation every other
Bank Nifty variant in this project already accepts.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_condor_banknifty.py
"""

import time
import logging
from datetime import datetime, time as dtime
from dataclasses import asdict

import dhan_source
import config_condor as ccfg

BANKNIFTY_SECURITY_ID = dhan_source.BANKNIFTY_UNDERLYING_SCRIP
BANKNIFTY_SEG = dhan_source.BANKNIFTY_UNDERLYING_SEG

# --- Bank Nifty overrides -- see module docstring's CALIBRATION STATUS.
# Patched BEFORE condor_tracker is imported: it snapshots ccfg.STATE_PATH/
# JOURNAL_PATH into its own module-level constants at import time (see
# its own source), so patching afterward would silently keep pointing at
# Nifty's files -- the exact hazard main_price_action_banknifty.py's
# docstring documents for its own tracker module.
ccfg.NIFTY_LOT_SIZE = 30
ccfg.SHORT_PREMIUM_MIN = 100.0
ccfg.SHORT_PREMIUM_MAX = 400.0
ccfg.HEDGE_DISTANCE_POINTS = 400
ccfg.MIN_IV_RANK_TO_OPEN = None   # see module docstring -- no valid Bank Nifty IV-rank measure yet
ccfg.STATE_PATH = "state/condor_position_banknifty.json"
ccfg.JOURNAL_PATH = "logs/condor_journal_banknifty.jsonl"

import condor_scanner
import condor_plan_generator
import condor_risk_checker
import condor_tracker
import trade_staging as staging

assert "banknifty" in str(condor_tracker.STATE_PATH).lower(), (
    "condor_tracker captured Nifty's STATE_PATH -- import order regressed, "
    "ccfg must be patched (done above) BEFORE `import condor_tracker`"
)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
# Same reasoning and same NOT-independently-re-verified-for-Bank-Nifty
# caveat as main_condor.py's own EXPIRY_SETTLEMENT_CUTOFF comment.
EXPIRY_SETTLEMENT_CUTOFF = dtime(15, 40)

from pathlib import Path
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"condor_banknifty_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("condor_banknifty")


def get_banknifty_expiry_list() -> list:
    return dhan_source.get_expiry_list(BANKNIFTY_SECURITY_ID, BANKNIFTY_SEG)


def get_banknifty_snapshot(expiry: str = None, must_include_strikes: set = None):
    return dhan_source.get_nifty_snapshot(
        expiry=expiry, must_include_strikes=must_include_strikes,
        symbol="BANKNIFTY", underlying_scrip=BANKNIFTY_SECURITY_ID, underlying_seg=BANKNIFTY_SEG,
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


def choose_expiry_to_open(expiry_list: list, min_days_out: int, today=None) -> str:
    """Same logic as main_condor.py's own -- see there for the reasoning.
    Mechanically identical for a monthly expiry list, just rolls whole
    months instead of weeks."""
    today = today or datetime.now().date()
    for expiry_str in expiry_list:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        if (expiry_date - today).days >= min_days_out:
            return expiry_str
    return None


def _tracked_strikes(position: dict) -> set:
    if not position or position.get("status") != "OPEN":
        return set()
    plan = position["plan"]
    return {plan["short_ce_strike"], plan["short_pe_strike"],
            plan["hedge_ce_strike"], plan["hedge_pe_strike"]}


def run_once(state: dict):
    protect = _tracked_strikes(state.get("position"))
    expiry_list = get_banknifty_expiry_list()
    nearest_expiry = expiry_list[0]
    snapshot = get_banknifty_snapshot(expiry=nearest_expiry, must_include_strikes=protect)
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"[{ts}] ({snapshot.source}) BANKNIFTY spot {snapshot.spot}")

    position = state.get("position")

    if position and position.get("status") == "OPEN":
        position = condor_tracker.update_position(state, snapshot)
        if position.get("status") != "OPEN":
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
        snapshot = get_banknifty_snapshot(expiry=expiry)

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
            f"Open Bank Nifty condor: sell {plan.short_ce_strike}CE @ {plan.short_ce_premium} / "
            f"sell {plan.short_pe_strike}PE @ {plan.short_pe_premium}, hedge with "
            f"{plan.hedge_ce_strike}CE @ {plan.hedge_ce_premium} / {plan.hedge_pe_strike}PE @ {plan.hedge_pe_premium}. "
            f"Net credit Rs {plan.net_credit_inr:,.0f}, max loss Rs {plan.max_loss_inr:,.0f}, "
            f"breakevens {plan.breakeven_lower}/{plan.breakeven_upper}."
        )
        record = staging.stage_and_maybe_auto_open(
            kind="condor_banknifty_open_candidate", detail=detail, note=f"expiry {expiry}",
            data=asdict(plan), auto_approve=ccfg.AUTO_APPROVE_NEW_POSITIONS,
            open_fn=condor_tracker.open_position, open_args=(state, plan),
        )
        if record["status"] == "EXECUTED":
            log.info("  AUTO-APPROVED and opened immediately.")
        else:
            log.info("  Staged for approval -- run approve_orders.py, then open_approved_condor.py once approved.")


def run_forever():
    state = condor_tracker.load_position()
    log.info("Bank Nifty condor strategy loop started. Ctrl+C to stop.")
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
        time.sleep(ccfg.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
