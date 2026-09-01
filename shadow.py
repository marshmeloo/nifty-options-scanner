"""
Shadow trading: simulate what ANY policy would have done, using recorded
market history, without risking a rupee.

WHY THIS EXISTS
---------------
The obvious way to learn how low-score setups perform is to trade them.
That means paying real money for information, and it means waiting: at
~4 trades a day, evaluating one threshold change to any useful
confidence takes months (see the README's measurement-methodology
section for the arithmetic).

Every input needed to answer those questions is already on disk.
snapshot_recorder.py stores the full chain every cycle, so a candidate
that was REJECTED can be opened as a simulated trade and walked forward
through subsequent recorded chains to a real target/stop/EOD outcome.
The result is the same information a live trade would have produced,
for free, and re-runnable after any logic change.

WHAT IS SIMULATED FAITHFULLY
----------------------------
  - Entry at the ASK, exit at the BID (both recorded), so the fill side
    matches live behaviour rather than flattering it.
  - Target/stop evaluated against the BID -- what a close would actually
    realise. A stop that only triggers on an unreachable LTP is not a stop.
  - Full transaction costs via costs.py, so results are net.
  - The real scanner, plan generator and risk checker, so a change to any
    of them shows up here.

WHAT IS NOT
-----------
  - Path within a 30s gap. Target and stop are checked at recorded
    cycles only; a spike that touched a level and reversed between two
    snapshots is invisible. This biases results OPTIMISTICALLY for stops
    (some would have triggered) and pessimistically for targets. The live
    system has the same blind spot at its 30s scan cadence, partly
    mitigated by its 5s fast check, which is not reproduced here.
  - Market impact and partial fills. At 1 lot on contracts quoting
    0.1-0.6% spreads this is small, but it is not zero.
  - Any feedback from the trade's own existence.

Treat shadow results as a strong filter for REJECTING variants, and as
suggestive-not-conclusive for accepting them. Same rule as replay.py.
"""

import argparse
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Optional

import config
import costs
import historical_source as hs
import oi_analytics
import price_action
import snapshot_recorder
import trade_tracker as tt
from plan_generator import build_plan
from risk_checker import check
from scanner import scan, compute_market_bias


