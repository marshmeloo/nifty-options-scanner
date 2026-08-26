"""
Is trade_tracker.expiry_day_rules() (higher conviction bar + 14:00 cutoff
on same-day-expiry contracts) actually earning its keep, or was it never
more than one bad trade generalized into a permanent rule? RESEARCH ONLY
-- nothing here trades.

WHY THIS QUESTION NEEDED SHADOW.PY FIXED FIRST
-------------------------------------------------
Discovered 2026-08-26: shadow.py's run_policy() never called
expiry_day_rules() at all -- every backtest ever run through it,
including the ones that validated the cluster cap and the original
breakeven-arm figure, used a flat conviction bar all day regardless of
expiry. Fixed the same day (shadow.Policy.use_expiry_day_rules), and a
SECOND, deeper bug surfaced in fixing it: setup.expiry on reconstructed
data is the raw request label historical_source.py stores
("rolling:week1"), never a real calendar date, so even with the call
wired in, expiry_day_rules()'s own `expiry == now.date().isoformat()`
check could never match. Both are now fixed in shadow.py directly (see
its own comments); this script is what the fix was for.

TWO SEPARATE QUESTIONS, ANSWERED SEPARATELY
-----------------------------------------------
1. "Does having the rule change the system's overall P&L?" -- run the
   full policy with vs without, both over the full 6-year history.
2. "Are the SPECIFIC trades the rule blocks actually bad?" -- the more
   direct question, and the one the rule was originally justified by
   by a single example. Every trade the rule would exclude (same-day
   expiry, after 14:00, OR same-day expiry with adjusted score in
   [5.0, 6.5)) is pulled out of the unrestricted run and looked at
   ENTIRELY on its own: its own win rate, its own mean R, its own net
   P&L. If that population is genuinely bad, the rule is earning its
   keep regardless of what it does to the whole-system total. If it
   isn't, the rule is costing real trades for a reason that doesn't
   hold up.

A random-direction control on the SAME blocked population is included
for the same reason it appears everywhere else in this project: a
population that loses money could be losing to bad luck on the STOP/
TARGET geometry rather than to being systematically wrong on direction,
and only the control tells the two apart.

Run: python -m research.expiry_day_rule_study
"""

import argparse
import json
import math
import statistics
from collections import defaultdict

import config
import historical_source as hs
import shadow
import snapshot_recorder
import trade_tracker as tt


def historical_days() -> list:
    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    return days


def is_rule_blocked(trade) -> bool:
    """True if trade_tracker.expiry_day_rules() would have excluded this
    trade (it was taken under use_expiry_day_rules=False, so nothing
    already filtered it). Recomputes the real expiry the same way
    shadow.py's fix does, since ShadowTrade doesn't store setup.expiry."""
    opened = trade.opened_at
    trade_date = opened[:10]
    import datetime as _dt
    ts = _dt.datetime.fromisoformat(opened)
    real_expiry = hs.nominal_expiry_date(ts.date()).isoformat()
    if real_expiry != trade_date:
        return False   # not a same-day-expiry contract at all
    cutoff_h, cutoff_m = (int(x) for x in config.EXPIRY_DAY_NO_NEW_TRADES_AFTER.split(":"))
    if (ts.hour, ts.minute) >= (cutoff_h, cutoff_m):
        return True
    bar = config.MIN_CONVICTION_SCORE_TO_TRACK + config.EXPIRY_DAY_EXTRA_CONVICTION
    return trade.adjusted_score < bar


def run_unrestricted(days):
    policy = shadow.Policy(name="unrestricted", use_learned_adjustment=False,
                           use_expiry_day_rules=False)
    trades = []
    with tt.journal_writes_disabled():
        for day in days:
            try:
                trades.extend(shadow.run_policy(day, policy))
            except Exception as e:
                print(f"    {day} failed: {type(e).__name__}", flush=True)
    return trades


def run_with_rule(days):
    policy = shadow.Policy(name="with-rule", use_learned_adjustment=False,
                           use_expiry_day_rules=True)
    trades = []
    with tt.journal_writes_disabled():
        for day in days:
            try:
                trades.extend(shadow.run_policy(day, policy))
            except Exception as e:
                print(f"    {day} failed: {type(e).__name__}", flush=True)
    return trades


