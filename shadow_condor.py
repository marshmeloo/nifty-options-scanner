"""
Shadow backtesting for the iron condor strategy.

WHY THIS IS SEPARATE FROM THE OTHER TWO SHADOW MODULES
--------------------------------------------------------
Momentum is one long leg exited on a price level. The directional spread
is two legs exited early on a % of max profit/loss OR at expiry. The
condor is FOUR legs (two shorts, two hedges) with NO automatic early
exit at all -- condor_tracker.py only STAGES a breach warning for human
review (see its docstring); it never auto-closes. So a condor position
in this backtest only ever resolves one way: hold to its own expiry and
settle there. Modeling an "early exit" here would be inventing a
mechanism the live strategy doesn't have.

Results are kept in their own ledger, never pooled with the other two --
different instrument, different P&L unit, different base rate.

WHAT IS REUSED RATHER THAN REIMPLEMENTED
------------------------------------------
Leg selection, plan pricing, mark-to-market and intrinsic-value expiry
settlement all come from the LIVE modules (condor_scanner,
condor_plan_generator, condor_tracker). The expiry-week bounding
(_nifty_expiry_weekday / _nominal_expiry_date / _window_days) is
imported from shadow_directional_spread.py rather than duplicated --
both strategies face the identical rolling-series identity hazard (see
that module's docstring) and must never be allowed to compute it
differently.

THE CONSTRAINT THAT MATTERS MOST HERE: STRIKE COVERAGE
---------------------------------------------------------
historical_source.py's reconstruction caps at ATM+/-10 strikes (~500pts).
The condor's SHORT_PREMIUM_MIN/MAX (23-30, cheaper and further OTM than
the directional spread's 30-60) plus HEDGE_DISTANCE_POINTS (300 further
beyond that) routinely wants strikes 400-900pts from spot -- outside the
window on a large fraction of cycles. This is NOT the same thing as "no
legitimate setup this week": condor_scanner.find_condor_legs() returning
None because a leg is missing from the data is a DATA GAP, and reporting
it as an ordinary rejected candidate would silently understate how much
of history this backtest can actually see. Every skipped cycle is
therefore classified as either a genuine no-candidate (premium band
didn't line up with anything) or a coverage gap (a candidate existed but
its hedge or short fell outside the reconstructed window), and both
counts are reported -- see coverage_summary().

A SECOND, SMALLER DATA LIMITATION: MID-WEEK ROLLS
----------------------------------------------------
Live's choose_expiry_to_open() rolls straight to the FOLLOWING week's
expiry if the nearest one has less than MIN_DAYS_TO_EXPIRY_TO_OPEN days
left (e.g. run on expiry day itself). historical_source.py's data is a
ROLLING nearest-weekly series -- it has no way to represent "next week's"
chain while this week's is still current, so that roll cannot be
simulated. This backtest instead SKIPS entry entirely on any cycle where
the nearest weekly expiry is too close, rather than pretending to roll
forward onto data that doesn't exist. This slightly understates the
strategy's real trade frequency (the live roll-forward case is a
legitimate, if lower-premium, position this backtest simply never opens)
but never fabricates a contract.

WHAT IS NOT SIMULATED FAITHFULLY
-----------------------------------
  - Fills at LTP (no bid/ask in historical data) -- more optimistic here
    than either other module, since a condor round-trip crosses 8
    leg-transactions (4 to open, 4 to close/settle).
  - Path within a bar.
  - Breach-warning staging is computed and counted for reporting (see
    ShadowCondor.breach_days) but never causes an exit, matching live.
"""

import argparse
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from typing import Optional

import condor_risk_checker
import config_condor as ccfg
import price_action
import snapshot_recorder
from condor_plan_generator import build_condor_plan
from condor_scanner import find_condor_legs
from condor_tracker import _current_leg_prices, _mark_to_market_pnl_inr, check_breach_warning
from shadow_directional_spread import (
    _load_cached,
    _nominal_expiry_date,
    _window_days,
    _ContextCache,
    _parse_hhmm,
)