@dataclass
class Policy:
    """
    A complete set of trading rules to evaluate. Every field maps to a
    real decision the live system makes, so a Policy that mirrors
    config.py should reproduce live behaviour.
    """
    name: str = "default"
    # Which underlying to replay, paired with snapshot_dir below.
    #
    # BOTH indices have real reconstructed history, backfilled from the
    # same Dhan Expired Options endpoint: logs/snapshots (NIFTY, 1,491
    # days from 2020-08) and logs/snapshots_banknifty (BANK NIFTY, 1,244
    # days from 2021-08). BOTH live only in the DEV checkout -- production
    # keeps just what its own processes record (23 and 12 days), which is
    # exactly the trap that produced a wrong claim on 2026-08-28 that Bank
    # Nifty had no history and all its conclusions were extrapolated. It
    # does, they aren't: sweep_banknifty_cluster_cap.py picked the live
    # 500pt band from that history across 5 independent ~1-year periods,
    # and research/banknifty_directional_exposure_backtest.py measures the
    # gate and reversal exit on it. Check the DEV checkout before
    # concluding data is missing.
    symbol: str = "NIFTY"
    snapshot_dir: str = None           # None -> snapshot_recorder.SNAPSHOT_DIR
    # config values to apply FOR THE DURATION of this replay, then restore
    # -- exactly the mechanism a live process uses for a second underlying
    # (main_live_banknifty_sentinel.py just assigns config.NIFTY_LOT_SIZE =
    # 30, config.PREMIUM_MIN = 300.0 and so on at import).
    #
    # Replaying another underlying means replicating ITS process's whole
    # config patch set, not just the obvious one. Learned the hard way on
    # 2026-08-28: a Bank Nifty replay given only the lot size still used
    # NIFTY's PREMIUM_MIN/MAX of 10-150 against Bank Nifty premiums, picked
    # strikes ~1,000-3,000 points away from the ones live actually traded,
    # and matched 0 of 31 real trades. Use BANKNIFTY_SENTINEL_OVERRIDES
    # below rather than assembling the dict by hand.
    config_overrides: dict = None
    min_score: float = None            # None -> config.MIN_CONVICTION_SCORE_TO_TRACK
    target_rr: float = None            # None -> config.DEFAULT_TARGET_RR
    start_time: str = "09:15"
    end_time: str = "15:30"
    max_trades_per_day: int = None     # None -> unlimited
    # End the day after the first genuine stop-out (outcome == "LOSS").
    # Deliberately does NOT trigger on a trade that merely finishes
    # negative at EOD_CLOSE -- that outcome is only knowable at 15:30,
    # by which point there is no more day left to stop trading during.
    # Only a real stop-hit is a discrete, real-time event a live trader
    # could act on, and it only takes effect from the moment it actually
    # happens (see loss_known_at in run_policy), not retroactively.
    stop_after_loss: bool = False
    # Defaults MIRROR LIVE. main_live.py allows concurrent positions on
    # different strikes (open_keys is keyed on strike+type), so a shadow
    # default of "one at a time" would silently model a different system
    # and make every comparison against live meaningless.
    one_at_a_time: bool = False        # no new entry while a position is open
    allow_repeat_strike: bool = False  # re-enter the same strike+type later the same day
    use_bias_gate: bool = True
    # Live's expiry-day discipline (trade_tracker.expiry_day_rules(): a
    # higher conviction bar on same-day-expiry contracts, and a hard
    # cutoff blocking new same-day-expiry entries after 14:00). Found
    # 2026-08-26 to be MISSING from run_policy() entirely -- every
    # backtest ever run through shadow.py, including the ones that
    # validated the cluster cap and the original breakeven-arm figure,
    # used a flat bar all day regardless of expiry, unlike every live
    # process (which calls trade_tracker.try_open_new_trade(), which DOES
    # enforce this). Defaults to True so shadow.py now matches live by
    # default; set False to reproduce the old (undocumented) behaviour,
    # which is also what this flag exists to let research/
    # expiry_day_rule_study.py test the rule itself against.
    #
    # NOTE (2026-08-26): the rule itself is now OFF live
    # (config.EXPIRY_DAY_RULES_ENABLED = False -- see that comment for
    # why), and tt.expiry_day_rules() checks that flag before anything
    # else. Same interaction as Policy.use_learned_adjustment already
    # has with config.LEARNED_TAG_ADJUSTMENT_ENABLED: this Policy switch
    # is currently inert regardless of its own value, and becomes
    # load-bearing again only if the live flag is ever turned back on.
    use_expiry_day_rules: bool = True
    # Learned tag adjustment reads the LIVE trade journal, which is a
    # record of a specific (recent) period. Applying it to reconstructed
    # history from an earlier period is look-ahead bias: 2026 tag win
    # rates informing a 2024 decision. Set False for historical runs.
    #
    # Currently inert either way regardless of this flag or journal size:
    # config.LEARNED_TAG_ADJUSTMENT_ENABLED was turned off live 2026-08-18
    # (see config.py's comment), and apply_learned_adjustment() checks
    # that flag before anything else, so it returns the raw score
    # untouched no matter what use_learned_adjustment is set to here. This
    # USED to be an accident of timing (a 9-trade journal sitting below
    # the 30-trade floor, about to silently start mattering the moment it
    # crossed that line) -- it is now a deliberate global switch instead,
    # which is the safer state to be inert FROM. If the live flag is ever
    # flipped back on, this backtest-side switch becomes load-bearing
    # again exactly as originally intended.
    use_learned_adjustment: bool = True
    # Optional callable(setup) -> float replacing the scanner's own score,
    # so alternative weightings can be evaluated over recorded history
    # WITHOUT editing scanner.py or config.py. Keeping variants out of the
    # live modules matters: a weighting under test must not be one edit
    # away from silently becoming the weighting that trades.
    rescore: object = None
    # Correlated-cluster caps (added 2026-08-15, BACKTEST-ONLY -- neither
    # is wired into risk_checker.check() or any live process). Investigated
    # after finding real sessions where the scanner fired the same
    # underlying signal repeatedly across adjacent strikes within minutes
    # (NIFTY 2026-08-12: 23 trades, all tagged "support", in three bursts
    # of 5-11 adjacent strikes; Bank Nifty 2026-08-14: 6 adjacent PE
    # strikes, all reaching ~0.75R together then reversing together).
    # MAX_TOTAL_EXPOSURE_PCT never caught this because it only sums risk
    # rupees -- seven small positions on adjacent strikes are each too
    # small individually to breach it, even though they are not a
    # diversified seven bets, they are close to being the same bet seven
    # times. Both are None (disabled) by default so every existing
    # backtest and comparison in this session stays reproducible; a
    # variant under test sets one explicitly.
    max_open_per_direction: int = None        # None -> unlimited, today's real behaviour
    strike_adjacency_band_points: float = None  # None -> disabled
    # Sentinel v1.1-dev refinement (added 2026-08-15, STILL BACKTEST-ONLY):
    # the two caps above had no time component -- a position blocked new
    # same-direction entries for its ENTIRE open lifetime, which for an
    # EOD-outcome trade can be hours, and backtested 30-54% profit cut for
    # a 71-75% drawdown cut -- the right direction, wrong magnitude,
    # because it was blocking ordinary trading, not just the rapid-fire
    # pattern. This narrows either cap above to only apply if the
    # blocking position was opened within this many minutes of `ts` --
    # None means no narrowing (identical to the original, untimed
    # behaviour), so this stays off unless explicitly set.
    cluster_window_minutes: float = None
    # Opposite-direction exposure gate (added 2026-08-27, matches
    # config.OPPOSITE_DIRECTION_GATE_ENABLED / trade_tracker.
    # opposite_direction_blocks()): blocks a new candidate while a
    # position in the OPPOSITE option_type is open, no adjacency/window
    # parameters -- see that config comment for the 2026-08-27 incident
    # and the 6-year backtest. Defaults True (matches live's default),
    # unlike the cluster caps above which default off/None to keep
    # every existing backtest reproducible -- set False to reproduce
    # pre-2026-08-27 behaviour.
    # Reconstruct the missing `delta` on historical quotes via
    # Black-Scholes (see fill_missing_delta() for the measurement and the
    # validation). Defaults True so shadow.py MATCHES LIVE by default --
    # same reasoning as use_expiry_day_rules above, and the same kind of
    # silent divergence: without it every historical trade gets a flat
    # 30%-of-premium stop while live computes 15-24% from ATR x delta.
    # Set False only to reproduce pre-2026-08-28 results.
    reconstruct_missing_greeks: bool = True
    use_opposite_direction_gate: bool = True
    # Reversal-exit (added 2026-08-27, research/reversal_exit_study.py's
    # tested hypothesis, built for real): when a fully-qualified OPPOSITE-
    # direction candidate is blocked by the gate above, ALSO close the
    # blocking position(s) right there at the current price instead of
    # letting them run to their original stop/target/EOD -- see
    # trade_tracker... no live counterpart exists yet, backtest-only until
    # this is validated the same way the gate itself was. Conservative:
    # closes the OLD position, does NOT open the NEW (still-blocked) one --
    # a direction flip is a different, untested hypothesis. Only takes
    # effect when use_opposite_direction_gate is also True (there is
    # nothing to react to otherwise). Default False -- unlike the gate,
    # this has not shipped anywhere; every existing study stays
    # reproducible unless a caller opts in explicitly.
    use_reversal_exit: bool = False
    # EXTENSION GUARD (added 2026-08-31). Refuse an entry when spot has
    # already travelled most of its recent range in the signal's own
    # direction -- i.e. do not buy PE at the bottom of the range, or CE
    # at the top.
    #
    # WHY. Every reason string behind the 2026-08-31 session's losing
    # entries was "Momentum aligned: X% ROC supports this direction", and
    # ROC is BACKWARD-LOOKING: it can only turn negative after price has
    # already fallen. So a momentum-confirmation entry is late by
    # construction. Measured on that session, all 13 Bank Nifty entries
    # were in the direction the previous 30 minutes had already moved
    # (PE after -57 to -146 pts, CE after +97 to +158), and the worst pair
    # never went a single point favourable. On a trending day the leg
    # continues; on that day the index ranged 0.66% and the move that
    # triggered the signal WAS the whole move.
    #
    # Expressed as a percentile of the lookback range: 0.8 means "refuse a
    # CE when spot sits above the 80th percentile of its recent range, and
    # a PE when it sits below the 20th". None disables it, so every
    # existing study stays reproducible.
    extension_guard_pctile: float = None
    extension_lookback_minutes: float = 30.0
    # QUIET-REGIME GATE (added 2026-08-31). Refuse NEW entries once the
    # session's range-so-far ranks below `quiet_regime_block_pctile` of a
    # TRAILING baseline, and enough of the session has elapsed for that
    # reading to mean anything.
    #
    # WHY. market_regime.py has computed exactly this live since July, for
    # an unrelated reason, and NOTHING GATES ON IT -- grep finds only
    # logging. On 2026-08-31 it printed QUIET every cycle: Bank Nifty p0
    # at the open, p7 by 10% elapsed, p9 by 20%, then flat at p13 all
    # afternoon. The nine losing PE entries fired at 10:09-10:23, inside
    # that p7-p9 window. The day used less range by 11am than 93% of days
    # use in total, and the system traded it 27 times.
    #
    # `regime_baseline` is the sorted list of trailing daily range
    # percentages for the day being replayed, supplied by the caller.
    # market_regime.get_range_distribution() fetches a LIVE 180-day
    # window, which applied to 2021 history would be look-ahead; the study
    # builds a rolling baseline from the reconstructed history instead.
    #
    # The elapsed floor is not optional. market_regime's own docstring
    # says it: an in-progress range is partial, so at 09:30 every day
    # scores as the quietest on record. Gating without it would block the
    # first trade of every session.
    # DEPLOYMENT CAP (added 2026-09-01). Refuse a new entry when the
    # PREMIUM already committed to open positions, plus this candidate's,
    # would exceed this % of config.TOTAL_CAPITAL.
    #
    # WHY IT IS NOT ALREADY COVERED. config.MAX_TOTAL_EXPOSURE_PCT (20%)
    # sounds like this and is not: trade_tracker.compute_risk_state sums
    # (entry - stop) x lot x lots, i.e. RISK AT STOP, not money handed
    # over. On 2026-08-27 the live book committed Rs 4,30,860 of premium
    # at once -- 86% of the Rs 5,00,000 allocation, Anchor alone holding
    # Rs 3,23,362 (65%) -- while that guard read roughly 3%. Actual
    # capital commitment has never been bounded by anything.
    #
    # None disables it, so every existing study stays reproducible.
    max_deployed_pct: float = None
    quiet_regime_block_pctile: float = None
    quiet_regime_min_elapsed_pct: float = 20.0
    regime_baseline: list = None

    def resolved_min_score(self) -> float:
        return self.min_score if self.min_score is not None else config.MIN_CONVICTION_SCORE_TO_TRACK

    def resolved_target_rr(self) -> float:
        return self.target_rr if self.target_rr is not None else config.DEFAULT_TARGET_RR