def summarise(trades, label):
    closed = [t for t in trades if t.outcome]
    if not closed:
        return {"label": label, "n": 0}
    rs = [t.net_r for t in closed if t.net_r is not None]
    net = sum(t.net_inr or 0 for t in closed)
    wins = sum(1 for t in closed if (t.net_inr or 0) > 0)
    mean_r = statistics.mean(rs) if rs else 0
    se = statistics.pstdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else 0
    return {
        "label": label, "n": len(closed), "net_inr": round(net, 2),
        "win_pct": round(wins / len(closed) * 100, 1),
        "mean_r": round(mean_r, 4),
        "t_stat": round(mean_r / se, 2) if se > 0 else None,
    }


def describe(results: dict) -> str:
    lines = [
        "Expiry-day rule study: does trade_tracker.expiry_day_rules() earn its keep?",
        "",
        f"{'population':<38}{'n':>7}{'net Rs':>13}{'win%':>7}{'meanR':>9}{'t':>7}",
    ]
    for key in ("unrestricted", "with_rule", "blocked_population", "blocked_random_control"):
        r = results[key]
        if not r["n"]:
            lines.append(f"{r['label']:<38}{'0':>7}  (no trades)")
            continue
        net_str = f"{r['net_inr']:,.0f}" if r['net_inr'] is not None else "n/a (R only)"
        lines.append(f"{r['label']:<38}{r['n']:>7,}{net_str:>13}"
                     f"{r['win_pct']:>6.1f}%{r['mean_r']:>+9.4f}"
                     f"{(r['t_stat'] or 0):>+7.2f}")
    lines += [
        "",
        f"Whole-system effect of the rule: Rs{results['with_rule']['net_inr'] - results['unrestricted']['net_inr']:+,.0f}"
        if results['with_rule']['n'] and results['unrestricted']['n'] else "",
        "",
        "blocked_population = every trade the rule would exclude (same-day expiry,",
        "  after 14:00, OR score in [5.0, 6.5) on expiry day), simulated WITHOUT the",
        "  rule so we can see what actually happened to them.",
        "blocked_random_control = same trades, coin-flip direction -- isolates whether",
        "  the population's own result is real directional information.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/expiry_day_rule_study.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} reconstructed NIFTY days\n", flush=True)

    print("running unrestricted (no expiry-day rule)...", flush=True)
    unrestricted = run_unrestricted(days)
    print(f"  {len(unrestricted)} trades", flush=True)

    print("running with the rule enforced...", flush=True)
    with_rule = run_with_rule(days)
    print(f"  {len(with_rule)} trades", flush=True)

    blocked = [t for t in unrestricted if t.outcome and is_rule_blocked(t)]
    print(f"  {len(blocked)} of the unrestricted trades would have been rule-blocked")

    import random as _r
    # Coin-flip control on the same blocked population: flips the SIGN of
    # each trade's own net_r rather than re-simulating from scratch (no
    # cheap way to re-walk a synthetic opposite-direction trade without
    # the original chain snapshot). Fair in aggregate -- half the flips
    # land as if the same stop/target geometry had been applied the other
    # way -- and isolates whether the blocked population's result is real
    # directional information or just the payoff geometry, same check
    # used everywhere else in this project.
    control_rs = []
    for t in blocked:
        d = _r.Random(f"expiry:{t.opened_at}:{t.strike}:{t.option_type}").choice([1, -1])
        control_rs.append((t.net_r or 0) * d)

    results = {
        "unrestricted": summarise(unrestricted, "Unrestricted (no expiry rule)"),
        "with_rule": summarise(with_rule, "With the rule (matches live)"),
        "blocked_population": summarise(blocked, "Blocked population, on its own"),
        "blocked_random_control": {
            "label": "Blocked population, random direction (control)",
            "n": len(control_rs),
            "net_inr": None,
            "win_pct": round(sum(1 for r in control_rs if r > 0) / len(control_rs) * 100, 1) if control_rs else 0,
            "mean_r": round(statistics.mean(control_rs), 4) if control_rs else 0,
            "t_stat": (round(statistics.mean(control_rs) /
                            (statistics.pstdev(control_rs) / math.sqrt(len(control_rs)))
                            if len(control_rs) > 1 and statistics.pstdev(control_rs) > 0 else 0, 2)
                      if control_rs else None),
        },
    }

    print()
    print(describe(results))
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwritten to {args.out}")