@dataclass
class CondorPolicy:
    """Rules to evaluate. Defaults mirror config_condor.py."""
    name: str = "default"
    start_time: str = "09:15"
    end_time: str = "15:30"
    one_at_a_time: bool = True   # mirrors MAX_CONCURRENT_POSITIONS = 1
    min_days_to_expiry: int = None   # None -> ccfg.MIN_DAYS_TO_EXPIRY_TO_OPEN
    # Early-exit rules NOT present in the live strategy (condor_tracker.py
    # only stages a breach warning for human review, never auto-closes --
    # see this module's own docstring). None means "hold to expiry",
    # reproducing exact prior behaviour. Non-None values exist so this
    # backtest can MEASURE whether adding either rule would have helped,
    # before any such rule is ever wired into the live tracker -- same
    # "measure before adopting" bar as the direction-chase gate.
    profit_target_pct: float = None            # close at this % of max_profit_inr
    stop_loss_credit_multiple: float = None    # close at a loss of this many x net_credit_inr

    def resolved_min_days_to_expiry(self) -> int:
        if self.min_days_to_expiry is not None:
            return self.min_days_to_expiry
        return ccfg.MIN_DAYS_TO_EXPIRY_TO_OPEN


@dataclass
class ShadowCondor:
    short_ce_strike: float
    short_pe_strike: float
    hedge_ce_strike: float
    hedge_pe_strike: float
    opened_at: str
    net_credit: float
    max_profit_inr: float
    max_loss_inr: float
    nominal_expiry: str
    closed_at: Optional[str] = None
    exit_reason: Optional[str] = None      # always "expiry_settlement" when resolved
    pnl_inr: Optional[float] = None
    peak_pnl_inr: Optional[float] = None
    trough_pnl_inr: Optional[float] = None
    breach_days: int = 0    # cycles where spot was within BREACH_WARNING_BUFFER_POINTS of a short
    cycles_held: int = 0
    # Per-leg entry premiums, so real costs can be charged after the
    # fact (see spread_cost_study.py). net_credit alone cannot give
    # these back, and they matter MORE here than for the 2-leg
    # directional spread: the condor's hedges sit 300pts further OTM
    # and price in single digits, a premium band whose measured
    # percentage spread is several times worse than its shorts'.
    # pnl_inr stays GROSS -- this module has never applied a cost model.
    short_ce_premium: Optional[float] = None
    short_pe_premium: Optional[float] = None
    hedge_ce_premium: Optional[float] = None
    hedge_pe_premium: Optional[float] = None


def _intrinsic(strike: float, option_type: str, spot: float) -> float:
    if option_type == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _settle_at_expiry(snapshot, plan_dict: dict, condor: ShadowCondor,
                      peak: float, trough: float, held: int) -> ShadowCondor:
    """Mirrors condor_tracker.close_position's intrinsic-value fallback exactly."""
    prices = _current_leg_prices(snapshot.chain, plan_dict)
    for key, (strike_key, opt_type) in {
        "short_ce": ("short_ce_strike", "CE"), "short_pe": ("short_pe_strike", "PE"),
        "hedge_ce": ("hedge_ce_strike", "CE"), "hedge_pe": ("hedge_pe_strike", "PE"),
    }.items():
        if prices[key] is None:
            prices[key] = _intrinsic(plan_dict[strike_key], opt_type, snapshot.spot)

    mtm = _mark_to_market_pnl_inr(plan_dict, prices)
    condor.closed_at = snapshot.timestamp.isoformat()
    condor.exit_reason = "expiry_settlement"
    condor.pnl_inr = round(mtm, 2) if mtm is not None else None
    condor.peak_pnl_inr = round(peak, 2)
    condor.trough_pnl_inr = round(trough, 2)
    condor.cycles_held = held
    return condor