@dataclass
class ShadowTrade:
    strike: float
    option_type: str
    opened_at: str
    entry: float
    stop: float
    target: float
    score: float
    adjusted_score: float
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None       # WIN / LOSS / EOD_CLOSE
    peak_price: Optional[float] = None
    trough_price: Optional[float] = None
    gross_r: Optional[float] = None
    net_r: Optional[float] = None
    peak_r: Optional[float] = None
    gross_inr: Optional[float] = None
    net_inr: Optional[float] = None
    cost_inr: Optional[float] = None
    reasons: list = field(default_factory=list)


def _parse_hhmm(s: str) -> dtime:
    h, m = (int(x) for x in s.split(":"))
    return dtime(h, m)


def build_price_index(cycles) -> dict:
    """
    {(strike, option_type): [(timestamp, bid, ask, ltp), ...]} in time
    order, so walking a simulated trade forward is a list scan rather
    than a re-search of every chain.
    """
    index = defaultdict(list)
    for snapshot, _candles, _meta in cycles:
        for q in snapshot.chain:
            index[(q.strike, q.option_type)].append(
                (snapshot.timestamp, q.bid, q.ask, q.ltp)
            )
    return index


def deployment_blocked(positions, ts, candidate_cost, max_pct) -> bool:
    """
    True when premium already committed, plus this candidate's, would
    exceed `max_pct` of config.TOTAL_CAPITAL.

    Counts a position as committed while opened_ts <= ts < closed_ts --
    the same causality risk_state_at() uses, so a trade that closes later
    in the day does not retroactively free capital for an earlier
    decision.
    """
    if max_pct is None:
        return False
    lot = getattr(config, "NIFTY_LOT_SIZE", 65)
    committed = sum(
        p["entry"] * lot * p["lots"] for p in positions
        if p["opened_ts"] <= ts and (p["closed_ts"] is None or p["closed_ts"] > ts))
    budget = config.TOTAL_CAPITAL * max_pct / 100.0
    return (committed + candidate_cost) > budget


def quiet_regime_blocked(candles, ts, baseline, block_pctile, min_elapsed_pct) -> bool:
    """
    True when the session so far is quieter than `block_pctile` of the
    trailing baseline, and enough of it has run to say so.

    Returns False whenever the question is unanswerable -- no baseline, no
    candles, degenerate range, or too little of the session elapsed. An
    unmeasurable condition must never silently halt trading.
    """
    if block_pctile is None or not baseline or not candles:
        return False
    session_start = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    session_minutes = 375.0                     # 09:15 -> 15:30
    elapsed_pct = min(100.0, max(0.0,
                      (ts - session_start).total_seconds() / 60.0 / session_minutes * 100.0))
    if elapsed_pct < min_elapsed_pct:
        return False
    day_open = candles[0].open
    if not day_open:
        return False
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    if hi <= lo:
        return False
    range_pct = (hi - lo) / day_open * 100.0
    below = sum(1 for r in baseline if r < range_pct)
    percentile = below / len(baseline) * 100.0
    return percentile <= block_pctile


def extension_blocked(option_type: str, spot: float, spot_history: list, ts,
                      pctile: float, lookback_minutes: float) -> bool:
    """
    True when spot has already run too far in the direction this contract
    needs, measured as its position within the lookback range.

    position = (spot - low) / (high - low), so 1.0 is the top of the range
    and 0.0 the bottom. A CE is refused above `pctile`; a PE below
    `1 - pctile`. Returns False when the range is degenerate or the
    history is too short to judge -- an unmeasurable condition must not
    silently block trading.
    """
    if pctile is None or not spot_history:
        return False
    cutoff = ts - timedelta(minutes=lookback_minutes)
    window = [p for t, p in spot_history if t >= cutoff]
    if len(window) < 5:
        return False
    lo, hi = min(window), max(window)
    if hi <= lo:
        return False
    position = (spot - lo) / (hi - lo)
    if option_type == "CE":
        return position > pctile
    return position < (1.0 - pctile)


def opposite_direction_blocked(setup, positions: list, ts) -> bool:
    """
    Backtest counterpart of trade_tracker.opposite_direction_blocks():
    True if a position in the OPPOSITE option_type is currently open
    (opened_ts <= ts < closed_ts). No adjacency/window narrowing, same
    as the live gate -- see Policy.use_opposite_direction_gate.
    """
    return any(
        p["key"][1] != setup.option_type
        and p["opened_ts"] <= ts
        and (p["closed_ts"] is None or p["closed_ts"] > ts)
        for p in positions
    )


def _closeable_price(bid, ltp) -> float:
    """What closing a long would realise: the bid, or LTP if no book."""
    if getattr(config, "USE_BID_ASK_FILLS", False) and bid:
        return bid
    return ltp


def _price_at(index, key, ts):
    """Last recorded closeable price for `key` at or before `ts`."""
    px = None
    for t, bid, _ask, ltp in index.get(key, []):
        if t > ts:
            break
        got = _closeable_price(bid, ltp)
        if got is not None:
            px = got
    return px


