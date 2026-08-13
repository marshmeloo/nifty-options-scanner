"""
Standalone live loop for the momentum strategy on Bank Nifty -- forward
paper-trading only, same "NOTHING HERE PLACES A REAL BROKER ORDER"
guarantee as main_live.py. Runs as its OWN process, completely
independent of main_live.py: own state, own journal, own log file, own
decision log, own snapshot recordings.

WHY A SEPARATE FILE rather than parameterizing main_live.py: this
project's own precedent for "a strategy variant needs its own
everything" is one process per variant, sharing only the low-level
data-fetching layer -- see main_price_action_banknifty.py's docstring
for the same reasoning, and BACKLOG.md's 2026-08-04 "Run every strategy
on Bank Nifty" entry for why a shared cross-strategy abstraction was
considered and set aside in favour of this simpler, already-proven
pattern.

DATA SOURCE: calls dhan_source directly with BANKNIFTY_UNDERLYING_SCRIP/
_SEG (symbol="BANKNIFTY") -- NOT resilient_source.py, which is hardcoded
to NIFTY with no Bank Nifty path at all. This means NO NSE/TradingView
fallback for Bank Nifty (Dhan-only), same limitation
main_price_action_banknifty.py already accepted.

CONFIG OVERRIDE MECHANISM: `config` (the SAME shared module main.py,
scanner.py, plan_generator.py etc. all import) is patched with Bank
Nifty's values BEFORE scanner/plan_generator/trade_tracker are used.
Safe ONLY because this runs as its own OS process -- scanner.py and
plan_generator.py read config.PREMIUM_MIN/MAX/NIFTY_LOT_SIZE live on
every call (confirmed by reading the source: no module-level copies are
taken at import time), so patching the shared module here can never
leak into a same-process NIFTY caller because there isn't one.
trade_tracker.py, snapshot_recorder.py, decision_log.py, and
market_regime.py each hardcode their own state/journal/baseline paths
(not config-driven), so those are patched directly as module attributes
after import instead.

CALIBRATION STATUS -- VALIDATED 2026-08-13, ADDED TO start_trading.ps1
------------------------------------------------------------------------
PREMIUM_MIN/MAX = 300/800 (this file's original placeholder, carried
over from main_price_action_banknifty.py's own real 2026-08-06 sweep)
was swept against the full backfilled Bank Nifty history (1,244 days,
2021-08-04..2026-08-11) alongside several other bands -- see
BACKLOG.md's 2026-08-13 entry for the full numbers. 300-800 held up
well (2nd-best of 10 bands tried) and, critically, was tested against 5
INDEPENDENT ~1-year periods (not just an aggregate or a single in-
sample/out-of-sample split, which initially looked regime-dependent --
one OOS split had 98% of its profit concentrated in a single 8-month
stretch): every one of the 5 years is independently positive
(Rs 4.6L-8.0L net each), and trade-level concentration is low (top-3
trades = 2-8% of total). This clears the same walk-forward +
outlier-concentration bar the NIFTY condor needed before going live,
and is now part of the daily automation (automation/start_trading.ps1).

Real, standing caveats (do not lose sight of these just because the
sweep passed): the reconstructed historical data has no bid/ask, so
shadow.py falls back to LTP fills -- the same OPTIMISTIC bias every
backtest against historical_source.py data carries, NIFTY's included.
Only ~8-9% of trades ever hit the exact target; most of the profit
comes from EOD_CLOSE trades averaging positive rather than genuine
target hits, a materially different profile from NIFTY's own momentum
character and one with no live track record yet to compare against --
this is the first time this strategy has ever actually traded Bank
Nifty. STRIKE_RANGE_POINTS = 2000 remains an unswept proportional
scale of NIFTY's 800pt default, not independently measured.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_live_banknifty.py
"""

import functools
import time
import logging
from datetime import datetime, date, time as dtime
from pathlib import Path

import config
import dhan_source

# --- Bank Nifty config overrides, patched before any strategy module is
# used. See module docstring's CALIBRATION STATUS for where these numbers
# come from and what still needs independent validation.
BANKNIFTY_SECURITY_ID = dhan_source.BANKNIFTY_UNDERLYING_SCRIP
BANKNIFTY_SEG = dhan_source.BANKNIFTY_UNDERLYING_SEG

config.NIFTY_LOT_SIZE = 30       # confirmed fresh from Dhan's instrument master, 2026-08-12
config.PREMIUM_MIN = 300.0
config.PREMIUM_MAX = 800.0
config.STRIKE_RANGE_POINTS = 2000

