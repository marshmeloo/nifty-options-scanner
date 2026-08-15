"""
Standalone live loop for the Sentinel v1.1-dev candidate on NIFTY --
forward PAPER-TRACKING only, same "NOTHING HERE PLACES A REAL BROKER
ORDER" guarantee as main_live.py. Runs as its OWN process, completely
independent of main_live.py (Anchor v1.0): own state, own journal, own
log file, own decision log. See STRATEGY_VERSIONS.md for the full
Anchor/Sentinel registry and promotion policy.

WHAT SENTINEL IS: the EXACT SAME scan -> plan -> risk -> breakeven@0.5R
pipeline as Anchor v1.0, PLUS the correlated-cluster cap
(config.CLUSTER_CAP_ENABLED / _ADJACENCY_POINTS / _WINDOW_MINUTES,
enforced in trade_tracker.try_open_new_trade via cluster_cap_blocks()):
reject a new signal if a same-direction position on a nearby strike
was opened within the last N minutes. Investigated after real
correlated bursts (NIFTY 2026-08-12: 23 trades in three bursts of
adjacent same-direction strikes; Bank Nifty 2026-08-14: 6 adjacent PE
strikes, all ~0.75R then reversing together) that
MAX_TOTAL_EXPOSURE_PCT never caught, since it only sums risk rupees.
Backtested (shadow.py, full NIFTY history): 200pt / 30min gave up 14%
of Anchor's profit for a 62% smaller max drawdown -- the best
trade-off found across several window sizes. Full numbers in
STRATEGY_VERSIONS.md.

WHY A SEPARATE PROCESS rather than a flag on main_live.py: this
project's own established precedent for "a strategy variant needs its
own everything" is one process per variant (see
main_live_banknifty.py's docstring for the same reasoning) -- and here
it also has a harder requirement behind it: Anchor v1.0 is FROZEN
(STRATEGY_VERSIONS.md's promotion policy). A flag on the same process
that real trades run through is one accidental default-flip away from
silently changing Anchor's live behaviour; a separate process cannot
do that even by mistake.

CONFIG OVERRIDE MECHANISM: same as main_live_banknifty.py -- `config`
is the SAME shared module every strategy file imports, patched with
Sentinel's values BEFORE any strategy module is used. Safe ONLY
because this runs as its own OS process; scanner.py, plan_generator.py,
and trade_tracker.py all read these config values live on every call,
never take a module-level copy at import time, so patching here can
never leak into Anchor's separate process.

DOES NOT RECORD ITS OWN SNAPSHOT ARCHIVE (config.RECORD_SNAPSHOTS =
False below): Sentinel sees the identical NIFTY chain Anchor's process
already archives to logs/snapshots/ within the same few seconds --
recording a second, near-duplicate archive would waste storage and
risk two processes racing to write the same day's file for no benefit.

Run:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 main_live_sentinel.py
"""

import time
import logging
from datetime import datetime, date, time as dtime
from pathlib import Path

import config

# --- Sentinel v1.1-dev identity + cluster-cap config, patched before any
# strategy module is used. See STRATEGY_VERSIONS.md for what these mean
# and the backtest evidence behind the 200pt/30min values.
config.STRATEGY_NAME = "Sentinel"
config.STRATEGY_VERSION = "1.1-dev"
config.CLUSTER_CAP_ENABLED = True
config.CLUSTER_CAP_ADJACENCY_POINTS = 200
config.CLUSTER_CAP_WINDOW_MINUTES = 30
config.RECORD_SNAPSHOTS = False

from resilient_source import get_nifty_snapshot, get_nearest_expiry, get_nifty_intraday_candles
from scanner import scan, compute_market_bias, tag_bias_conflicts
from plan_generator import build_plan
from risk_checker import check
from price_action import analyze_with_context, compute_atr
import trade_tracker as tt
import news_source
import banknifty_context
import opening_gap
import decision_log
import volume_profile as vp
import anchored_vwap as avwap
import snapshot_recorder
import logic_version
import workspace
import market_regime

assert tt.JOURNAL_PATH.name == "trade_journal.jsonl", (
    "trade_tracker captured a non-default JOURNAL_PATH -- import order regressed, "
    "config must be patched (done above) but trade_tracker's own paths still "
    "need patching AFTER this import, which the block below does"
)
tt.OPEN_TRADES_PATH = tt.STATE_DIR / "open_trades_sentinel.json"
tt.JOURNAL_PATH = tt.LOG_DIR / "trade_journal_sentinel.jsonl"
tt.BREAKEVEN_SHADOW_JOURNAL_PATH = tt.LOG_DIR / "breakeven_shadow_journal_sentinel.jsonl"
tt.DELAY_PROBE_JOURNAL_PATH = tt.LOG_DIR / "execution_delay_journal_sentinel.jsonl"
decision_log.LOG_PATH = Path(__file__).parent / "logs" / "decision_log_sentinel.jsonl"
market_regime.BASELINE_PATH = market_regime.STATE_DIR / "regime_baseline_sentinel.json"

