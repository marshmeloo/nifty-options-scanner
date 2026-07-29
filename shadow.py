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
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime
from typing import Optional

import config
import costs
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
    min_score: float = None            # None -> config.MIN_CONVICTION_SCORE_TO_TRACK
    target_rr: float = None            # None -> config.DEFAULT_TARGET_RR
    start_time: str = "09:15"
    end_time: str = "15:30"
    max_trades_per_day: int = None     # None -> unlimited
    stop_after_loss: bool = False      # end the day on the first losing trade
    # Defaults MIRROR LIVE. main_live.py allows concurrent positions on
    # different strikes (open_keys is keyed on strike+type), so a shadow
    # default of "one at a time" would silently model a different system
    # and make every comparison against live meaningless.
    one_at_a_time: bool = False        # no new entry while a position is open
    allow_repeat_strike: bool = False  # re-enter the same strike+type later the same day
    use_bias_gate: bool = True

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


def _closeable_price(bid, ltp) -> float:
    """What closing a long would realise: the bid, or LTP if no book."""
    if getattr(config, "USE_BID_ASK_FILLS", False) and bid:
        return bid
    return ltp


def walk_trade_forward(index, key, entry_ts, trade: ShadowTrade, lots: int = 1) -> ShadowTrade:
    """
    Advance a simulated trade through every later recorded cycle until it
    hits target, hits stop, or the day ends. Prices are evaluated on the
    side a real exit would cross.
    """
    series = index.get(key, [])
    peak = trough = trade.entry

    for ts, bid, _ask, ltp in series:
        if ts <= entry_ts:
            continue
        px = _closeable_price(bid, ltp)
        if px is None:
            continue
        peak = max(peak, px)
        trough = min(trough, px)

        outcome = None
        if px >= trade.target:
            outcome = "WIN"
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


class _StructureCache:
    """
    price_action.analyze() is the expensive part of a cycle and the
    candle series only changes every few minutes. Keyed on the last
    candle's timestamp plus the count, which is exactly when the derived
    structure can change.
    """

    def __init__(self):
        self._key = None
        self._value = ([], None, None)

    def get(self, candles):
        if not candles:
            return [], None, None
        key = (len(candles), candles[-1].timestamp)
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


def run_policy(day: str, policy: Policy, verbose: bool = False) -> list:
    """
    Replay a day under `policy`, returning the simulated trades it would
    have taken. Journal writes are suppressed throughout -- this touches
    the real trade record under no circumstances.
    """
    cycles = list(snapshot_recorder.load_day(day))
    if not cycles:
        return []

    index = build_price_index(cycles)
    cache = _StructureCache()
    start, end = _parse_hhmm(policy.start_time), _parse_hhmm(policy.end_time)
    min_score, target_rr = policy.resolved_min_score(), policy.resolved_target_rr()

    trades = []
    open_until = None       # timestamp the current position closes at
    traded_keys = set()
    stopped_for_day = False

    with tt.journal_writes_disabled():
        for snapshot, candles, _meta in cycles:
            if stopped_for_day:
                break
            ts = snapshot.timestamp
            if not (start <= ts.time() <= end):
                continue
            if policy.max_trades_per_day and len(trades) >= policy.max_trades_per_day:
                break
            if policy.one_at_a_time and open_until and ts <= open_until:
                continue

            levels, context, atr = cache.get(candles)
            try:
                snapshot.oi_analysis = oi_analytics.analyze(snapshot.chain, snapshot.spot)
            except Exception:
                snapshot.oi_analysis = None

            setups = scan(snapshot, price_levels=levels, context=context)
            if not setups:
                continue
            bias_label, bias_score, _r = compute_market_bias(snapshot, context)

            for setup in setups:
                key = (setup.strike, setup.option_type)
                if not policy.allow_repeat_strike and key in traded_keys:
                    continue

                adjusted, _notes = tt.apply_learned_adjustment(setup.score, setup.reasons)
                if policy.use_bias_gate:
                    from scanner import apply_bias_gate
                    blocked, penalty, _note = apply_bias_gate(setup, bias_label, bias_score)
                    if blocked:
                        continue
                    adjusted -= penalty
                if adjusted < min_score:
                    continue

                try:
                    plan = build_plan(snapshot, setup, atr=atr)
                except ValueError:
                    continue
                if plan.lots <= 0:
                    continue
                verdict = check(plan, 0.0, 0.0, "normal")
                if verdict.decision != "APPROVED":
                    continue

                # Re-derive the target from the policy's R:R rather than
                # config's, so R:R variants are testable without touching
                # global state.
                risk_unit = plan.entry - plan.stop
                if risk_unit <= 0:
                    continue
                target = round(plan.entry + risk_unit * target_rr, 2)

                trade = ShadowTrade(
                    strike=setup.strike, option_type=setup.option_type,
                    opened_at=ts.isoformat(), entry=plan.entry, stop=plan.stop,
                    target=target, score=setup.score, adjusted_score=round(adjusted, 2),
                    reasons=list(setup.reasons)[:4],
                )
                trade = walk_trade_forward(index, key, ts, trade, plan.lots)
                trades.append(trade)
                traded_keys.add(key)
                if trade.closed_at:
                    open_until = datetime.fromisoformat(trade.closed_at)
                if verbose:
                    print(f"  {ts.time()} {setup.strike:.0f}{setup.option_type} "
                          f"score {adjusted:.2f} -> {trade.outcome} {trade.net_r:+.2f}R")
                if policy.stop_after_loss and (trade.net_r or 0) <= 0:
                    stopped_for_day = True
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
