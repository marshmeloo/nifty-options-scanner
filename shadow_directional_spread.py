"""
Shadow backtesting for the directional credit-spread strategy.

WHY THIS IS SEPARATE FROM shadow.py
-----------------------------------
shadow.py simulates the momentum scanner: one long option, resolved
against a target and a stop on that contract's own price. A credit spread
is a different instrument entirely -- two legs, opened for a CREDIT,
marked to market as the cost to close, and exited on a percentage of max
profit or max loss rather than a price level. Bolting that onto shadow.py
would mean a Policy where half the fields are meaningless for whichever
strategy isn't running.

Results from this module are kept in their own ledger and are never
pooled with momentum's. The two strategies have different instruments,
different P&L units and different base rates; a blended win rate across
them would describe no strategy that exists.

WHAT IS REUSED RATHER THAN REIMPLEMENTED
----------------------------------------
Direction selection, strike selection, plan pricing, mark-to-market and
the managed-exit rule all come from the LIVE modules
(directional_spread_scanner, directional_spread_plan_generator,
directional_spread_tracker). Reimplementing any of them here would let
the backtest and the live system drift apart silently, which would make
every result a description of code that isn't running.

Only the surrounding LOOP is written here -- the part that live gets from
main_directional_spread.py's polling and its state file. State is held in
memory, so no journal or state file is ever touched. That is deliberate:
replay.py once wrote 35 synthetic trades into the real trade journal (see
its docstring), and this module must not be able to repeat that.

WHAT IS NOT SIMULATED FAITHFULLY
--------------------------------
  - Fills. Historical data carries no bid/ask, so legs are priced at LTP.
    A real credit spread sells the short at the BID and buys the hedge at
    the ASK, collecting LESS credit than LTP implies. Results here are
    therefore OPTIMISTIC, and more so than shadow.py's, because a spread
    crosses two spreads at entry and two more at exit.
  - Path within a bar. Exits are evaluated at recorded cycles only, so a
    stop breached and recovered inside one 5-minute bar is invisible.
  - Assignment and expiry settlement mechanics beyond intrinsic value.
"""

import argparse
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional

import config_directional_spread as dcfg
import price_action
import snapshot_recorder
from directional_spread_plan_generator import build_directional_spread_plan
from directional_spread_scanner import find_directional_spread_legs
from directional_spread_tracker import (
    _current_leg_prices,
    _mark_to_market_pnl_inr,
    check_managed_exit,
)
from scanner import compute_market_bias


@dataclass
class SpreadPolicy:
    """Rules to evaluate. Defaults mirror config_directional_spread.py."""
    name: str = "default"
    bias_threshold: float = None      # None -> dcfg.BIAS_STRONG_THRESHOLD
    start_time: str = "09:15"
    end_time: str = "15:30"
    # Live holds one position at a time (the state file has a single
    # "position" slot), so the default must too or this measures a system
    # that doesn't exist.
    one_at_a_time: bool = True
    max_positions_per_day: int = None

    def resolved_bias_threshold(self) -> float:
        if self.bias_threshold is not None:
            return self.bias_threshold
        return dcfg.BIAS_STRONG_THRESHOLD


@dataclass
class ShadowSpread:
    direction: str
    short_strike: float
    hedge_strike: float
    opened_at: str
    net_credit: float
    max_profit_inr: float
    max_loss_inr: float
    bias_label: str
    bias_score: float
    closed_at: Optional[str] = None
    exit_reason: Optional[str] = None      # profit_target / stop_loss / EOD_CLOSE
    pnl_inr: Optional[float] = None
    peak_pnl_inr: Optional[float] = None
    trough_pnl_inr: Optional[float] = None
    cycles_held: int = 0


def _parse_hhmm(s: str) -> dtime:
    h, m = (int(x) for x in s.split(":"))
    return dtime(h, m)


class _ContextCache:
    """
    price_action.analyze_with_context() is the expensive part of a cycle
    and only changes when the candle series does. Keyed on the last
    candle's timestamp plus the count -- exactly when the derived context
    can change. Same approach as shadow.py's _StructureCache.
    """

    def __init__(self):
        self._key = None
        self._value = None

    def get(self, candles):
        if not candles:
            return None
        key = (len(candles), candles[-1].timestamp)
        if key != self._key:
            try:
                _levels, context = price_action.analyze_with_context(candles)
            except Exception:
                context = None
            self._key, self._value = key, context
        return self._value


def _walk_spread_forward(cycles, start_index: int, plan_dict: dict,
                         spread: ShadowSpread) -> ShadowSpread:
    """
    Advance an open spread through later cycles until a managed exit
    fires or the day ends.

    A cycle where either leg has no quote is SKIPPED rather than treated
    as zero P&L. That distinction is the exact bug that made condor MTM
    read "unavailable" on 2026-07-30 -- a missing quote is missing
    information, not a valid mark, and scoring it as flat would invent
    exits that never happened.
    """
    peak = trough = 0.0
    held = 0

    for snapshot, _candles, _meta in cycles[start_index + 1:]:
        prices = _current_leg_prices(snapshot.chain, plan_dict)
        mtm = _mark_to_market_pnl_inr(plan_dict, prices)
        if mtm is None:
            continue
        held += 1
        peak = max(peak, mtm)
        trough = min(trough, mtm)

        reason = check_managed_exit(mtm, plan_dict)
        if reason:
            spread.closed_at = snapshot.timestamp.isoformat()
            spread.exit_reason = reason
            spread.pnl_inr = round(mtm, 2)
            spread.peak_pnl_inr = round(peak, 2)
            spread.trough_pnl_inr = round(trough, 2)
            spread.cycles_held = held
            return spread

    # Never resolved -- mark at the last cycle that had a usable quote,
    # as an end-of-day close would.
    for snapshot, _candles, _meta in reversed(cycles[start_index + 1:]):
        prices = _current_leg_prices(snapshot.chain, plan_dict)
        mtm = _mark_to_market_pnl_inr(plan_dict, prices)
        if mtm is None:
            continue
        spread.closed_at = snapshot.timestamp.isoformat()
        spread.exit_reason = "EOD_CLOSE"
        spread.pnl_inr = round(mtm, 2)
        spread.peak_pnl_inr = round(peak, 2)
        spread.trough_pnl_inr = round(trough, 2)
        spread.cycles_held = held
        return spread

    return spread