def _close_early(snapshot, plan_dict: dict, condor: ShadowCondor,
                 peak: float, trough: float, held: int, mtm: float, reason: str) -> ShadowCondor:
    """
    Close at the CURRENT mark, not intrinsic value -- unlike
    _settle_at_expiry this isn't a forced exchange settlement, it's a
    real order the strategy chose to place while a book still exists.
    """
    condor.closed_at = snapshot.timestamp.isoformat()
    condor.exit_reason = reason
    condor.pnl_inr = round(mtm, 2)
    condor.peak_pnl_inr = round(peak, 2)
    condor.trough_pnl_inr = round(trough, 2)
    condor.cycles_held = held
    return condor


def _walk_condor_forward(remaining_cycles: list, plan_dict: dict,
                         condor: ShadowCondor, policy: "CondorPolicy" = None) -> ShadowCondor:
    """
    Advance an open condor through cycles already bounded to its own
    expiry week (see _window_days) until the window is exhausted, then
    settle. By default (policy=None or both exit fields None) NO early
    exit exists to check for -- condor_tracker.py only stages breach
    warnings for human review, it never auto-closes, and this
    reproduces that exactly. policy.profit_target_pct /
    stop_loss_credit_multiple exist to MEASURE whether either rule
    would help, not because the live tracker has them yet.
    """
    policy = policy or CondorPolicy()
    peak = trough = 0.0
    held = 0
    breach_days_seen = set()
    last_snapshot = None

    profit_target_inr = (
        policy.profit_target_pct / 100.0 * plan_dict["max_profit_inr"]
        if policy.profit_target_pct is not None else None
    )
    stop_loss_inr = (
        -policy.stop_loss_credit_multiple * plan_dict["net_credit_inr"]
        if policy.stop_loss_credit_multiple is not None else None
    )

    for snapshot, _candles, _meta in remaining_cycles:
        last_snapshot = snapshot
        prices = _current_leg_prices(snapshot.chain, plan_dict)
        mtm = _mark_to_market_pnl_inr(plan_dict, prices)
        if mtm is not None:
            held += 1
            peak = max(peak, mtm)
            trough = min(trough, mtm)

            if profit_target_inr is not None and mtm >= profit_target_inr:
                return _close_early(snapshot, plan_dict, condor, peak, trough, held, mtm, "profit_target")
            if stop_loss_inr is not None and mtm <= stop_loss_inr:
                return _close_early(snapshot, plan_dict, condor, peak, trough, held, mtm, "stop_loss")

        if check_breach_warning(snapshot.spot, plan_dict):
            breach_days_seen.add(snapshot.timestamp.date())

    condor.breach_days = len(breach_days_seen)

    if last_snapshot is None:
        return condor  # unresolved -- see run_policy's docstring

    return _settle_at_expiry(last_snapshot, plan_dict, condor, peak, trough, held)