def risk_state_at(index, positions: list, ts) -> tuple:
    """
    (open_exposure_pct, daily_loss_pct) as of `ts`, mirroring
    trade_tracker.compute_risk_state so the backtest gates on the same
    numbers live does.

    CAUSALITY IS THE WHOLE POINT. Every simulated trade is resolved
    immediately by walking forward through recorded prices, so its outcome
    is sitting in memory long "before" it happens in simulated time. A
    loss is therefore only counted once `closed_at <= ts`, and a position
    only counts toward exposure while `opened_at <= ts < closed_at`.
    Without that gating the breaker would trip at 10:00 on a loss that
    does not occur until 14:00 -- the same look-ahead defect already fixed
    once in this module's stop_after_loss rule.
    """
    capital = config.TOTAL_CAPITAL
    lot_size = getattr(config, "NIFTY_LOT_SIZE", 65)

    realized = 0.0
    unrealized = 0.0
    exposure_inr = 0.0

    for p in positions:
        if p["closed_ts"] is not None and p["closed_ts"] <= ts:
            realized += p["net_inr"] or 0.0
        elif p["opened_ts"] <= ts:
            exposure_inr += (p["entry"] - p["stop"]) * lot_size * p["lots"]
            px = _price_at(index, p["key"], ts)
            if px is not None:
                unrealized += (px - p["entry"]) * lot_size * p["lots"]

    net = realized + unrealized
    return (
        round(exposure_inr / capital * 100, 4),
        round(max(0.0, -net) / capital * 100, 4),
    )


def walk_trade_forward(index, key, entry_ts, trade: ShadowTrade, lots: int = 1) -> ShadowTrade:
    """
    Advance a simulated trade through every later recorded cycle until it
    hits target, hits stop, or the day ends. Prices are evaluated on the
    side a real exit would cross.

    BREAKEVEN ARM. Mirrors trade_tracker.update_open_trades exactly: once
    the trade has EVER been up config.BREAKEVEN_ARM_R, a return to the
    entry price closes it here rather than letting it keep sliding to the
    original stop. Same ordering live uses -- target first (an outright
    win is strictly better), then the breakeven floor, then the original
    stop.

    This was MISSING here until 2026-08-28, which meant every backtest
    that did not go through research/ratchet_study.walk_with_ratchet
    silently modelled a strategy without the breakeven arm, while live
    has run it the whole time (measured: 4 of 35 real Sentinel trades,
    11%, closed as BREAKEVEN_STOP -- an exit the backtest could not
    produce at all). Gated on config.BREAKEVEN_ARM_R, the SAME switch
    live reads, so the two cannot drift apart again; set it to None to
    reproduce pre-2026-08-28 behaviour.

    Intentionally NOT a Policy field: research/ratchet_study.py and
    research/breakeven_arm_study.py monkeypatch this function wholesale
    with their own 5-argument version, so adding a parameter here would
    break that contract silently. Reading the config both sides already
    share is what keeps them honest.
    """
    series = index.get(key, [])
    peak = trough = trade.entry
    arm_r = getattr(config, "BREAKEVEN_ARM_R", None)
    risk_unit = trade.entry - trade.stop
    armed = False

    for ts, bid, _ask, ltp in series:
        if ts <= entry_ts:
            continue
        px = _closeable_price(bid, ltp)
        if px is None:
            continue
        peak = max(peak, px)
        trough = min(trough, px)
        if arm_r is not None and risk_unit > 0 and not armed:
            armed = (peak - trade.entry) / risk_unit >= arm_r

        outcome = None
        if px >= trade.target:
            outcome = "WIN"
        elif armed and px <= trade.entry:
            outcome = "BREAKEVEN_STOP"
        elif px <= trade.stop:
            outcome = "LOSS"
        if outcome:
            return _finalise(trade, ts, px, outcome, peak, trough, lots)

    # Never resolved -- close at the last price seen, as EOD would.
    if series:
        last_ts, last_bid, _a, last_ltp = series[-1]
        px = _closeable_price(last_bid, last_ltp)
        if px is not None:
            return _finalise(trade, last_ts, px, "EOD_CLOSE", peak, trough, lots)
    return trade


def _finalise(trade: ShadowTrade, ts, exit_px, outcome, peak, trough, lots) -> ShadowTrade:
    lot_size = getattr(config, "NIFTY_LOT_SIZE", 65)
    risk_unit = trade.entry - trade.stop

    trade.closed_at = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    trade.exit_price = round(exit_px, 2)
    trade.outcome = outcome
    trade.peak_price = round(peak, 2)
    trade.trough_price = round(trough, 2)

    gross_inr = (exit_px - trade.entry) * lot_size * lots
    cost_inr = costs.round_trip(trade.entry, exit_px, lots, lot_size)["total_inr"]
    trade.gross_inr = round(gross_inr, 2)
    trade.cost_inr = round(cost_inr, 2)
    trade.net_inr = round(gross_inr - cost_inr, 2)

    if risk_unit > 0:
        risk_inr = risk_unit * lot_size * lots
        trade.gross_r = round(gross_inr / risk_inr, 3)
        trade.net_r = round((gross_inr - cost_inr) / risk_inr, 3)
        trade.peak_r = round((peak - trade.entry) / risk_unit, 3)
    return trade


def _close_trade_early(index, key, entry_ts, exit_ts, exit_px, trade: ShadowTrade, lots: int) -> ShadowTrade:
    """
    Recomputes a trade's outcome as if it had been closed at exit_ts
    instead of running to its natural resolution -- reuses _finalise, the
    same costing/R-multiple logic every other exit in this file goes
    through, so an early close is priced exactly like a real one, not a
    separate one-off formula.

    Peak/trough are re-walked over the narrower [entry_ts, exit_ts]
    window, not the trade's original full lifetime -- an early exit must
    not credit itself with favourable excursion the position never
    actually got the chance to realise before this closed it.

    Mutates `trade` IN PLACE (same as walk_trade_forward/_finalise
    already do) -- the caller is responsible for also updating that
    position's bookkeeping entry in `positions` (closed_ts, net_inr) so
    later gate checks (cluster cap, opposite-direction, risk state) see
    this position as closed from exit_ts onward, not its original later
    close time.
    """
    series = index.get(key, [])
    peak = trough = trade.entry
    for ts, bid, _ask, ltp in series:
        if ts <= entry_ts or ts > exit_ts:
            continue
        px = _closeable_price(bid, ltp)
        if px is None:
            continue
        peak, trough = max(peak, px), min(trough, px)
    return _finalise(trade, exit_ts, exit_px, "REVERSAL_EXIT", peak, trough, lots)


# Mirrors main_live_banknifty_sentinel.py's own config patches (that file
# assigns these directly at import). Kept beside the Policy that consumes
# them so a change there is one grep away from being reflected here --
# they WILL drift otherwise, and the failure is silent: a Bank Nifty
# replay run with NIFTY's premium band picks strikes thousands of points
# away from the real ones and simply reports different trades.
BANKNIFTY_SENTINEL_OVERRIDES = {
    "NIFTY_LOT_SIZE": 30,
    "PREMIUM_MIN": 300.0,
    "PREMIUM_MAX": 800.0,
    "STRIKE_RANGE_POINTS": 2000,
    "CLUSTER_CAP_ADJACENCY_POINTS": 500,
    "CLUSTER_CAP_WINDOW_MINUTES": 30,
}