def run_policy(day: str, policy: SpreadPolicy = None, verbose: bool = False) -> list:
    """
    Replay one day under `policy`, returning the spreads it would have
    opened. Touches no state file and no journal -- everything is in
    memory.
    """
    policy = policy or SpreadPolicy()
    cycles = list(snapshot_recorder.load_day(day))
    if not cycles:
        return []

    start, end = _parse_hhmm(policy.start_time), _parse_hhmm(policy.end_time)
    threshold = policy.resolved_bias_threshold()

    cache = _ContextCache()
    spreads = []
    open_until = None

    for i, (snapshot, candles, _meta) in enumerate(cycles):
        ts = snapshot.timestamp
        if not (start <= ts.time() <= end):
            continue
        if policy.max_positions_per_day and len(spreads) >= policy.max_positions_per_day:
            break
        if policy.one_at_a_time and open_until and ts <= open_until:
            continue

        # The context derived from candles is what actually moves the bias
        # score -- trend and RSI together are worth +/-1.5 of it. Passing
        # context=None instead leaves PCR as the only input, which scored
        # 0.0 "neutral/range" on all 709 cycles of a real recorded day and
        # made the strategy look like it simply never found a setup.
        context = cache.get(candles)
        try:
            bias_label, bias_score, _reasons = compute_market_bias(snapshot, context)
        except Exception:
            continue
        if bias_label is None or bias_score is None or abs(bias_score) < threshold:
            continue

        legs = find_directional_spread_legs(snapshot.chain, bias_label, bias_score)
        plan = build_directional_spread_plan(
            legs, expiry=snapshot.chain[0].expiry if snapshot.chain else "unknown",
            bias_label=bias_label, bias_score=bias_score,
        )
        if plan is None:
            continue

        from dataclasses import asdict
        plan_dict = asdict(plan)

        spread = ShadowSpread(
            direction=plan.direction,
            short_strike=plan.short_strike,
            hedge_strike=plan.hedge_strike,
            opened_at=ts.isoformat(),
            net_credit=plan.net_credit,
            max_profit_inr=plan.max_profit_inr,
            max_loss_inr=plan.max_loss_inr,
            bias_label=bias_label,
            bias_score=round(bias_score, 2),
        )
        spread = _walk_spread_forward(cycles, i, plan_dict, spread)
        spreads.append(spread)

        if spread.closed_at:
            open_until = datetime.fromisoformat(spread.closed_at)
        if verbose:
            print(f"  {ts.time()} {plan.direction} {plan.short_strike:.0f}/"
                  f"{plan.hedge_strike:.0f} credit Rs {plan.net_credit:.2f} "
                  f"-> {spread.exit_reason} Rs {spread.pnl_inr:+,.0f}")

    return spreads


def summarise(spreads: list, label: str = "") -> str:
    usable = [s for s in spreads if s.pnl_inr is not None]
    if not usable:
        return f"{label}: no positions"

    pnls = [s.pnl_inr for s in usable]
    wins = [p for p in pnls if p > 0]
    return (
        f"{label:<28} n={len(usable):>4}  win={100*len(wins)/len(usable):>5.1f}%  "
        f"avg=Rs {statistics.mean(pnls):>+9,.0f}  median=Rs {statistics.median(pnls):>+9,.0f}  "
        f"total=Rs {sum(pnls):>11,.0f}"
    )


def exit_reason_breakdown(spreads: list) -> dict:
    counts = {}
    for s in spreads:
        if s.exit_reason:
            counts[s.exit_reason] = counts.get(s.exit_reason, 0) + 1
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", help="ISO date; omit for all recorded days")
    p.add_argument("--bias-threshold", type=float, default=None)
    p.add_argument("--start", default="09:15")
    p.add_argument("--end", default="15:30")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    days = [args.day] if args.day else snapshot_recorder.available_days()
    if not days:
        print("No recorded snapshots yet.")
        return

    policy = SpreadPolicy(name="cli", bias_threshold=args.bias_threshold,
                          start_time=args.start, end_time=args.end)

    all_spreads = []
    for day in days:
        s = run_policy(day, policy, verbose=args.verbose)
        all_spreads.extend(s)
        if s:
            print(summarise(s, day))

    if len(days) > 1:
        print()
        print(summarise(all_spreads, "ALL DAYS"))
        print(f"\nexit reasons: {exit_reason_breakdown(all_spreads)}")
        print("\nNOTE: legs priced at LTP -- historical data carries no bid/ask. "
              "A real spread collects less credit than this shows.")


if __name__ == "__main__":
    main()