def _scan_day(day: str, policy: CondorPolicy, cycles: list, day_cache: dict,
              all_days: list, open_until, verbose: bool = False) -> tuple:
    """
    Scan one day for a new entry, threading `open_until` (the
    one_at_a_time / MAX_CONCURRENT_POSITIONS cursor) IN and back OUT
    rather than owning it locally -- see
    shadow_directional_spread._scan_day's docstring for the exact bug
    this prevents (there, 29 of 137 positions overlapped illegally
    before this pattern was adopted). A condor position spans an entire
    expiry week, so the risk of a per-day-local cursor silently losing
    track of a still-open position is, if anything, larger here.

    Returns (condors_opened_today, skip_counts, updated_open_until).
    """
    start, end = _parse_hhmm(policy.start_time), _parse_hhmm(policy.end_time)
    min_days = policy.resolved_min_days_to_expiry()

    cache = _ContextCache()
    condors = []
    skips = {"no_candidate": 0, "coverage_gap": 0, "risk_rejected": 0}

    for i, (snapshot, candles, _meta) in enumerate(cycles):
        ts = snapshot.timestamp
        if not (start <= ts.time() <= end):
            continue
        if policy.one_at_a_time and open_until and ts <= open_until:
            continue

        nominal_expiry = _nominal_expiry_date(ts.date())
        if (nominal_expiry - ts.date()).days < min_days:
            # Live rolls to next week's expiry here; our rolling-series
            # data cannot represent that contract, so this cycle is
            # skipped entirely rather than faked. Not counted in
            # skip_counts -- it isn't a rejected candidate, the strategy
            # never even looked for one on this cycle (matches
            # choose_expiry_to_open's own gate, which runs before any
            # leg selection).
            continue

        legs = find_condor_legs(snapshot.chain)
        # Distinguish "premium band matched nothing" from "a leg was
        # found but fell outside the ~500pt reconstructed window" --
        # conflating them would hide how much of history this backtest
        # cannot actually see.
        if legs["short_ce"] is None or legs["short_pe"] is None:
            skips["no_candidate"] += 1
            continue
        if legs["hedge_ce"] is None or legs["hedge_pe"] is None:
            skips["coverage_gap"] += 1
            continue

        plan = build_condor_plan(legs, expiry=nominal_expiry.isoformat())
        if plan is None:
            skips["no_candidate"] += 1
            continue

        # currently_open_positions is always 0 here: one_at_a_time above
        # already blocks reaching this point while a position is open, so
        # this mirrors what live's own concurrency count would read.
        # Matters now specifically because MIN_NET_CREDIT and
        # MAX_CAPITAL_AT_RISK scale directly with whatever premium band
        # and hedge distance a caller is testing -- exactly the
        # parameters a config sweep varies -- so a variant that would be
        # rejected live must be rejected here too, not silently traded.
        verdict = condor_risk_checker.check(plan, currently_open_positions=0)
        if verdict.decision != "APPROVED":
            skips["risk_rejected"] += 1
            continue

        plan_dict = asdict(plan)
        condor = ShadowCondor(
            short_ce_strike=plan.short_ce_strike, short_pe_strike=plan.short_pe_strike,
            hedge_ce_strike=plan.hedge_ce_strike, hedge_pe_strike=plan.hedge_pe_strike,
            opened_at=ts.isoformat(), net_credit=plan.net_credit,
            max_profit_inr=plan.max_profit_inr, max_loss_inr=plan.max_loss_inr,
            nominal_expiry=nominal_expiry.isoformat(),
            short_ce_premium=plan.short_ce_premium,
            short_pe_premium=plan.short_pe_premium,
            hedge_ce_premium=plan.hedge_ce_premium,
            hedge_pe_premium=plan.hedge_pe_premium,
        )

        window = _window_days(all_days, day, nominal_expiry)
        remaining = cycles[i + 1:]
        for later_day in window[1:]:
            remaining = remaining + _load_cached(day_cache, later_day)

        condor = _walk_condor_forward(remaining, plan_dict, condor, policy)
        condors.append(condor)

        if condor.closed_at:
            open_until = datetime.fromisoformat(condor.closed_at)
        if verbose:
            status = (f"{condor.exit_reason} Rs {condor.pnl_inr:+,.0f}"
                      if condor.pnl_inr is not None else "unresolved (data ended)")
            print(f"  {ts.time()} CE {plan.short_ce_strike:.0f}/{plan.hedge_ce_strike:.0f}  "
                  f"PE {plan.short_pe_strike:.0f}/{plan.hedge_pe_strike:.0f}  "
                  f"credit Rs {plan.net_credit:.2f} -> expiry {nominal_expiry} -> {status}")

    return condors, skips, open_until


def run_policy(day: str, policy: CondorPolicy = None, verbose: bool = False,
               day_cache: dict = None, all_days: list = None) -> tuple:
    """
    Scan `day` in isolation, returning (condors, skip_counts) opened that
    day.

    Convenience wrapper for single-day exploration and tests. Does NOT
    enforce one_at_a_time across a day boundary -- for a real multi-day
    backtest use run_all(), which threads that state through the whole
    run; see _scan_day's docstring for why.
    """
    policy = policy or CondorPolicy()
    day_cache = day_cache if day_cache is not None else {}
    all_days = all_days if all_days is not None else snapshot_recorder.available_days()

    cycles = _load_cached(day_cache, day)
    if not cycles:
        return [], {"no_candidate": 0, "coverage_gap": 0, "risk_rejected": 0}

    condors, skips, _open_until = _scan_day(day, policy, cycles, day_cache, all_days,
                                            open_until=None, verbose=verbose)
    return condors, skips