POLL_INTERVAL_SECONDS = 30   # identical cadence to Anchor -- same signal source, same timing
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 15)   # see main_live.py's own comment on why not the nominal 15:30

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_path = LOG_DIR / f"nifty_scan_sentinel_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("nifty_scanner_sentinel")

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


_banknifty_cache = {"context": None, "fetched_at": None}


def get_cached_banknifty_context():
    now = datetime.now()
    stale = (
        _banknifty_cache["context"] is None
        or _banknifty_cache["fetched_at"] is None
        or (now - _banknifty_cache["fetched_at"]).total_seconds() >= config.BANKNIFTY_CACHE_MINUTES * 60
    )
    if stale:
        _banknifty_cache["context"] = banknifty_context.get_banknifty_context()
        _banknifty_cache["fetched_at"] = now
    return _banknifty_cache["context"]


def _tracked_strikes(state: dict) -> set:
    return ({t["strike"] for t in state.get("trades", [])}
            | {s["strike"] for s in state.get("breakeven_shadows", [])}
            | {p["strike"] for p in state.get("delay_probes", [])})


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def run_once(expiry: str, state: dict):
    snapshot = get_nifty_snapshot(expiry=expiry, must_include_strikes=_tracked_strikes(state))
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"\n[{ts}] ({snapshot.source}) [SENTINEL] NIFTY spot {snapshot.spot}, VWAP-proxy {snapshot.vwap}, PCR {snapshot.pcr}")

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
    if gap.get("nifty") or gap.get("banknifty"):
        parts = []
        if gap.get("nifty"):
            g = gap["nifty"]
            parts.append(f"NIFTY {g['gap_points']:+.1f} pts ({g['gap_pct']:+.2f}%)")
        if gap.get("banknifty"):
            g = gap["banknifty"]
            parts.append(f"Bank Nifty {g['gap_points']:+.1f} pts ({g['gap_pct']:+.2f}%)")
        log.info(f"  Opening gap: {' | '.join(parts)}")

    bn_ctx, divergence = None, None
    vol_profile = {"error": "not computed"}
    anchored_vwap_ctx = {"error": "not computed"}
    atr = None
    regime = {}
    try:
        candles = get_nifty_intraday_candles()
        price_levels, context = analyze_with_context(candles)
        atr = compute_atr(candles)
        # RECORD_SNAPSHOTS = False above -- Anchor's own process already
        # archives this same market data, see module docstring.
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

        if candles:
            nifty_pct_change = round((candles[-1].close - candles[0].open) / candles[0].open * 100, 2)
            bn_ctx = get_cached_banknifty_context()
            divergence = banknifty_context.compute_divergence(nifty_pct_change, bn_ctx)
            if "error" not in bn_ctx:
                log.info(
                    f"  Bank Nifty: {bn_ctx['spot']} ({bn_ctx['pct_change_today']:+.2f}%, {bn_ctx['trend']}) -- "
                    f"{divergence['read']}: {divergence['detail']}"
                )
            else:
                log.info(f"  Bank Nifty context unavailable this cycle: {bn_ctx['error']}")
    except Exception as e:
        log.info(f"  Price-action fetch failed this cycle, scanning without it: {e}")
        price_levels, context = [], None

    closed = tt.update_open_trades(state, snapshot)
    for trade in closed:
        if trade["outcome"] == "BREAKEVEN_STOP":
            log.info(
                f"  [BREAKEVEN STOP] {trade['strike']} {trade['option_type']}  "
                f"entry {trade['entry']} -> exit {trade['exit_ltp']} (armed at {config.BREAKEVEN_ARM_R:g}R, peaked {trade.get('max_r_reached')}R)  "
                f"pnl {trade['pnl_pct']:+.1f}% (Rs {trade['pnl_inr']:+,.0f}) -- now watching to see if it would have hit target anyway"
            )
        else:
            log.info(
                f"  [TRADE CLOSED: {trade['outcome']}] {trade['strike']} {trade['option_type']}  "
                f"entry {trade['entry']} -> exit {trade['exit_ltp']}  "
                f"pnl {trade['pnl_pct']:+.1f}% (Rs {trade['pnl_inr']:+,.0f})"
            )
        log.info(f"    lesson: {trade['lesson']}")

    resolved_shadows = tt.update_breakeven_shadows(state, snapshot)
    for shadow in resolved_shadows:
        log.info(
            f"  [BREAKEVEN SHADOW RESOLVED] {shadow['strike']} {shadow['option_type']}  "
            f"{shadow['resolution']}  (closed at breakeven for Rs {shadow['breakeven_pnl_inr']:+,.0f}, "
            f"peaked {shadow.get('max_r_seen_after_breakeven')}R afterward)"
        )

    for probe in tt.update_delay_probes(state, snapshot):
        last = probe["samples"][-1]
        log.info(
            f"  [DELAY PROBE] {probe['strike']} {probe['option_type']} {probe['leg']}  "
            f"signal {probe['signal_price']} -> {last['price']} after {last['delay_sec']:.0f}s "
            f"(Rs {last['adverse_inr']:+,.0f} if filled that late)"
        )

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
            elif tt.cluster_cap_blocks(state, best_setup.strike, best_setup.option_type, snapshot.timestamp):
                reason = (f"correlated-cluster cap: a same-direction position within "
                         f"{config.CLUSTER_CAP_ADJACENCY_POINTS}pt opened in the last "
                         f"{config.CLUSTER_CAP_WINDOW_MINUTES}min")
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
        bn_ctx, divergence, news_risk, gap, results, state, new_trade,
        volume_profile=vol_profile, anchored_vwap=anchored_vwap_ctx, market_regime=regime,
    )

    tt.save_open_trades(state)
    return snapshot