# market_regime.py calls dhan_source.get_nifty_daily_candles() internally
# with no symbol/security_id parameter of its own -- binding Bank Nifty's
# scrip/seg here means its "what's a normal day's range" baseline is
# built from BANK NIFTY's own daily history, not Nifty's, without having
# to modify market_regime.py itself.
dhan_source.get_nifty_daily_candles = functools.partial(
    dhan_source.get_nifty_daily_candles,
    security_id=BANKNIFTY_SECURITY_ID, exchange_seg=BANKNIFTY_SEG,
)

from scanner import scan, compute_market_bias, tag_bias_conflicts
from plan_generator import build_plan
from risk_checker import check
from price_action import analyze_with_context, compute_atr
import trade_tracker as tt
import news_source
import opening_gap
import decision_log
import volume_profile as vp
import anchored_vwap as avwap
import snapshot_recorder
import logic_version
import workspace
import market_regime

assert tt.JOURNAL_PATH.name == "trade_journal.jsonl", (
    "trade_tracker captured Nifty's JOURNAL_PATH -- import order regressed, "
    "config must be patched (done above) but trade_tracker's own paths still "
    "need patching AFTER this import, which the block below does"
)
tt.OPEN_TRADES_PATH = tt.STATE_DIR / "open_trades_banknifty.json"
tt.JOURNAL_PATH = tt.LOG_DIR / "trade_journal_banknifty.jsonl"
snapshot_recorder.SNAPSHOT_DIR = Path(__file__).parent / "logs" / "snapshots_banknifty"
snapshot_recorder.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
decision_log.LOG_PATH = Path(__file__).parent / "logs" / "decision_log_banknifty.jsonl"
market_regime.BASELINE_PATH = market_regime.STATE_DIR / "regime_baseline_banknifty.json"

POLL_INTERVAL_SECONDS = 30
MARKET_OPEN = dtime(9, 15)
# Inherited from main_live.py's own directly-verified 15:15 finding
# (spot froze solid from ~15:15 in recorded NIFTY snapshots -- see that
# file's own comment). The underlying mechanism (continuous cash trading
# ending at 15:15, options getting a separate 15:35-15:40 reactive
# window) is exchange-wide, not NIFTY-specific, so this SHOULD transfer
# -- but it has not been independently re-verified against Bank Nifty's
# own recorded data. Check this once real Bank Nifty snapshots exist.
MARKET_CLOSE = dtime(15, 15)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"banknifty_scan_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("banknifty_momentum")

_news_cache = {"flags": None, "fetched_at": None}


def get_cached_news_flags():
    now = datetime.now()
    stale = (
        _news_cache["flags"] is None
        or _news_cache["fetched_at"] is None
        or (now - _news_cache["fetched_at"]).total_seconds() >= config.NEWS_CACHE_MINUTES * 60
    )
    if stale:
        _news_cache["flags"] = news_source.get_news_flags()
        _news_cache["fetched_at"] = now
    return _news_cache["flags"]


def get_banknifty_snapshot(must_include_strikes: set = None):
    """Dhan only -- no NSE/TradingView fallback for Bank Nifty, same
    limitation main_price_action_banknifty.py already accepted."""
    return dhan_source.get_nifty_snapshot(
        must_include_strikes=must_include_strikes,
        symbol="BANKNIFTY",
        underlying_scrip=BANKNIFTY_SECURITY_ID,
        underlying_seg=BANKNIFTY_SEG,
    )


def get_banknifty_nearest_expiry() -> str:
    return dhan_source.get_nearest_expiry(BANKNIFTY_SECURITY_ID, BANKNIFTY_SEG)


def get_banknifty_intraday_candles(interval: str = None, from_date: str = None, to_date: str = None):
    return dhan_source.get_nifty_intraday_candles(
        interval=interval, from_date=from_date, to_date=to_date,
        security_id=BANKNIFTY_SECURITY_ID, exchange_seg=BANKNIFTY_SEG,
    )