def run_all(days: list = None, policy: CondorPolicy = None, verbose: bool = False) -> tuple:
    """
    Drive the backtest across every day in sequence, carrying
    `open_until` and the day-cycle cache forward across the whole run --
    this is the function real backtests must use. Returns (condors,
    aggregated_skip_counts).
    """
    days = days or snapshot_recorder.available_days()
    day_cache = {}
    open_until = None
    all_condors = []
    all_skips = []

    for day in days:
        cycles = _load_cached(day_cache, day)
        if not cycles:
            continue
        condors, skips, open_until = _scan_day(day, policy or CondorPolicy(), cycles,
                                               day_cache, days, open_until, verbose=verbose)
        all_condors.extend(condors)
        all_skips.append(skips)

    return all_condors, coverage_summary(all_skips)


def summarise(condors: list, label: str = "") -> str:
    usable = [c for c in condors if c.pnl_inr is not None]
    if not usable:
        return f"{label}: no positions"

    pnls = [c.pnl_inr for c in usable]
    wins = [p for p in pnls if p > 0]
    return (
        f"{label:<28} n={len(usable):>4}  win={100*len(wins)/len(usable):>5.1f}%  "
        f"avg=Rs {statistics.mean(pnls):>+9,.0f}  median=Rs {statistics.median(pnls):>+9,.0f}  "
        f"total=Rs {sum(pnls):>11,.0f}"
    )


def coverage_summary(all_skips: list) -> dict:
    """Aggregate skip_counts dicts from many run_policy() calls."""
    total = {"no_candidate": 0, "coverage_gap": 0, "risk_rejected": 0}
    for s in all_skips:
        for k in total:
            total[k] += s.get(k, 0)
    return total


def unresolved_count(condors: list) -> int:
    return sum(1 for c in condors if c.pnl_inr is None)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", help="ISO date; omit for all recorded days")
    p.add_argument("--start", default="09:15")
    p.add_argument("--end", default="15:30")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    days = [args.day] if args.day else snapshot_recorder.available_days()
    if not days:
        print("No recorded snapshots yet.")
        return

    policy = CondorPolicy(name="cli", start_time=args.start, end_time=args.end)

    # run_all(), not a per-day run_policy() loop: one_at_a_time must be
    # enforced across day boundaries now that positions span an entire
    # expiry week -- see _scan_day's docstring.
    all_condors, cov = run_all(days, policy, verbose=args.verbose)

    if len(days) == 1:
        print(summarise(all_condors, days[0]))
    if len(days) > 1:
        by_day = {}
        for c in all_condors:
            by_day.setdefault(c.opened_at[:10], []).append(c)
        for day in days:
            if day in by_day:
                print(summarise(by_day[day], day))
        print()
        print(summarise(all_condors, "ALL DAYS"))
        total_skips = cov["no_candidate"] + cov["coverage_gap"] + cov["risk_rejected"]
        if total_skips:
            gap_pct = 100 * cov["coverage_gap"] / total_skips
            print(f"\nskipped cycles: {cov['no_candidate']} no matching premium, "
                  f"{cov['coverage_gap']} coverage gap (hedge/short outside the "
                  f"~500pt reconstructed window), {cov['risk_rejected']} risk-gate rejected "
                  f"(MIN_NET_CREDIT/MAX_CAPITAL_AT_RISK) -- {gap_pct:.0f}% of all skips were "
                  f"data gaps, not real rejections")
        unresolved = unresolved_count(all_condors)
        if unresolved:
            print(f"unresolved (recorded history ended before their expiry): {unresolved}")
        print("\nNOTE: legs priced at LTP -- historical data carries no bid/ask, "
              "and a condor crosses 8 leg-transactions per round trip.")


if __name__ == "__main__":
    main()
