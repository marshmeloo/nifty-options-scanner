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
from datetime import datetime, date, time as dtime
from pathlib import Path

import config
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

_news_cache = {"flags": None, "fetched_at": None}


def get_cached_news_flags():
    """News doesn't need a 30s refresh like OI/price does -- refetch at
    most every config.NEWS_CACHE_MINUTES so we're not hammering RSS feeds
    every single poll cycle."""
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
    """Same reasoning as get_cached_news_flags -- Bank Nifty's own trend
    doesn't meaningfully change every 30s either."""
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


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def run_once(expiry: str, state: dict):
    snapshot = get_nifty_snapshot(expiry=expiry)
    ts = snapshot.timestamp.strftime("%H:%M:%S")
    log.info(f"\n[{ts}] ({snapshot.source}) NIFTY spot {snapshot.spot}, VWAP-proxy {snapshot.vwap}, PCR {snapshot.pcr}")

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
        # Record the raw inputs behind this cycle BEFORE any decision is
        # taken, so the recorded history is exactly what the logic saw.
        # Only on full cycles -- the 5s fast check doesn't come through
        # here, which keeps the recording cadence at the scan interval.
        snapshot_recorder.record(snapshot, candles, logic_version.compute())
        # Regime context: is today even a normal day? Cheap -- reuses the
        # candles above, and the trailing baseline is cached once a day.
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

        # Reuses the SAME candles already fetched above for price_action --
        # no extra API call needed for the volume profile.
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

    # --- Step 1: update trades already being tracked, BEFORE looking for new ones ---
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

    # Recompute the risk gates from live state EVERY cycle. These used to
    # be static 0.0 arguments threaded down from __main__, which silently
    # disabled both the total-exposure cap and the daily-loss circuit
    # breaker entirely. Computed AFTER update_open_trades() above, so
    # realized P&L includes anything that closed this cycle and unrealized
    # reflects this cycle's marks.
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

    # --- Step 2: only consider opening ONE new trade this cycle, and only ---
    # if the daily cap isn't reached and conviction (after the learned
    # adjustment) clears the raised bar. This replaces printing the whole
    # noisy chain every cycle.
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
            # Show the ADJUSTED score and the precise reason, not just the
            # raw score against the bar -- a candidate can clear the raw
            # bar and still get rejected after the learned tag-based
            # adjustment, which the old version of this line never showed
            # (that's exactly what caused the "score 5.75, bar 5.0, but no
            # trade" confusion on 2026-07-24).
            best_setup, best_plan, best_verdict = results[0]
            adjusted_score, learn_notes = tt.apply_learned_adjustment(best_setup.score, best_setup.reasons)
            conviction_bar, expiry_blocked = tt.expiry_day_rules(best_setup.expiry, snapshot.timestamp)
            if best_verdict.decision != "APPROVED":
                reason = f"risk check: {'; '.join(best_verdict.reasons) if best_verdict.reasons else best_verdict.decision}"
            elif expiry_blocked:
                reason = expiry_blocked
            elif tt.is_repeat_of_stopped_plan(state, best_setup.strike, best_setup.option_type, best_plan.entry):
                reason = f"repeat of a plan already stopped out today near entry {best_plan.entry}"
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
    """
    Called once at/after market close to settle any trades still open.
    Prefers `last_snapshot` (the final snapshot fetched while the market
    was still confirmed open) over fetching a brand-new one -- a fresh
    fetch made AFTER close can come back thin/stale/incomplete right at
    the transition, which used to make already-closed trades look like
    flat 0% outcomes when they'd actually moved during the day (see the
    2026-07-22 incident notes in trade_tracker.force_close_end_of_day).
    Only falls back to a fresh fetch if no last_snapshot is available at all.
    """
    if not state["trades"]:
        return
    snapshot = last_snapshot if last_snapshot is not None else get_nifty_snapshot(expiry=expiry)
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
    """
    Runs every FAST_CHECK_INTERVAL_SECONDS between full scan cycles --
    re-checks already-open trades' target/stop only, so a spike-through
    between two 30s snapshots doesn't go unnoticed for up to 30s longer
    than necessary. Skips the fetch entirely if there's nothing open
    (the common case for most of a session), so this costs nothing when
    there's nothing to check.
    """
    if not state["trades"]:
        return
    try:
        snapshot = get_nifty_snapshot(expiry=expiry)
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
    # Which checkout is this, and is it running released code? A live
    # session started from the development checkout would write real
    # trades into a journal that is only ever a COPY of production's --
    # silently splitting the trade record across two places. Warn loudly
    # rather than refuse: someone may legitimately be testing the loop.
    ws = workspace.role()
    git = workspace.git_state()
    log.info(f"Workspace: {ws.upper()}  |  git {git['branch']} @ {git['commit']}"
             + ("  [UNCOMMITTED CHANGES]" if git["dirty"] else ""))
    if ws == workspace.DEVELOPMENT:
        log.info("  WARNING: running a live session from the DEVELOPMENT checkout. Trades "
                 "journalled here will not be in production's record.")
    if ws == workspace.PRODUCTION and git["branch"] != "master":
        log.info(f"  WARNING: production is on '{git['branch']}', not master -- this session "
                 f"is running unreleased code.")

    log.info("Fetching nearest expiry...")
    expiry = get_nearest_expiry()
    log.info(f"Tracking expiry: {expiry}. Polling every {POLL_INTERVAL_SECONDS}s during market hours.")
    log.info(f"Max {config.MAX_NEW_TRADES_PER_DAY} new trades/day, conviction bar {config.MIN_CONVICTION_SCORE_TO_TRACK}.")
    log.info(tt.summarize_recent_lessons())

    state = tt.load_open_trades()

    # Settle leftover open trades in TWO situations, not just one:
    #   1. The state is from a previous calendar date (existing check).
    #   2. The state is from TODAY, but the process is only starting now
    #      and the market is ALREADY closed -- e.g. the previous instance
    #      of this script silently died mid-session (crashed, hung, or
    #      was killed) with trades still open, and by the time it got
    #      restarted the trading day was already over. This happened for
    #      real on 2026-07-24: a trade opened at 11:04, the process then
    #      went completely silent (no error, no further cycles logged)
    #      for the rest of the day, and a same-day restart's first check
    #      landed after 15:30 -- since that restart's own
    #      `was_open_last_cycle` starts False, the normal market-close
    #      transition that triggers settlement never fires, and the
    #      trade would otherwise sit "OPEN" in state forever with no one
    #      the wiser. Catching this here, at startup, closes that gap
    #      regardless of what caused the original process to go silent.
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
            f"Recovering and journaling them now, using each trade's last known live price as the exit (not a "
            f"guess -- see each journal entry's lesson for its exact price source)."
        )
        try:
            recovery_snapshot = get_nifty_snapshot(expiry=get_nearest_expiry())
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
    last_full_cycle_at = 0.0  # forces a full cycle on the very first iteration

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
    # Exposure and daily-loss are now computed live each cycle by
    # tt.compute_risk_state() from this tool's OWN journal + open trades
    # (see run_once), so the total-exposure cap and daily-loss circuit
    # breaker are actually enforced rather than being passed a permanent
    # 0.0. NOTE: that means they reflect trades THIS TOOL tracked, not
    # your real broker account. Wiring them to Dhan's positions/funds
    # endpoints (https://dhanhq.co/docs/v2/portfolio/,
    # https://dhanhq.co/docs/v2/funds/) is still the right move if you
    # trade the same account manually alongside this.
    run_forever()