def _tracked_strikes(state: dict) -> set:
    return {t["strike"] for t in state.get("trades", [])}


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def run_once(expiry: str, state: dict):
    snapshot = get_banknifty_snapshot(must_include_strikes=_tracked_strikes(state))
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"\n[{ts}] ({snapshot.source}) BANKNIFTY spot {snapshot.spot}, VWAP-proxy {snapshot.vwap}, PCR {snapshot.pcr}")

    oi = snapshot.oi_analysis
    if oi:
        log.info(
            f"  OI: max pain {oi.max_pain_strike} ({oi.max_pain_distance_pct:+.2f}% from spot) | "
            f"call wall {oi.call_wall_strike} (OI {oi.call_wall_oi:,}) | "
            f"put wall {oi.put_wall_strike} (OI {oi.put_wall_oi:,}) | "
            f"net delta OI {oi.net_delta_oi:+,} ({oi.net_delta_oi_bias})"
        )

    news_flags = get_cached_news_flags()
    news_risk = news_flags["risk"]
    if news_risk["level"] == "elevated":
        log.info(
            f"  NEWS RISK: elevated (categories: {', '.join(news_risk['categories_hit'])}) -- "
            f"{news_risk['headline_count']} matching headline(s)"
        )

    gap = opening_gap.capture_opening_gap()
    if gap.get("banknifty"):
        g = gap["banknifty"]
        log.info(f"  Opening gap: Bank Nifty {g['gap_points']:+.1f} pts ({g['gap_pct']:+.2f}%)")

    vol_profile = {"error": "not computed"}
    anchored_vwap_ctx = {"error": "not computed"}
    atr = None
    regime = {}
    try:
        candles = get_banknifty_intraday_candles()
        price_levels, context = analyze_with_context(candles)
        atr = compute_atr(candles)
        snapshot_recorder.record(snapshot, candles, logic_version.compute())
        regime = market_regime.classify(candles)
        if regime:
            log.info(f"  {market_regime.describe(regime)}")
        if atr is not None:
            log.info(f"  ATR({config.ATR_PERIOD}): {atr} pts -- stop/target sized from this")
        if price_levels:
            log.info(f"  Structure: {len(price_levels)} live OB/FVG/S-R/sweep/breakout/pullback levels (stale/mitigated pruned)")
        log.info(
            f"  Trend: {context.trend} | RSI: {context.rsi} ({context.rsi_state}) | "
            f"ROC: {context.roc_pct}% | Volume: {context.volume_ratio}x avg"
        )

        vol_profile = vp.get_volume_profile_context(candles=candles)
        if "error" not in vol_profile:
            log.info(
                f"  Volume profile: POC {vol_profile['poc']}  |  "
                f"Value Area {vol_profile['value_area_low']}-{vol_profile['value_area_high']} "
                f"({vol_profile['value_area_captured_pct']}% of volume)"
            )

        anchored_vwap_ctx = avwap.get_anchored_vwap_context(candles)
        if "error" not in anchored_vwap_ctx:
            log.info(
                "  Anchored VWAP: "
                + " | ".join(
                    f"{a['label']} {a['vwap']} ({a['position']}, {a['distance_pct']:+.2f}%)"
                    for a in anchored_vwap_ctx["anchors"]
                )
            )
    except Exception as e:
        log.info(f"  Price-action fetch failed this cycle, scanning without it: {e}")
        price_levels, context = [], None

    closed = tt.update_open_trades(state, snapshot)
    for trade in closed:
        log.info(
            f"  [TRADE CLOSED: {trade['outcome']}] {trade['strike']} {trade['option_type']}  "
            f"entry {trade['entry']} -> exit {trade['exit_ltp']}  "
            f"pnl {trade['pnl_pct']:+.1f}% (Rs {trade['pnl_inr']:+,.0f})"
        )
        log.info(f"    lesson: {trade['lesson']}")

    if state["trades"]:
        log.info(f"  Currently tracking {len(state['trades'])} open trade(s):")
        for trade in state["trades"]:
            max_seen = trade.get("max_ltp_seen")
            max_seen_note = f"  (best seen: {max_seen})" if max_seen is not None else ""
            current_r = tt.r_multiple(trade, trade.get("current_ltp"))
            r_note = ""
            if current_r is not None:
                hit = sorted(float(m) for m in trade.get("rr_milestones_hit", {}))
                r_note = f"  [{current_r:+.2f}R now, peak {trade.get('max_r_seen', 0):+.2f}R"
                r_note += f", hit {max(hit):g}R]" if hit else ", no milestone yet]"
            log.info(
                f"    {trade['strike']} {trade['option_type']}  entry {trade['entry']} "
                f"target {trade['target']} stop {trade['stop']}  "
                f"current {trade.get('current_ltp', '?')}  "
                f"running pnl {trade.get('running_pnl_pct', 0):+.1f}% (Rs {trade.get('running_pnl_inr', 0):+,.0f})"
                f"{max_seen_note}{r_note}"
            )

    setups = scan(snapshot, price_levels=price_levels, context=context)
    if not setups:
        log.info("  No setups flagged this cycle.")
        tt.save_open_trades(state)
        return snapshot

    current_open_exposure_pct, current_daily_loss_pct = tt.compute_risk_state(state)
    log.info(
        f"  Risk state: exposure {current_open_exposure_pct:.2f}% of {config.MAX_TOTAL_EXPOSURE_PCT}% cap, "
        f"day P&L drawdown {current_daily_loss_pct:.2f}% of {config.MAX_DAILY_LOSS_PCT}% breaker"
    )
    if current_daily_loss_pct >= config.MAX_DAILY_LOSS_PCT:
        log.info("  DAILY LOSS BREAKER TRIPPED -- no new trades will be opened for the rest of today.")

    bias_label, bias_score, bias_reasons = compute_market_bias(snapshot, context)
    log.info(f"  Market bias: {bias_label} (score {bias_score})  [{', '.join(bias_reasons) if bias_reasons else 'no strong signal'}]")

    results = []
    for setup in setups:
        plan = build_plan(snapshot, setup, atr=atr)
        verdict = check(
            plan,
            current_open_exposure_pct=current_open_exposure_pct,
            current_daily_loss_pct=current_daily_loss_pct,
            news_risk_level=news_risk["level"],
        )
        if verdict.decision == "REJECTED" and plan.lots == 0:
            continue
        results.append((setup, plan, verdict))

    tag_bias_conflicts(results)
    results.sort(key=lambda r: r[0].score, reverse=True)

    new_trade = None
    if state["opened_today"] >= config.MAX_NEW_TRADES_PER_DAY:
        log.info(f"  Daily trade cap reached ({state['opened_today']}/{config.MAX_NEW_TRADES_PER_DAY}). Not opening new trades today.")
    else:
        new_trade = tt.try_open_new_trade(results, state, snapshot, bias_label, bias_score)
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
            best_setup, best_plan, best_verdict = results[0]
            adjusted_score, learn_notes = tt.apply_learned_adjustment(best_setup.score, best_setup.reasons)
            conviction_bar, expiry_blocked = tt.expiry_day_rules(best_setup.expiry, snapshot.timestamp)
            if best_verdict.decision != "APPROVED":
                reason = f"risk check: {'; '.join(best_verdict.reasons) if best_verdict.reasons else best_verdict.decision}"
            elif expiry_blocked:
                reason = expiry_blocked
            elif tt.is_repeat_of_stopped_plan(state, best_setup.strike, best_setup.option_type, best_plan.entry):
                reason = f"repeat of a plan already stopped out today near entry {best_plan.entry}"
            elif tt.is_direction_chase(state, best_setup.option_type, snapshot.timestamp):
                reason = f"chasing {best_setup.option_type} -- a same-direction trade stopped out within the last {config.DIRECTION_CHASE_COOLDOWN_MINUTES}min"
            elif adjusted_score < conviction_bar:
                reason = f"adjusted score {adjusted_score} < bar {conviction_bar}"
                if learn_notes:
                    reason += f" ({'; '.join(learn_notes)})"
            else:
                reason = "already tracking this strike/type"
            log.info(
                f"  No new trade this cycle ({state['opened_today']}/{config.MAX_NEW_TRADES_PER_DAY} used today). "
                f"Highest candidate: {best_setup.strike} {best_setup.option_type} raw score {best_setup.score} "
                f"-> adjusted {adjusted_score}  ({reason})"
            )

    decision_log.log_cycle(
        snapshot, context, bias_label, bias_score, bias_reasons,
        None, None, news_risk, gap, results, state, new_trade,
        volume_profile=vol_profile, anchored_vwap=anchored_vwap_ctx, market_regime=regime,
    )

    tt.save_open_trades(state)
    return snapshot


