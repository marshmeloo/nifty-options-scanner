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

WHY THE WALK IS BOUNDED TO ONE EXPIRY WEEK, NOT JUST "MULTI-DAY"
------------------------------------------------------------------
historical_source.py's data is a ROLLING series: `expiryFlag=WEEK,
expiryCode=1` means "whichever expiry is nearest AT EACH POINT IN TIME",
not a fixed named contract. A (strike, option_type) key that looks
identical across two dates can refer to two DIFFERENT actual contracts if
a weekly rollover happened between them -- the same failure mode as
naively trusting a ticker symbol across a futures roll. Walking a
position past its own real expiry using this data would silently start
pricing it against next week's contract.

The fix is to bound every walk at the position's own nominal expiry date
(see _nominal_expiry_date) and settle there, mirroring exactly what
directional_spread_tracker.close_position does live when expiry is
reached -- never read a cycle from beyond that date for this position,
regardless of how much more history is on disk.

WHAT IS NOT SIMULATED FAITHFULLY
--------------------------------
  - Fills. Historical data carries no bid/ask, so legs are priced at LTP.
    A real credit spread sells the short at the BID and buys the hedge at
    the ASK, collecting LESS credit than LTP implies. Results here are
    therefore OPTIMISTIC, and more so than shadow.py's, because a spread
    crosses two spreads at entry and two more at exit.
  - Path within a bar. Exits are evaluated at recorded cycles only, so a
    stop breached and recovered inside one 5-minute bar is invisible.
  - Exchange holidays are handled by their natural absence from the
    recorded trading-day calendar (see _window_days), not looked up
    explicitly -- if a nominal expiry weekday is a holiday, the window
    simply ends at the last day actually on disk before it, which
    approximates but does not guarantee matching NSE's real
    shift-to-previous-working-day rule in every edge case.
"""

import argparse
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

import config_directional_spread as dcfg
import directional_spread_risk_checker
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

# NIFTY's weekly options expiry moved from Thursday to Tuesday effective
# 2025-09-01 (SEBI circular, October 2024) -- both regimes fall inside
# our 2024-08 to 2026-07 recorded history, so which weekday a position
# settles on depends on WHEN it was opened, not a single constant.
_EXPIRY_WEEKDAY_CHANGE_DATE = date(2025, 9, 1)
_EXPIRY_WEEKDAY_BEFORE = 3   # Thursday
_EXPIRY_WEEKDAY_ON_OR_AFTER = 1   # Tuesday


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
    # None -> dcfg.MAX_NEW_POSITIONS_PER_DAY. Left at None rather than
    # hardcoded 1 so a caller can still ask for something else, but the
    # RESOLVED default mirrors live's real gate. one_at_a_time alone is
    # not enough: it only blocks a new entry while a position is still
    # OPEN, so a position that closes early (a stop_loss hit within
    # minutes) leaves the door open for a second brand-new entry later
    # the same day -- something directional_spread_risk_checker.check()'s
    # opened_today count would refuse live. Found by comparing the
    # backtest's own trade list against this config value: 21 of 158
    # positions across 12 days violated it, contributing Rs 14,303 (15%
    # of the run's total) before this default was added.
    max_positions_per_day: int = None

    def resolved_bias_threshold(self) -> float:
        if self.bias_threshold is not None:
            return self.bias_threshold
        return dcfg.BIAS_STRONG_THRESHOLD

    def resolved_max_positions_per_day(self) -> int:
        if self.max_positions_per_day is not None:
            return self.max_positions_per_day
        return dcfg.MAX_NEW_POSITIONS_PER_DAY


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


def _nifty_expiry_weekday(on_date: date) -> int:
    """Monday=0..Sunday=6, for the regime in effect on `on_date`."""
    return _EXPIRY_WEEKDAY_ON_OR_AFTER if on_date >= _EXPIRY_WEEKDAY_CHANGE_DATE else _EXPIRY_WEEKDAY_BEFORE


def _nominal_expiry_date(entry_date: date) -> date:
    """Next occurrence of the applicable expiry weekday, on or after `entry_date`."""
    target = _nifty_expiry_weekday(entry_date)
    return entry_date + timedelta(days=(target - entry_date.weekday()) % 7)


def _window_days(all_days: list, entry_day: str, expiry_date: date) -> list:
    """
    Trading days from `entry_day` (inclusive) through the last recorded
    trading day on or before `expiry_date`. Drawn from the actual
    recorded calendar rather than generated, so exchange holidays are
    handled by their natural absence (see this module's docstring on the
    holiday-handling caveat).
    """
    expiry_iso = expiry_date.isoformat()
    return [d for d in all_days if entry_day <= d <= expiry_iso]


def _intrinsic(direction: str, strike: float, spot: float) -> float:
    if direction == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _settle_at_expiry(snapshot, plan_dict: dict, spread: ShadowSpread,
                      peak: float, trough: float, held: int) -> ShadowSpread:
    """
    Settle at the position's own nominal expiry, mirroring
    directional_spread_tracker.close_position's expiry-settlement path
    exactly: fall back to intrinsic value for any leg with no quote --
    far-OTM legs often stop trading before market close on expiry day,
    but intrinsic value is still true at settlement.
    """
    prices = _current_leg_prices(snapshot.chain, plan_dict)
    direction = plan_dict["direction"]
    for key, strike_key in (("short", "short_strike"), ("hedge", "hedge_strike")):
        if prices[key] is None:
            prices[key] = _intrinsic(direction, plan_dict[strike_key], snapshot.spot)

    mtm = _mark_to_market_pnl_inr(plan_dict, prices)
    spread.closed_at = snapshot.timestamp.isoformat()
    spread.exit_reason = "expiry_settlement"
    spread.pnl_inr = round(mtm, 2) if mtm is not None else None
    spread.peak_pnl_inr = round(peak, 2)
    spread.trough_pnl_inr = round(trough, 2)
    spread.cycles_held = held
    return spread


def _walk_spread_forward(remaining_cycles: list, plan_dict: dict,
                         spread: ShadowSpread) -> ShadowSpread:
    """
    Advance an open spread through subsequent cycles -- already bounded
    to the position's own expiry week by the caller, see _window_days --
    until a managed exit fires or the window is exhausted, in which case
    it settles at expiry.

    A cycle where either leg has no quote is SKIPPED for the managed-exit
    check rather than treated as zero P&L. That distinction is the exact
    bug that made condor MTM read "unavailable" on 2026-07-30 -- a
    missing quote is missing information, not a valid mark, and scoring
    it as flat would invent exits that never happened.
    """
    peak = trough = 0.0
    held = 0
    last_snapshot = None

    for snapshot, _candles, _meta in remaining_cycles:
        last_snapshot = snapshot
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

    if last_snapshot is None:
        # No recorded data at all inside this position's own expiry
        # week -- e.g. it opened in the final days of the recorded
        # history. Left unresolved (pnl_inr stays None) rather than
        # fabricating a close; every stats function already excludes
        # pnl_inr is None from its sample.
        return spread

    return _settle_at_expiry(last_snapshot, plan_dict, spread, peak, trough, held)


def _load_cached(day_cache: dict, day: str) -> list:
    if day not in day_cache:
        day_cache[day] = list(snapshot_recorder.load_day(day))
    return day_cache[day]


def _scan_day(day: str, policy: SpreadPolicy, cycles: list, day_cache: dict,
              all_days: list, open_until, verbose: bool = False) -> tuple:
    """
    Scan one day for new entries, threading `open_until` (the
    one_at_a_time cursor) IN and back OUT rather than owning it locally.

    THE BUG THIS EXISTS TO PREVENT: a position opened on day N can close
    on day N+2 (this strategy is held overnight to expiry). If
    `open_until` were a fresh local variable reset to None on every call
    -- as it was in the first version of this multi-day walk -- a caller
    driving day N+1 in a SEPARATE call would have no memory that a
    position from day N is still open, and one_at_a_time would silently
    stop being enforced across the day boundary it now needs to span.
    Caught by auditing a real run: 29 of 137 positions overlapped illegally.

    Returns (spreads_opened_today, updated_open_until).
    """
    start, end = _parse_hhmm(policy.start_time), _parse_hhmm(policy.end_time)
    threshold = policy.resolved_bias_threshold()

    cache = _ContextCache()
    spreads = []

    for i, (snapshot, candles, _meta) in enumerate(cycles):
        ts = snapshot.timestamp
        if not (start <= ts.time() <= end):
            continue
        if len(spreads) >= policy.resolved_max_positions_per_day():
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

        # opened_today = len(spreads): matches live's own opened_today
        # counter for this day, since currently_open_positions is always
        # 0 here (one_at_a_time already blocked reaching this point while
        # a position was open). MIN_NET_CREDIT/MAX_CAPITAL_AT_RISK scale
        # directly with whatever premium band and hedge distance is under
        # test, exactly what a config sweep varies -- a variant that
        # would be rejected live must be rejected here too.
        verdict = directional_spread_risk_checker.check(
            plan, currently_open_positions=0, opened_today=len(spreads)
        )
        if verdict.decision != "APPROVED":
            continue

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

        nominal_expiry = _nominal_expiry_date(ts.date())
        window = _window_days(all_days, day, nominal_expiry)
        remaining = cycles[i + 1:]
        for later_day in window[1:]:
            remaining = remaining + _load_cached(day_cache, later_day)

        spread = _walk_spread_forward(remaining, plan_dict, spread)
        spreads.append(spread)

        if spread.closed_at:
            open_until = datetime.fromisoformat(spread.closed_at)
        if verbose:
            status = (f"{spread.exit_reason} Rs {spread.pnl_inr:+,.0f}"
                      if spread.pnl_inr is not None else "unresolved (data ended)")
            print(f"  {ts.time()} {plan.direction} {plan.short_strike:.0f}/"
                  f"{plan.hedge_strike:.0f} credit Rs {plan.net_credit:.2f} "
                  f"-> nominal expiry {nominal_expiry} -> {status}")

    return spreads, open_until


def run_policy(day: str, policy: SpreadPolicy = None, verbose: bool = False,
               day_cache: dict = None, all_days: list = None) -> list:
    """
    Scan `day` in isolation, returning the spreads opened that day.

    Convenience wrapper for single-day exploration and tests. Does NOT
    enforce one_at_a_time across a day boundary -- there is nothing here
    for it to remember a still-open position from a PRIOR call with. For
    a real multi-day backtest, use run_all(), which threads that state
    through the whole run; see _scan_day's docstring for the bug this
    distinction exists to prevent (29 of 137 positions overlapped
    illegally before it was caught).
    """
    policy = policy or SpreadPolicy()
    day_cache = day_cache if day_cache is not None else {}
    all_days = all_days if all_days is not None else snapshot_recorder.available_days()

    cycles = _load_cached(day_cache, day)
    if not cycles:
        return []

    spreads, _open_until = _scan_day(day, policy, cycles, day_cache, all_days,
                                     open_until=None, verbose=verbose)
    return spreads


def run_all(days: list = None, policy: SpreadPolicy = None, verbose: bool = False) -> list:
    """
    Drive the backtest across every day in sequence, carrying
    `open_until` and the day-cycle cache forward across the whole run --
    this is the function real backtests must use. See _scan_day's
    docstring for why a per-day call in isolation cannot enforce
    one_at_a_time once positions span multiple days.
    """
    days = days or snapshot_recorder.available_days()
    day_cache = {}
    open_until = None
    all_spreads = []

    for day in days:
        cycles = _load_cached(day_cache, day)
        if not cycles:
            continue
        spreads, open_until = _scan_day(day, policy or SpreadPolicy(), cycles, day_cache,
                                        days, open_until, verbose=verbose)
        all_spreads.extend(spreads)

    return all_spreads


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


def unresolved_count(spreads: list) -> int:
    """Positions still open when the recorded history ran out -- excluded from every stat."""
    return sum(1 for s in spreads if s.pnl_inr is None)


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

    # run_all(), not a per-day run_policy() loop: one_at_a_time must be
    # enforced across day boundaries now that positions span multiple
    # days -- see _scan_day's docstring.
    all_spreads = run_all(days, policy, verbose=args.verbose)

    if len(days) == 1:
        print(summarise(all_spreads, days[0]))
    if len(days) > 1:
        by_day = {}
        for s in all_spreads:
            by_day.setdefault(s.opened_at[:10], []).append(s)
        for day in days:
            if day in by_day:
                print(summarise(by_day[day], day))
        print()
        print(summarise(all_spreads, "ALL DAYS"))
        print(f"\nexit reasons: {exit_reason_breakdown(all_spreads)}")
        unresolved = unresolved_count(all_spreads)
        if unresolved:
            print(f"unresolved (recorded history ended before their expiry): {unresolved}")
        print("\nNOTE: legs priced at LTP -- historical data carries no bid/ask. "
              "A real spread collects less credit than this shows.")


if __name__ == "__main__":
    main()
