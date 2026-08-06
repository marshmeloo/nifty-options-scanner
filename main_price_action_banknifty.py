"""
Standalone live loop for the price-action strategy on Bank Nifty --
forward paper-trading only, tracking real decisions against real data
before any live-capital decision is made. Same "NOTHING HERE PLACES A
REAL BROKER ORDER" guarantee as main_price_action.py.

WHY A SEPARATE FILE rather than parameterizing main_price_action.py:
this project's own precedent for "a strategy variant needs its own
everything" is one process per variant, sharing only the low-level
data-fetching layer (see main_price_action.py's own docstring) -- see
also BACKLOG.md's 2026-08-06 Bank Nifty backtest entry for why this
needed a real second config, not a shared one.

CONFIG OVERRIDE MECHANISM: config_price_action.py is imported and
monkeypatched with Bank Nifty's calibrated values BEFORE
price_action_scanner/plan_generator/tracker are imported. This is safe
ONLY because this runs as its OWN OS process, entirely separate from
main_price_action.py's -- price_action_scanner.py and
price_action_plan_generator.py read pcfg.PREMIUM_MIN/MAX etc. live on
every call (safe to patch any time), but price_action_tracker.py
snapshots pcfg.STATE_PATH/JOURNAL_PATH into its OWN module-level
constants AT IMPORT TIME -- patching pcfg after price_action_tracker is
already imported would silently keep pointing at Nifty's files. Import
order below is deliberate: config_price_action first and patched, every
other price_action_* module after.

CALIBRATION SOURCE (2026-08-06 investigation, see BACKLOG.md):
  - PREMIUM_MIN/MAX = 300/800: Bank Nifty's real premiums are far higher
    than Nifty's tuned 40-200 band (outermost reachable strike still
    prices ~342). Swept 18 band combinations on 5 years of real data;
    this range sits in the flat, non-overfit middle rather than the
    nominal-best 200-500 edge (which showed no real peak vs. noise).
  - ACTIVE_PAIRS = ["intraday"] only: the "daily_hourly" pair stayed net
    NEGATIVE even on the contamination-filtered clean subset (-0.23R,
    52 trades) -- a real underperformance, not a data artifact. Only
    "intraday" cleared the bar (+1.38R clean, 38 trades) worth watching
    forward.
  - LOT_SIZE = 30, confirmed fresh from Dhan's own instrument master
    (2026-08-06), NOT assumed from Nifty's 65.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_price_action_banknifty.py
"""

import time
import logging
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import config_price_action as pcfg

pcfg.PREMIUM_MIN = 300.0
pcfg.PREMIUM_MAX = 800.0
pcfg.NIFTY_LOT_SIZE = 30  # yes, still spelled NIFTY_LOT_SIZE -- see price_action_tracker.py
pcfg.ACTIVE_PAIRS = ["intraday"]
pcfg.STATE_PATH = "state/price_action_position_banknifty.json"
pcfg.JOURNAL_PATH = "logs/price_action_journal_banknifty.jsonl"

import dhan_source
import price_action_plan_generator as pag
import price_action_scanner as pas
import price_action_tracker as pat
import price_structure as ps
import trade_staging as staging

assert str(pat.STATE_PATH).endswith("price_action_position_banknifty.json"), (
    "price_action_tracker captured Nifty's STATE_PATH -- import order regressed, "
    "config_price_action must be patched before price_action_tracker is imported"
)