def force_close_all(state: dict, expiry: str, last_snapshot=None):
    if not state["trades"]:
        return
    snapshot = last_snapshot if last_snapshot is not None else get_banknifty_snapshot(
        must_include_strikes=_tracked_strikes(state))
    closed = tt.force_close_end_of_day(state, snapshot)
    for trade in closed:
        log.info(
            f"  [EOD CLOSE] {trade['strike']} {trade['option_type']}  "
            f"entry {trade['entry']} -> exit {trade['exit_ltp']}  "
            f"pnl {trade['pnl_pct']:+.1f}% (Rs {trade['pnl_inr']:+,.0f})"
            + ("  [ESTIMATED -- no closing quote available]" if trade.get("exit_price_estimated") else "")
        )
        log.info(f"    lesson: {trade['lesson']}")
    tt.save_open_trades(state)


def check_open_trades_fast(state: dict, expiry: str):
    if not state["trades"]:
        return
    try:
        snapshot = get_banknifty_snapshot(must_include_strikes=_tracked_strikes(state))
    except Exception as e:
        log.info(f"  [fast check] snapshot fetch failed, will retry next fast check: {e}")
        return

    closed = tt.update_open_trades(state, snapshot)
    for trade in closed:
        log.info(
            f"  [FAST CHECK - TRADE CLOSED: {trade['outcome']}] {trade['strike']} {trade['option_type']}  "
            f"entry {trade['entry']} -> exit {trade['exit_ltp']}  "
            f"pnl {trade['pnl_pct']:+.1f}% (Rs {trade['pnl_inr']:+,.0f})"
        )
        log.info(f"    lesson: {trade['lesson']}")
    tt.save_open_trades(state)