def force_close_all(state: dict, expiry: str, last_snapshot=None):
    if not state["trades"] and not state.get("breakeven_shadows") and not state.get("delay_probes"):
        return
    snapshot = last_snapshot if last_snapshot is not None else get_nifty_snapshot(
        expiry=expiry, must_include_strikes=_tracked_strikes(state))
    closed = tt.force_close_end_of_day(state, snapshot)
    for trade in closed:
        log.info(
            f"  [EOD CLOSE] {trade['strike']} {trade['option_type']}  "
            f"entry {trade['entry']} -> exit {trade['exit_ltp']}  "
            f"pnl {trade['pnl_pct']:+.1f}% (Rs {trade['pnl_inr']:+,.0f})"
            + ("  [ESTIMATED -- no closing quote available]" if trade.get("exit_price_estimated") else "")
        )
        log.info(f"    lesson: {trade['lesson']}")

    resolved_shadows = tt.force_close_breakeven_shadows(state, snapshot)
    for shadow in resolved_shadows:
        log.info(
            f"  [BREAKEVEN SHADOW EOD] {shadow['strike']} {shadow['option_type']}  "
            f"still between breakeven and target when the market closed (peaked {shadow.get('max_r_seen_after_breakeven')}R)"
        )

    for probe in tt.force_close_delay_probes(state, snapshot):
        log.info(
            f"  [DELAY PROBE EOD] {probe['strike']} {probe['option_type']} {probe['leg']}  "
            f"{len(probe['samples'])} sample(s) collected before close"
            + ("" if probe.get("complete") else " -- incomplete, flagged in the journal")
        )
    tt.save_open_trades(state)


def check_open_trades_fast(state: dict, expiry: str):
    if not state["trades"]:
        return
    try:
        snapshot = get_nifty_snapshot(expiry=expiry, must_include_strikes=_tracked_strikes(state))
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
    log.info(f"[SENTINEL v1.1-dev] Workspace: {ws.upper()}  |  git {git['branch']} @ {git['commit']}"
             + ("  [UNCOMMITTED CHANGES]" if git["dirty"] else ""))
    if ws == workspace.DEVELOPMENT:
        log.info("  WARNING: running a live session from the DEVELOPMENT checkout. Trades "
                 "journalled here will not be in production's record.")
    if ws == workspace.PRODUCTION and git["branch"] != "master":
        log.info(f"  WARNING: production is on '{git['branch']}', not master -- this session "
                 f"is running unreleased code.")

    log.info("Fetching nearest expiry...")
    expiry = get_nearest_expiry()
    log.info(f"[SENTINEL] Tracking expiry: {expiry}. Polling every {POLL_INTERVAL_SECONDS}s during market hours.")
    log.info(f"Max {config.MAX_NEW_TRADES_PER_DAY} new trades/day, conviction bar {config.MIN_CONVICTION_SCORE_TO_TRACK}.")
    log.info(f"Cluster cap: {config.CLUSTER_CAP_ADJACENCY_POINTS}pt / {config.CLUSTER_CAP_WINDOW_MINUTES}min "
             f"(the ONLY difference from Anchor v1.0 -- see STRATEGY_VERSIONS.md)")
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
            f"likely the previous run of this script was stopped, crashed, or went silent before market close. "
            f"Recovering and journaling them now, using each trade's last known live price as the exit."
        )
        try:
            recovery_snapshot = get_nifty_snapshot(
                expiry=get_nearest_expiry(), must_include_strikes=_tracked_strikes(state))
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
            "breakeven_shadows": [],
            "delay_probes": [],
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
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] [SENTINEL] Market closed, sleeping...")
            was_open_last_cycle = False

        time.sleep(config.FAST_CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
