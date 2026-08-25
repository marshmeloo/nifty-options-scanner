"""
Committed, reproducible measurement of config.BREAKEVEN_ARM_R's effect on
NIFTY, over the full reconstructed history. RESEARCH ONLY -- read-only,
journal writes disabled throughout (see shadow.journal_writes_disabled).

WHY THIS EXISTS
---------------
config.py cites a backtest for BREAKEVEN_ARM_R (drawdown 44.8%->15.8%,
net P&L +Rs16.6L->+Rs39.2L "STT-only" / +Rs14.3L->+Rs36.8L "real
spread-inclusive", 10,660 NIFTY trades, 2020-08..2026-08) and
STRATEGY_VERSIONS.md repeats the spread-inclusive figure. NEITHER the
script that produced it nor its methodology survives in this repo --
only the finished HTML report does (research/nifty_momentum_
breakeven_0.5R.html, committed 2026-08-15) -- so the number could not
be checked, only trusted.

Attempting to reproduce it (2026-08-24) surfaced a real, structural
finding, and a real, unresolved discrepancy:

  1. Reconstructed historical quotes carry NO real bid/ask book --
     every quote's `has_book` is False (checked directly against
     2020-08-03's first snapshot: 0/42 quotes). So
     config.USE_BID_ASK_FILLS has NO EFFECT on this data regardless of
     its value: plan_generator.build_plan()'s `use_book = ... and
     quote.has_book` always evaluates False, and every fill is LTP.
     There is therefore no honest way to reproduce a "real spread-
     inclusive" number from this data source AT ALL -- it can only
     ever be the LTP/"STT-only" variant. Any spread-inclusive figure
     must have come from an assumed slippage number layered on top,
     not from real recorded spreads (there are none to use).

  2. Even restricted to the STT-only/LTP comparison -- where THIS
     script's zero-rule baseline matches the documented one almost
     exactly (see below) -- the SIZE of the breakeven-arm's benefit
     does not reproduce. Documented improvement: +Rs22.6L. This
     script's: roughly +Rs2L, an order of magnitude smaller. Tracing
     individual trades (see research/ratchet_study.py's development)
     shows the mechanism behaving exactly as designed -- some trades
     that would have recovered to a win get exited near breakeven
     instead, which is the correct, expected cost of the rule -- but
     nothing was found that explains the missing ~Rs20L.

CONCLUSION: this script's own numbers are the only ones in this repo
that can currently be checked by rerunning them. The previously
documented 36.8L/39.2L figures should be treated as UNVERIFIED, not
as ground truth to reconcile against. config.py and STRATEGY_VERSIONS.md
should be corrected to point here instead of to a number nobody can
regenerate.

METHOD
------
Reuses shadow.py's real reconstruction (run_policy) so entries, scoring
and gating are the live logic -- only the EXIT rule is swapped between
"no rule" and "breakeven-arm at 0.5R", via the same fixed exit-price
walker as research/ratchet_study.py (exits at the price actually
OBSERVED on the cycle that noticed the breach, never at the trigger
level itself -- matching trade_tracker.py's real trade["exit_ltp"] =
current_ltp behaviour). All NIFTY reconstructed days
(dhan_historical source, snapshot_recorder default symbol="NIFTY").

Also checks the documented "never made a single one of 73 calendar
months worse" claim independently, since it is falsifiable and cheap
to verify alongside everything else here.

Run: python -m research.breakeven_arm_study
"""

import argparse
import json
import statistics
from collections import defaultdict

import shadow
import snapshot_recorder
from research.ratchet_study import summarise, walk_with_ratchet

VARIANTS = {
    "no_rule": [],
    "breakeven_0.5R": [(0.5, 0.0)],
}


def historical_nifty_days() -> list:
    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day, symbol="NIFTY"), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    return days


def run_variant(days, tiers):
    original = shadow.walk_trade_forward
    shadow.walk_trade_forward = (
        lambda index, key, entry_ts, trade, lots=1:
        walk_with_ratchet(index, key, entry_ts, trade, tiers, lots))
    policy = shadow.Policy(name="breakeven-arm-study", use_learned_adjustment=False)
    trades = []
    try:
        for day in days:
            try:
                trades.extend(shadow.run_policy(day, policy))
            except Exception as e:
                print(f"    {day} failed: {type(e).__name__}", flush=True)
    finally:
        shadow.walk_trade_forward = original
    return trades


def monthly_breakdown(trades):
    """{YYYY-MM: net_inr} from opened_at, for the 'no month made worse' check."""
    by_month = defaultdict(float)
    for t in trades:
        if t.outcome and t.net_inr is not None:
            by_month[t.opened_at[:7]] += t.net_inr
    return dict(by_month)


def describe(no_rule, with_rule):
    s0, s1 = summarise(no_rule, "no_rule"), summarise(with_rule, "breakeven_0.5R")
    m0, m1 = monthly_breakdown(no_rule), monthly_breakdown(with_rule)
    months = sorted(set(m0) | set(m1))
    worse = [m for m in months if m1.get(m, 0) < m0.get(m, 0)]

    lines = [
        f"NIFTY, {len(months)} months, reconstructed 2020-08..present",
        "LTP-only fills throughout -- reconstructed data has no real bid/ask book",
        "(has_book is False on every quote), so this is the STT-only variant;",
        "a spread-inclusive number cannot be honestly produced from this data.",
        "",
        f"{'variant':<18}{'n':>7}{'net Rs':>13}{'meanR':>8}{'maxDD Rs':>12}{'win%':>7}",
    ]
    for s in (s0, s1):
        lines.append(f"{s['label']:<18}{s['n']:>7,}{s['net_inr']:>13,.0f}"
                     f"{(s['mean_r'] or 0):>+8.3f}{s['max_drawdown_inr']:>12,.0f}"
                     f"{s['win_pct']:>6.1f}%")
    lines += [
        "",
        f"improvement from the rule: Rs{s1['net_inr'] - s0['net_inr']:+,.0f}",
        f"documented (unverified, 2026-08-15): +Rs22,60,000 (Rs16.6L -> Rs39.2L, STT-only)",
        "",
        f"'never made a month worse' check: {len(worse)}/{len(months)} months WERE worse "
        f"with the rule" + (" -- CLAIM HOLDS" if not worse else " -- claim does NOT hold here"),
    ]
    if worse:
        lines.append("worse months: " + ", ".join(
            f"{m} ({m1.get(m,0)-m0.get(m,0):+,.0f})" for m in worse[:10]))
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/breakeven_arm_study.json")
    p.add_argument("--limit-days", type=int, default=None)
    args = p.parse_args()

    days = historical_nifty_days()
    if args.limit_days:
        days = days[-args.limit_days:]
    print(f"{len(days)} reconstructed NIFTY days\n", flush=True)

    print("running no_rule...", flush=True)
    no_rule = run_variant(days, VARIANTS["no_rule"])
    print("running breakeven_0.5R...", flush=True)
    with_rule = run_variant(days, VARIANTS["breakeven_0.5R"])

    print()
    print(describe(no_rule, with_rule))

    with open(args.out, "w") as f:
        json.dump({
            "no_rule": summarise(no_rule, "no_rule"),
            "breakeven_0.5R": summarise(with_rule, "breakeven_0.5R"),
            "monthly_no_rule": monthly_breakdown(no_rule),
            "monthly_breakeven_0.5R": monthly_breakdown(with_rule),
        }, f, indent=2)
    print(f"\nwritten to {args.out}")