def fill_missing_book(snapshot, spread_pct: float = None) -> int:
    """
    Give quotes with no top-of-book a SYNTHETIC bid/ask straddling LTP,
    so entry crosses an ask and exit hits a bid exactly as live does.
    Returns how many were filled; quotes that already have a real book
    are never touched.

    WHY. Dhan's Expired Options endpoint returns OHLC only, so every
    reconstructed quote has bid = ask = None. plan_generator.build_plan
    then falls back to `entry = quote.ltp` and shadow._closeable_price
    falls back to LTP on the way out -- the backtest transacted at the
    midpoint on both legs and paid no spread at all, while live pays the
    ask on entry and receives the bid on exit. costs.round_trip does NOT
    make this up: it deliberately excludes slippage on the grounds that
    "when bid/ask fills are on, crossing the spread is already reflected
    in the entry/exit prices themselves" -- which is true live and false
    on reconstructed data, so the cost simply went missing.

    MEASURED, NOT ASSUMED. The default comes from 43,927 real live quotes
    (2026-08-17..27) inside the actual config.PREMIUM_MIN..PREMIUM_MAX
    band: median spread 0.10 points, 0.266% of mid. Worth stating plainly
    because costs.py's own docstring reasons from a ONE-POINT spread --
    10x wider than anything actually observed -- so the cost of this gap
    had been overestimated by roughly an order of magnitude. Real impact
    is small: about 0.005-0.017R per trade against a measured edge of
    ~0.195R, i.e. 3-6% of it. Small is not zero, and it is the right
    sign, so it is now modelled rather than argued about.

    Synthesising the BOOK rather than special-casing the fill maths means
    build_plan, _closeable_price and build_price_index all work unchanged
    -- the same trick fill_missing_delta() uses. Must run BEFORE
    build_price_index(), which snapshots bid/ask into the price series.
    """
    if spread_pct is None:
        spread_pct = getattr(config, "SYNTHETIC_SPREAD_PCT", 0.266)
    if not spread_pct or spread_pct <= 0:
        return 0
    half = spread_pct / 200.0     # % of mid -> half-spread as a fraction of price
    filled = 0
    for q in snapshot.chain:
        if q.bid is not None or q.ask is not None or not q.ltp or q.ltp <= 0:
            continue
        q.bid = round(q.ltp * (1 - half), 2)
        q.ask = round(q.ltp * (1 + half), 2)
        filled += 1
    return filled


def fill_missing_delta(snapshot, resolved_expiry: str = None) -> int:
    """
    Populate `delta` on any quote in `snapshot.chain` that has none,
    using Black-Scholes from what reconstructed history DOES carry
    (spot, strike, time to expiry, IV). Returns how many were filled.
    Quotes that already have a real delta are never touched.

    WHY THIS MATTERS MORE THAN IT LOOKS. plan_generator._stop_distance
    sizes the stop as ATR x |delta| x STOP_ATR_MULTIPLE and falls back to
    a FLAT config.DEFAULT_STOP_LOSS_PCT whenever a quote has no delta.
    Dhan's Expired Options endpoint returns no Greeks, so EVERY backtest
    over reconstructed history was silently taking that fallback while
    live took the real path. Measured 2026-08-28 over 1,085 reconstructed
    trades: the stop was 30.0% of premium on every single one of them
    (median, mean, min 29.95, max 30.05), against live's real
    ATR x delta outcome of 15-24% (usually pinned to the 15%
    MIN_STOP_PCT floor). The backtest was giving every trade about TWICE
    the stop room live gives it -- so 1R meant a different thing on each
    side, and every R-multiple, win rate and expectancy compared across
    them was comparing two different strategies.

    VALIDATED, NOT ASSUMED, the same way black_scholes.gamma() was:
    against 3,227 real Dhan-reported deltas from live recorded snapshots
    (2026-08-17..27), median absolute relative error 4.2%. That error is
    worst on far-OTM strikes where delta is tiny and a relative error
    means little; on what actually matters -- the resulting stop
    distance, after MIN_STOP_PCT/MAX_STOP_PCT clamping, over 835 quotes
    inside the real PREMIUM_MIN..PREMIUM_MAX band -- reconstructed delta
    reproduces live's stop to a MEDIAN DIFFERENCE OF 0.000 percentage
    points (mean 0.74pp, p90 2.25pp), versus the ~13pp error of the flat
    30% fallback it replaces. The clamp absorbs most of the delta error,
    which is exactly why the far-OTM tail doesn't propagate.

    Deliberately leaves theta/vega/gamma alone: nothing in the entry or
    exit path reads them, so reconstructing them would be unvalidated
    machinery with no consumer.
    """
    from research import black_scholes as bs

    now = snapshot.timestamp
    filled = 0
    for q in snapshot.chain:
        if q.delta is not None or not q.iv or not q.strike:
            continue
        expiry = resolved_expiry
        if expiry is None:
            expiry = q.expiry
            if not expiry or str(expiry).startswith("rolling:"):
                expiry = hs.nominal_expiry_date(now.date()).isoformat()
        t = bs.time_to_expiry_years(expiry, now)
        if t <= 0:
            continue
        d = bs.delta(snapshot.spot, q.strike, t, q.iv / 100, q.option_type)
        if d:
            q.delta = d
            filled += 1
    return filled


class _StructureCache:
    """
    price_action.analyze() is the expensive part of a cycle, so its result
    is cached. The key must capture EVERYTHING that can change the derived
    structure between two cycles.

    The key used to be (len(candles), candles[-1].timestamp) on the
    reasoning that "the candle series only changes every few minutes".
    That reasoning was wrong, and it silently corrupted every backtest
    ever run through run_policy() until 2026-08-28.

    The LAST candle is still FORMING. Within one minute-candle's life the
    recorder captures many cycles, and that candle's high/low/close/volume
    keep moving while its timestamp and the list length stay put -- so the
    old key was identical across all of them. Real example, NIFTY
    2026-08-17, all three keying to (46, 13:00:00):

        13:00:18   high 24339.30   close 24338.85   vol   256,492
        13:01:21   high 24347.45   close 24337.10   vol   952,252
        13:02:03   high 24347.45   close 24343.25   vol 1,284,466

    The structure computed at 13:00:18 was therefore served unchanged
    until a NEW candle appeared ~5 minutes later, so the backtest scanned
    on market structure up to a full candle stale. LIVE has no such cache
    -- main_live*.py calls analyze_with_context()/compute_atr() fresh on
    every cycle (main_live_sentinel.py ~line 219) -- so this was a pure
    backtest-vs-live divergence, in the direction of the backtest seeing a
    staler market than the live process it was supposed to be modelling.

    Measured cost of the stale read, same 2026-08-17 13:02:03 cycle: NIFTY
    24300 CE scored 6.0 live (cleared MIN_CONVICTION_SCORE_TO_TRACK=5.0,
    and live really did trade it) but 3.0 through the stale cache -- below
    the bar, so no backtest ever took it. Verified as cause directly:
    same cycle, same candles, cold cache 6.0 / warm cache 3.0.

    Same class of silent backtest/live divergence as the expiry_day_rules
    "rolling:" label bug documented in Policy.use_expiry_day_rules -- and
    found the same way, by refusing to explain away a live-vs-replay
    mismatch as timing noise.

    Keying on the last candle's OHLCV as well makes a forming candle
    invalidate the cache exactly when its data actually moves, which is
    the behaviour live gets for free by not caching at all. The cache
    still does its job: it holds across cycles where nothing changed.
    """

    def __init__(self):
        self._key = None
        self._value = ([], None, None)

    def get(self, candles):
        if not candles:
            return [], None, None
        last = candles[-1]
        key = (len(candles), last.timestamp, last.open, last.high,
               last.low, last.close, getattr(last, "volume", None))
        if key != self._key:
            try:
                levels, context = price_action.analyze_with_context(candles)
                atr = price_action.compute_atr(candles)
            except Exception:
                levels, context, atr = [], None, None
            self._key, self._value = key, (levels, context, atr)
        return self._value


