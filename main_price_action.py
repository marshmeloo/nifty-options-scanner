"""
Standalone live loop for the pure price-action strategy. Runs as its own
process, independent of main_live.py/main_condor.py/
main_directional_spread.py -- separate state (config_price_action.
STATE_PATH), separate journal, separate log file. Shares only the
data-fetching layer and the Dhan rate limiter with the other three.

What it does each cycle, for EACH pair in config_price_action.ACTIVE_PAIRS:
  - No open trade for this pair -> fetch that pair's HTF/LTF candles
    fresh (see _htf_ltf_for -- live has no need for the backtest's
    cross-day candle accumulation, since real history is always
    available on request), check price_structure.generate_signal(),
    scan/plan a candidate, and (if AUTO_APPROVE_NEW_POSITIONS) open it
    immediately, else stage it for manual review via approve_orders.py +
    open_approved_price_action.py.
  - Open trade for this pair -> price_action_tracker.update_positions
    marks it to market every cycle and closes it on target, stop, or its
    own real contract expiry.

NOTHING HERE PLACES A REAL BROKER ORDER, auto-approved or not -- see
trade_staging.py's own docstring. "Opening a position" only ever means
writing price_action_tracker's own state file.

See price_structure.py's docstring for the entry rule itself, and
BACKLOG.md's 2026-08-04 entry for the backtest evidence this was built
on: 19-24 distinct qualifying setups over 2 years, a 26% max drawdown on
a Rs 20k account walk, mildly positive but on a sample this project's
own standard treats as suggestive, not proven. Watch it the same way
every other strategy here was watched before being trusted.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_price_action.py
"""

import time
import logging
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import config_price_action as pcfg
import dhan_source
import price_action_plan_generator as pag
import price_action_scanner as pas
import price_action_tracker as pat
import price_structure as ps
import trade_staging as staging
from resilient_source import get_nifty_snapshot, get_nifty_intraday_candles

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
# NOT used as the entry cutoff for the "intraday" pair -- see
# RETAIL_TRADABLE_CLOSE below. Kept at the nominal close here because
# "daily_hourly" positions are meant to be held across days and still
# need marking to market right up to real close, even if the final
# stretch's prices are lower quality (see RETAIL_TRADABLE_CLOSE's own
# docstring for why that stretch is unreliable) -- a stale mark is a
# lesser problem for a multi-day hold than for a same-session strategy.

# Verified directly against 2026-08-04's and 2026-08-03's recorded
# snapshots: spot froze at the exact same value for 10+ consecutive
# cycles starting ~15:15 both days (see main_live.py's MARKET_CLOSE
# comment for the full evidence -- same finding, same root cause).
# The "intraday" pair (config_price_action.TIMEFRAME_PAIRS) is a
# same-session strategy by design (5min/15min, resolved same day), so a
# new position opened after this point would need to exit before close
# using exactly the unreliable prices this cutoff exists to avoid.
# "daily_hourly" is unaffected -- its holds already span days, so
# opening it right before close is no different from opening it any
# other time relative to its own multi-day resolution.
RETAIL_TRADABLE_CLOSE = dtime(15, 15)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"price_action_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("price_action")


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _htf_ltf_for(pair: str, now: datetime = None) -> tuple:
    """
    Real candles for one timeframe pair, fetched fresh each cycle.

    "daily_hourly": HTF is real daily bars (today's own still-forming bar
    excluded -- a zone must be built from COMPLETED days, same rule the
    backtest's daily_bars accumulation enforced). LTF is a wide window of
    5-min candles resampled to 60-min, since a single day's session
    (~6 hourly bars) falls well short of generate_signal's 12-candle
    minimum -- exactly the bug shadow_price_action.py's own history
    fix addressed; live sidesteps it entirely by just fetching enough
    real days up front rather than accumulating across restarts.

    "intraday": today's session only, same shape as every other
    strategy's live candle fetch in this project.
    """
    now = now or datetime.now()
    tf = pcfg.TIMEFRAME_PAIRS[pair]

    if pair == "daily_hourly":
        daily = dhan_source.get_nifty_daily_candles(days_back=pcfg.HTF_DAILY_LOOKBACK_DAYS)
        htf = [c for c in daily if c.timestamp.date() < now.date()]

        from_date = (now - timedelta(days=pcfg.LTF_HOURLY_LOOKBACK_DAYS)).strftime("%Y-%m-%d 09:15:00")
        to_date = now.strftime("%Y-%m-%d %H:%M:%S")
        five_min = get_nifty_intraday_candles(interval="5", from_date=from_date, to_date=to_date)
        ltf = ps.resample_candles(five_min, tf["ltf_interval"], source_minutes=5)
        return htf, ltf

    five_min = get_nifty_intraday_candles(interval="5")  # today's session so far
    htf = ps.resample_candles(five_min, tf["htf_interval"], source_minutes=5)
    return htf, five_min