BANKNIFTY_SECURITY_ID = dhan_source.BANKNIFTY_UNDERLYING_SCRIP
BANKNIFTY_SEG = dhan_source.BANKNIFTY_UNDERLYING_SEG

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# Same reasoning as main_price_action.py's RETAIL_TRADABLE_CLOSE -- kept
# identical rather than re-derived, since the spot-freeze behaviour
# documented there is a Dhan feed characteristic, not Nifty-specific.
RETAIL_TRADABLE_CLOSE = dtime(15, 15)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"price_action_banknifty_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("price_action_banknifty")


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def get_banknifty_snapshot(must_include_strikes: set = None):
    """
    Dhan only -- no NSE/TradingView fallback for Bank Nifty yet (unlike
    Nifty's resilient_source.py three-tier chain). A Dhan outage skips
    that cycle via _handle_pair's existing try/except, same as any other
    transient fetch failure; it does not fail over to a second source.
    """
    return dhan_source.get_nifty_snapshot(
        must_include_strikes=must_include_strikes,
        symbol="BANKNIFTY",
        underlying_scrip=BANKNIFTY_SECURITY_ID,
        underlying_seg=BANKNIFTY_SEG,
    )


def get_banknifty_intraday_candles(interval: str = None, from_date: str = None, to_date: str = None):
    return dhan_source.get_nifty_intraday_candles(
        interval=interval, from_date=from_date, to_date=to_date,
        security_id=BANKNIFTY_SECURITY_ID, exchange_seg=BANKNIFTY_SEG,
    )


def _htf_ltf_for(pair: str, now: datetime = None) -> tuple:
    """Same shape as main_price_action.py's own _htf_ltf_for -- see there
    for the reasoning on daily_hourly vs intraday candle fetching. Bank
    Nifty only runs "intraday" (see module docstring), so the
    daily_hourly branch is unused but kept for parity/future use."""
    now = now or datetime.now()
    tf = pcfg.TIMEFRAME_PAIRS[pair]

    if pair == "daily_hourly":
        daily = dhan_source.get_nifty_daily_candles(
            days_back=pcfg.HTF_DAILY_LOOKBACK_DAYS,
            security_id=BANKNIFTY_SECURITY_ID, exchange_seg=BANKNIFTY_SEG,
        )
        htf = [c for c in daily if c.timestamp.date() < now.date()]

        from_date = (now - timedelta(days=pcfg.LTF_HOURLY_LOOKBACK_DAYS)).strftime("%Y-%m-%d 09:15:00")
        to_date = now.strftime("%Y-%m-%d %H:%M:%S")
        five_min = get_banknifty_intraday_candles(interval="5", from_date=from_date, to_date=to_date)
        ltf = ps.resample_candles(five_min, tf["ltf_interval"], source_minutes=5)
        return htf, ltf

    five_min = get_banknifty_intraday_candles(interval="5")  # today's session so far
    htf = ps.resample_candles(five_min, tf["htf_interval"], source_minutes=5)
    return htf, five_min


def _handle_pair(pair: str, state: dict, snapshot, now: datetime):
    if pat.has_open_trade(state, pair):
        return

    if pair == "intraday" and now.time() >= RETAIL_TRADABLE_CLOSE:
        log.info(f"  [{pair}] past {RETAIL_TRADABLE_CLOSE.strftime('%H:%M')} -- "
                 f"no new same-session entries this late")
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
        kind="price_action_open_candidate_banknifty", detail=detail, note=pair,
        data={**asdict(plan), "pair": pair},
        auto_approve=pcfg.AUTO_APPROVE_NEW_POSITIONS,
        open_fn=pat.open_position, open_args=(state, setup, plan, pair, snapshot),
    )
    if record["status"] == "EXECUTED":
        log.info(f"  [{pair}] AUTO-APPROVED and opened immediately (paper).")
    else:
        log.info(f"  [{pair}] Staged for approval.")


def run_once(state: dict):
    protect = pat.tracked_strikes(state)
    snapshot = get_banknifty_snapshot(must_include_strikes=protect)
    now = snapshot.timestamp
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"[{ts}] ({snapshot.source}) BANKNIFTY spot {snapshot.spot}")

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
    log.info("Bank Nifty price-action paper-trade loop started (Ctrl+C to stop). "
             f"Pairs: {pcfg.ACTIVE_PAIRS}, band Rs {pcfg.PREMIUM_MIN:g}-{pcfg.PREMIUM_MAX:g}, "
             f"lot size {pcfg.NIFTY_LOT_SIZE}.")
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