def day_is_complete(cycles, close_time: str = "15:25") -> bool:
    """
    Does this recorded day run to (near) the market close?

    A day recorded while the session was still live ends mid-flight, and
    every unresolved trade then force-closes at the truncation point. The
    effect is severe and silent: outcomes become 100% EOD_CLOSE, no
    target is ever reached, so every R:R variant returns IDENTICAL
    numbers and any "stop after a loss" rule looks brilliant because it
    is stopping on an artifact. Results from a partial day are not weak
    evidence -- they are no evidence.
    """
    if not cycles:
        return False
    last_ts = cycles[-1][0].timestamp
    return last_ts.time() >= _parse_hhmm(close_time)


def warn_if_incomplete(day: str, cycles) -> Optional[str]:
    if day_is_complete(cycles):
        return None
    last = cycles[-1][0].timestamp if cycles else None
    return (
        f"WARNING: {day} is INCOMPLETE (last cycle {last.time() if last else 'n/a'}). "
        f"Unresolved trades force-close at that point, so outcomes collapse to "
        f"EOD_CLOSE and R:R comparisons become meaningless. Do not draw conclusions."
    )


def correlated_cluster_blocked(setup, positions: list, ts, policy: Policy) -> bool:
    """
    True if `setup` should be rejected because it's the same underlying
    bet as an already-open position, under whichever of policy's two
    cluster caps is set (both None -> always False, today's real
    behaviour). See Policy's own comment for the real sessions that
    motivated this.

    Same causality rule as risk_state_at: a position only counts as
    "open" during opened_ts <= ts < closed_ts, even though `positions`
    already holds its fully-resolved outcome (every trade here is
    resolved instantly by walking forward through recorded prices).

    If policy.cluster_window_minutes is set (Sentinel v1.1-dev), a
    same-direction position only counts toward EITHER cap if it was
    also opened within that many minutes of `ts` -- narrowing "blocks
    for its entire open lifetime" down to "blocks only while the same
    burst is plausibly still happening."
    """
    if policy.max_open_per_direction is None and policy.strike_adjacency_band_points is None:
        return False
    open_same_direction = [
        p for p in positions
        if p["key"][1] == setup.option_type
        and p["opened_ts"] <= ts
        and (p["closed_ts"] is None or p["closed_ts"] > ts)
    ]
    if policy.cluster_window_minutes is not None:
        cutoff = ts - timedelta(minutes=policy.cluster_window_minutes)
        open_same_direction = [p for p in open_same_direction if p["opened_ts"] >= cutoff]
    if policy.max_open_per_direction is not None and len(open_same_direction) >= policy.max_open_per_direction:
        return True
    # Inclusive (`<=`), fixed 2026-08-17 to match the live gate -- see
    # trade_tracker.cluster_cap_blocks()'s own docstring for why the
    # exact-boundary case is the TYPICAL one for Bank Nifty rather than an
    # edge case. Backtest numbers recorded in STRATEGY_VERSIONS.md before
    # this date were produced with the old exclusive comparison.
    if policy.strike_adjacency_band_points is not None and any(
        abs(p["key"][0] - setup.strike) <= policy.strike_adjacency_band_points
        for p in open_same_direction
    ):
        return True
    return False