def _handle_pair(pair: str, state: dict, snapshot, now: datetime):
    if pat.has_open_trade(state, pair):
        return  # already marked to market in run_once via update_positions

    if pair == "intraday" and now.time() >= RETAIL_TRADABLE_CLOSE:
        log.info(f"  [{pair}] past {RETAIL_TRADABLE_CLOSE.strftime('%H:%M')} -- "
                 f"no new same-session entries this late (see RETAIL_TRADABLE_CLOSE)")
        return

    try:
        htf, ltf = _htf_ltf_for(pair, now)
    except Exception as e:
        log.info(f"  [{pair}] candle fetch failed this cycle: {e}")
        return

    try:
        signal = ps.generate_signal(htf, ltf)
    except Exception as e:
        log.info(f"  [{pair}] signal generation error: {e}")
        return

    if signal is None:
        log.info(f"  [{pair}] no signal this cycle (htf={len(htf)} ltf={len(ltf)} candles)")
        return

    setups = pas.scan(snapshot, signal)
    if not setups:
        log.info(f"  [{pair}] {signal.direction} signal, but no candidate cleared the premium/liquidity screen")
        return
    setup = setups[0]

    try:
        plan = pag.build_plan(snapshot, setup, signal)
    except ValueError as e:
        log.info(f"  [{pair}] plan build failed: {e}")
        return

    if plan.lots <= 0:
        log.info(f"  [{pair}] candidate {setup.strike}{setup.option_type} sized to 0 lots "
                 f"(risk budget too small) -- skipped")
        return

    log.info(
        f"  [{pair}] Candidate: BUY {setup.strike}{setup.option_type} @ {plan.entry}, "
        f"stop {plan.stop}, target {plan.target} ({pcfg.TARGET_RR:g}R)"
    )
    for r in signal.reasons:
        log.info(f"      - {r}")

    detail = (
        f"[{pair}] Buy {setup.strike}{setup.option_type} @ {plan.entry}, "
        f"stop {plan.stop}, target {plan.target}. "
        f"{signal.reasons[0] if signal.reasons else ''}"
    )
    record = staging.stage_and_maybe_auto_open(
        kind="price_action_open_candidate", detail=detail, note=pair,
        data={**asdict(plan), "pair": pair},
        auto_approve=pcfg.AUTO_APPROVE_NEW_POSITIONS,
        open_fn=pat.open_position, open_args=(state, setup, plan, pair, snapshot),
    )
    if record["status"] == "EXECUTED":
        log.info(f"  [{pair}] AUTO-APPROVED and opened immediately.")
    else:
        log.info(f"  [{pair}] Staged for approval -- run approve_orders.py, "
                 f"then open_approved_price_action.py once approved.")


def run_once(state: dict):
    protect = pat.tracked_strikes(state)
    snapshot = get_nifty_snapshot(must_include_strikes=protect)
    # Derived from the snapshot's own timestamp, not a separate
    # datetime.now() call -- ties the RETAIL_TRADABLE_CLOSE gate to the
    # same instant the market data was actually fetched, not a
    # slightly-later wall-clock read.
    now = snapshot.timestamp
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"[{ts}] ({snapshot.source}) NIFTY spot {snapshot.spot}")

    closed = pat.update_positions(state, snapshot)
    for t in closed:
        pnl = t.get("pnl_inr_net", t["pnl_inr"])
        log.info(
            f"  [{t['pair']}] CLOSED {t['strike']}{t['option_type']}: {t['outcome']} "
            f"({t['exit_reason']}) entry {t['entry']} -> exit {t['exit_ltp']}  net Rs {pnl:,.0f}"
        )

    for t in pat.open_trades(state):
        r = pat.r_multiple(t, t.get("current_ltp"))
        r_note = f"{r:+.2f}R" if r is not None else "n/a"
        log.info(
            f"  [{t['pair']}] Open: {t['strike']}{t['option_type']} entry {t['entry']} "
            f"-> current {t.get('current_ltp', 'n/a')} ({r_note}), target {t['target']}, stop {t['stop']}"
        )

    for pair in pcfg.ACTIVE_PAIRS:
        _handle_pair(pair, state, snapshot, now)


def run_forever():
    state = pat.load_state()
    log.info("Price-action strategy loop started. Ctrl+C to stop.")
    while True:
        if market_is_open():
            try:
                run_once(state)
            except Exception as e:
                log.info(f"  Error this cycle (will retry next cycle): {e}")
        else:
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] Market closed, sleeping...")
        time.sleep(pcfg.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