def run_forever():
    ws = workspace.role()
    git = workspace.git_state()
    log.info(f"Workspace: {ws.upper()}  |  git {git['branch']} @ {git['commit']}"
             + ("  [UNCOMMITTED CHANGES]" if git["dirty"] else ""))
    if ws == workspace.DEVELOPMENT:
        log.info("  WARNING: running a live session from the DEVELOPMENT checkout. Trades "
                 "journalled here will not be in production's record.")
    log.info("  CALIBRATION NOTE: premium band / lot size / strike range are a reasoned "
             "starting point, not independently swept for momentum -- see this file's own "
             "module docstring before trusting results from this session.")

    log.info("Fetching nearest Bank Nifty expiry...")
    expiry = get_banknifty_nearest_expiry()
    log.info(f"Tracking expiry: {expiry}. Polling every {POLL_INTERVAL_SECONDS}s during market hours.")
    log.info(f"Max {config.MAX_NEW_TRADES_PER_DAY} new trades/day, conviction bar {config.MIN_CONVICTION_SCORE_TO_TRACK}.")
    log.info(tt.summarize_recent_lessons())

    state = tt.load_open_trades()

    needs_recovery = state.get("_stale_from_previous_session") or (
        state.get("trades") and not market_is_open()
    )

    if needs_recovery:
        reason = (
            f"from a previous session ({state.get('date')})" if state.get("_stale_from_previous_session")
            else "from earlier today, but the market is already closed and this process is only starting now"
        )
        log.info(
            f"  WARNING: found {len(state['trades'])} still-OPEN trade(s) {reason} that never got settled -- "
            f"recovering and journaling them now using each trade's last known live price as the exit."
        )
        try:
            recovery_snapshot = get_banknifty_snapshot(must_include_strikes=_tracked_strikes(state))
        except Exception as e:
            log.info(f"  Could not fetch a fresh snapshot for recovery ({e}) -- using each trade's last known price instead.")
            recovery_snapshot = None
        recovered = tt.settle_stale_trades(state, snapshot=recovery_snapshot)
        for trade in recovered:
            log.info(
                f"    [RECOVERED] {trade['strike']} {trade['option_type']}  entry {trade['entry']} -> "
                f"exit {trade['exit_ltp']}  pnl {trade['pnl_pct']:+.1f}% (Rs {trade['pnl_inr']:+,.0f})"
                + ("  [ESTIMATED]" if trade["exit_price_estimated"] else "")
            )
        was_same_day = not state.get("_stale_from_previous_session")
        state = {
            "date": date.today().isoformat(),
            "trades": [],
            "opened_today": state.get("opened_today", 0) if was_same_day else 0,
        }
        tt.save_open_trades(state)

    was_open_last_cycle = False
    last_good_snapshot = None
    last_full_cycle_at = 0.0

    while True:
        is_open = market_is_open()

        if is_open:
            now = time.monotonic()
            if now - last_full_cycle_at >= POLL_INTERVAL_SECONDS:
                try:
                    last_good_snapshot = run_once(expiry, state)
                except Exception as e:
                    log.info(f"  Error this cycle (will retry next cycle): {e}")
                last_full_cycle_at = now
            else:
                try:
                    check_open_trades_fast(state, expiry)
                except Exception as e:
                    log.info(f"  [fast check] error (will retry next fast check): {e}")
            was_open_last_cycle = True
        else:
            if was_open_last_cycle and state["trades"]:
                log.info("  Market just closed — settling any still-open trades.")
                try:
                    force_close_all(state, expiry, last_snapshot=last_good_snapshot)
                except Exception as e:
                    log.info(f"  Could not settle open trades cleanly: {e}")
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] Market closed, sleeping...")
            was_open_last_cycle = False

        time.sleep(config.FAST_CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