def run_policy(day: str, policy: Policy, verbose: bool = False, blocked_reversal_sink: list = None) -> list:
    """
    Replay a day under `policy`, returning the simulated trades it would
    have taken. Journal writes are suppressed throughout -- this touches
    the real trade record under no circumstances.

    blocked_reversal_sink: optional list, appended with one record per
    opposite-direction-gate block that ALSO cleared every other gate (see
    the check's own comment below) -- {"ts", "blocked_key", "blocked_entry",
    "blocking_keys"}. For research/reversal_exit_study.py: was the
    opposite-direction signal that got blocked also useful information for
    exiting the position(s) it was blocked BY, earlier than their own
    stop/target/EOD? Default None -- zero cost/behavior change for every
    existing caller.
    """
    from contextlib import contextmanager
    from pathlib import Path as _Path

    @contextmanager
    def _config_overrides(overrides):
        """Apply a live process's own config patches for the duration of a
        replay, the same way that process applies them -- but restore them
        afterwards, since a backtest must never leave global state changed
        for whatever runs next in the same interpreter."""
        if not overrides:
            yield
            return
        _MISSING = object()
        previous = {k: getattr(config, k, _MISSING) for k in overrides}
        for k, v in overrides.items():
            setattr(config, k, v)
        try:
            yield
        finally:
            for k, old in previous.items():
                if old is _MISSING:
                    delattr(config, k)
                else:
                    setattr(config, k, old)

    # Only pass the extra kwargs when they are actually non-default: the
    # NIFTY path then calls load_day(day) exactly as it always has, which
    # keeps every existing caller and test stub working unchanged.
    load_kwargs = {}
    if policy.snapshot_dir:
        load_kwargs["snapshot_dir"] = _Path(policy.snapshot_dir)
    if policy.symbol and policy.symbol != "NIFTY":
        load_kwargs["symbol"] = policy.symbol
    cycles = list(snapshot_recorder.load_day(day, **load_kwargs))
    if not cycles:
        return []

    # Both enrichments must happen BEFORE build_price_index(), which
    # snapshots bid/ask/ltp into the per-contract price series that every
    # later exit is priced off. Filling the book afterwards would give
    # entries a spread and exits none.
    if policy.reconstruct_missing_greeks:
        for _snap, _c, _m in cycles:
            fill_missing_book(_snap)

    index = build_price_index(cycles)
    cache = _StructureCache()
    start, end = _parse_hhmm(policy.start_time), _parse_hhmm(policy.end_time)
    min_score, target_rr = policy.resolved_min_score(), policy.resolved_target_rr()

    trades = []
    # Parallel record of what each trade meant for RISK, in the shape
    # risk_state_at needs. Kept alongside `trades` rather than on
    # ShadowTrade because it is bookkeeping for the gates, not part of the
    # result a caller consumes.
    positions = []
    open_until = None       # timestamp the current position closes at
    traded_keys = set()
    # Timestamp a real stop-out becomes KNOWN, not the timestamp of the
    # trade that caused it. A trade opened at 09:30 that doesn't hit its
    # stop until 11:00 cannot inform a decision made at 09:45 -- yet
    # because every trade here is resolved instantly by walking forward
    # through recorded prices, a naive "stop for the day" flag set the
    # moment a losing trade resolves would retroactively block entries
    # that, in real time, happened BEFORE the loss was knowable. Gating
    # on this timestamp instead of a boolean fixed a look-ahead bug this
    # exact policy had: EOD_CLOSE outcomes (only knowable at 15:30, by
    # which point the day is over anyway) were also wrongly counted as
    # "a loss" that ended the day, when only a genuine stop-out (outcome
    # == "LOSS") is a discrete, real-time event a live trader would act on.
    loss_known_at = None
    spot_history = []       # (ts, spot) for the extension guard's lookback window

    with tt.journal_writes_disabled(), _config_overrides(policy.config_overrides):
        for snapshot, candles, _meta in cycles:
            ts = snapshot.timestamp
            if snapshot.spot:
                spot_history.append((ts, snapshot.spot))
            if loss_known_at is not None and ts >= loss_known_at:
                break
            if not (start <= ts.time() <= end):
                continue
            if policy.max_trades_per_day and len(trades) >= policy.max_trades_per_day:
                break
            if policy.one_at_a_time and open_until and ts <= open_until:
                continue

            if policy.reconstruct_missing_greeks:
                fill_missing_delta(snapshot)

            levels, context, atr = cache.get(candles)
            try:
                snapshot.oi_analysis = oi_analytics.analyze(snapshot.chain, snapshot.spot)
            except Exception:
                snapshot.oi_analysis = None

            setups = scan(snapshot, price_levels=levels, context=context)
            if not setups:
                continue
            # scan() returns candidates ranked by ITS score, and the loop
            # below takes the first that clears every gate. Under a rescore
            # that ranking is stale, so the variant would still be picking
            # by the baseline's preference and only re-scoring the winner --
            # measuring almost nothing. Re-rank on the score actually in use.
            if policy.rescore:
                setups = sorted(setups, key=policy.rescore, reverse=True)
            bias_label, bias_score, _r = compute_market_bias(snapshot, context)

            for setup in setups:
                key = (setup.strike, setup.option_type)
                if not policy.allow_repeat_strike and key in traded_keys:
                    continue

                if correlated_cluster_blocked(setup, positions, ts, policy):
                    continue

                # Live's expiry-day discipline: a raised bar and a 14:00
                # cutoff on same-day-expiry contracts (trade_tracker.
                # expiry_day_rules(), enforced live by try_open_new_trade()
                # -- see Policy.use_expiry_day_rules' own comment for why
                # this was missing from here until 2026-08-26). The bar
                # returned is anchored to config.MIN_CONVICTION_SCORE_TO_TRACK,
                # not this policy's own min_score, matching exactly what
                # expiry_day_rules() computes for every live process --
                # correct for the default policy (min_score=None), and
                # the one thing to know if testing a policy with a custom
                # min_score alongside this flag.
                #
                # setup.expiry on RECONSTRUCTED data is the raw request
                # label historical_source.py stores ("rolling:week1"),
                # never a real calendar date -- confirmed directly against
                # a live sample. expiry_day_rules() compares its `expiry`
                # argument to now.date().isoformat(), so passed the raw
                # label it can NEVER match and silently never fires. Every
                # backtest ever run through shadow.py before this fix
                # therefore also never actually detected an expiry day,
                # even after use_expiry_day_rules was added above in this
                # same investigation. Resolved via the same function
                # gamma_exposure.py already uses for this exact problem.
                effective_min_score = min_score
                if policy.use_expiry_day_rules:
                    real_expiry = (setup.expiry if not setup.expiry.startswith("rolling:")
                                  else hs.nominal_expiry_date(ts.date()).isoformat())
                    conviction_bar, expiry_blocked = tt.expiry_day_rules(real_expiry, ts)
                    if expiry_blocked:
                        continue
                    effective_min_score = conviction_bar

                base_score = policy.rescore(setup) if policy.rescore else setup.score
                if policy.use_learned_adjustment:
                    adjusted, _notes = tt.apply_learned_adjustment(base_score, setup.reasons)
                else:
                    adjusted = base_score
                if policy.use_bias_gate:
                    from scanner import apply_bias_gate
                    blocked, penalty, _note = apply_bias_gate(setup, bias_label, bias_score)
                    if blocked:
                        continue
                    adjusted -= penalty
                if adjusted < effective_min_score:
                    continue

                try:
                    plan = build_plan(snapshot, setup, atr=atr)
                except ValueError:
                    continue
                if plan.lots <= 0:
                    continue
                exposure_pct, daily_loss_pct = risk_state_at(index, positions, ts)
                verdict = check(plan, exposure_pct, daily_loss_pct, "normal")
                if verdict.decision != "APPROVED":
                    continue

                # Re-derive the target from the policy's R:R rather than
                # config's, so R:R variants are testable without touching
                # global state.
                risk_unit = plan.entry - plan.stop
                if risk_unit <= 0:
                    continue
                target = round(plan.entry + risk_unit * target_rr, 2)

                # Opposite-direction gate, checked HERE rather than earlier
                # in the loop (moved 2026-08-27) so that when blocked_reversal_sink
                # is provided, only a setup that ALSO cleared every other
                # gate (expiry, learned adjustment, bias, score bar, risk
                # check) gets recorded -- a genuine signal that would have
                # opened in every respect except this one, not noise that
                # would have been rejected anyway regardless of direction.
                # Reordering has no other effect: nothing between the old
                # and new position mutates `positions`/`trades`, only reads.
                # Deployment cap: would this tie up more premium than the
                # book is allowed to have committed at once?
                if deployment_blocked(
                        positions, ts,
                        plan.entry * getattr(config, "NIFTY_LOT_SIZE", 65) * plan.lots,
                        policy.max_deployed_pct):
                    continue

                # Quiet-regime gate: is today even a day worth trading?
                # Checked here, with the other post-qualification gates, so
                # it only ever rejects a candidate that would have opened.
                if quiet_regime_blocked(candles, ts, policy.regime_baseline,
                                        policy.quiet_regime_block_pctile,
                                        policy.quiet_regime_min_elapsed_pct):
                    continue

                # Extension guard: refuse a fully-qualified signal that is
                # merely chasing a move already made. Placed here, after
                # every other gate, for the same reason the opposite-
                # direction check sits here -- so it only ever rejects a
                # candidate that would genuinely have opened.
                if extension_blocked(setup.option_type, snapshot.spot, spot_history, ts,
                                     policy.extension_guard_pctile,
                                     policy.extension_lookback_minutes):
                    continue

                if policy.use_opposite_direction_gate and opposite_direction_blocked(setup, positions, ts):
                    blocking = [
                        p for p in positions
                        if p["key"][1] != setup.option_type
                        and p["opened_ts"] <= ts
                        and (p["closed_ts"] is None or p["closed_ts"] > ts)
                    ]
                    if blocked_reversal_sink is not None:
                        blocked_reversal_sink.append({
                            "ts": ts, "blocked_key": key, "blocked_entry": plan.entry,
                            "blocking_keys": [p["key"] for p in blocking],
                        })
                    if policy.use_reversal_exit:
                        # research/reversal_exit_study.py's tested hypothesis,
                        # built for real: a fully-qualified OPPOSITE-direction
                        # signal is evidence the market has turned, worth
                        # exiting the blocking position(s) for RIGHT NOW rather
                        # than letting them run to their original stop/target/
                        # EOD close. Conservative version of the idea -- closes
                        # the OLD position(s) but still does not open the NEW
                        # (still-blocked) candidate this cycle, matching
                        # exactly what the study measured ("close now"), not a
                        # direction flip into the new signal.
                        for p in blocking:
                            close_now_px = _price_at(index, p["key"], ts)
                            if close_now_px is None:
                                continue
                            owner = next(
                                (t for t in trades if (t.strike, t.option_type) == p["key"]
                                and t.opened_at == p["opened_ts"].isoformat()),
                                None,
                            )
                            if owner is None:
                                continue
                            _close_trade_early(index, p["key"], p["opened_ts"], ts,
                                              close_now_px, owner, p["lots"])
                            p["closed_ts"] = ts
                            p["net_inr"] = owner.net_inr
                    continue

                trade = ShadowTrade(
                    strike=setup.strike, option_type=setup.option_type,
                    opened_at=ts.isoformat(), entry=plan.entry, stop=plan.stop,
                    target=target, score=setup.score, adjusted_score=round(adjusted, 2),
                    reasons=list(setup.reasons)[:4],
                )
                trade = walk_trade_forward(index, key, ts, trade, plan.lots)
                trades.append(trade)
                positions.append({
                    "key": key,
                    "entry": plan.entry,
                    "stop": plan.stop,
                    "lots": plan.lots,
                    "opened_ts": ts,
                    "closed_ts": datetime.fromisoformat(trade.closed_at) if trade.closed_at else None,
                    "net_inr": trade.net_inr,
                })
                traded_keys.add(key)
                if trade.closed_at:
                    open_until = datetime.fromisoformat(trade.closed_at)
                if verbose:
                    print(f"  {ts.time()} {setup.strike:.0f}{setup.option_type} "
                          f"score {adjusted:.2f} -> {trade.outcome} {trade.net_r:+.2f}R")
                if policy.stop_after_loss and trade.outcome == "LOSS" and trade.closed_at:
                    closed = datetime.fromisoformat(trade.closed_at)
                    if loss_known_at is None or closed < loss_known_at:
                        loss_known_at = closed
                break   # at most one new position per cycle, as live does

    return trades


def score_bucket_performance(day: str, policy: Policy = None, bucket: float = 1.0) -> dict:
    """
    The question live trading cannot cheaply answer: how do candidates at
    EACH score level actually perform? Simulates every distinct
    (strike, type) candidate regardless of whether the bar would have
    let it through, so accepted and rejected setups are measured on the
    same footing.
    """
    policy = policy or Policy(name="all-candidates", min_score=-99, one_at_a_time=False,
                              allow_repeat_strike=False)
    trades = run_policy(day, policy)
    buckets = defaultdict(list)
    for t in trades:
        if t.net_r is None:
            continue
        buckets[round(t.adjusted_score / bucket) * bucket].append(t)
    return buckets


def summarise(trades: list, label: str = "") -> str:
    usable = [t for t in trades if t.net_r is not None]
    if not usable:
        return f"{label}: no trades"

    nets = [t.net_r for t in usable]
    wins = [t for t in usable if t.net_r > 0]
    total_inr = sum(t.net_inr for t in usable)
    return (
        f"{label:<28} n={len(usable):>4}  win={100*len(wins)/len(usable):>5.1f}%  "
        f"expectancy={statistics.mean(nets):+.3f}R  median={statistics.median(nets):+.3f}R  "
        f"total=Rs {total_inr:>9,.0f}"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", help="ISO date to simulate; omit for all recorded days")
    p.add_argument("--min-score", type=float, default=None)
    p.add_argument("--target-rr", type=float, default=None)
    p.add_argument("--start", default="09:15")
    p.add_argument("--end", default="15:30")
    p.add_argument("--max-trades", type=int, default=None)
    p.add_argument("--stop-after-loss", action="store_true")
    p.add_argument("--by-score", action="store_true",
                   help="performance broken down by score bucket, all candidates")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    days = [args.day] if args.day else snapshot_recorder.available_days()
    if not days:
        print("No recorded snapshots yet -- run a live session first.")
        return

    incomplete = []
    for d in days:
        w = warn_if_incomplete(d, list(snapshot_recorder.load_day(d)))
        if w:
            incomplete.append(w)
    for w in incomplete:
        print(w)
    if incomplete:
        print()

    policy = Policy(
        name="cli", min_score=args.min_score, target_rr=args.target_rr,
        start_time=args.start, end_time=args.end,
        max_trades_per_day=args.max_trades, stop_after_loss=args.stop_after_loss,
    )

    if args.by_score:
        allt = defaultdict(list)
        for day in days:
            for b, ts in score_bucket_performance(day).items():
                allt[b].extend(ts)
        print(f"Performance by adjusted-score bucket ({', '.join(days)}):\n")
        print(f"{'score':>7} {'n':>5} {'win%':>7} {'expectancy':>12} {'total Rs':>12}")
        for b in sorted(allt):
            ts = [t for t in allt[b] if t.net_r is not None]
            if not ts:
                continue
            nets = [t.net_r for t in ts]
            w = 100 * sum(1 for x in nets if x > 0) / len(nets)
            print(f"{b:>7.1f} {len(ts):>5} {w:>6.1f}% {statistics.mean(nets):>+11.3f}R "
                  f"{sum(t.net_inr for t in ts):>11,.0f}")
        return

    all_trades = []
    for day in days:
        t = run_policy(day, policy, verbose=args.verbose)
        all_trades.extend(t)
        print(summarise(t, f"{day}"))
    if len(days) > 1:
        print()
        print(summarise(all_trades, "ALL DAYS"))


if __name__ == "__main__":
    main()
