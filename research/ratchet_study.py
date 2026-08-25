"""
Does a RATCHET stop beat the current breakeven-arm rule? RESEARCH ONLY
-- nothing here trades, no live module is imported for writing.

THE PROBLEM, FROM A REAL SESSION
----------------------------------
On 2026-08-24 the live system took 26 trades. Twelve reached the 2.0R
target and booked ~100% of their peak. Five others peaked between 1.79R
and 1.95R -- just short -- and gave nearly all of it back:

    N 24350PE   peak 1.95R -> exit +0.78R
    N 24000PE   peak 1.91R -> exit -0.31R   (a winner became a loser)
    S 24050PE   peak 1.91R -> exit -0.23R   (same)
    BN 58100PE  peak 1.88R -> exit +0.60R
    BN 58200PE  peak 1.79R -> exit +0.63R

The existing rule (config.BREAKEVEN_ARM_R = 0.5) moves the stop to ENTRY
once a trade has been up 0.5R, and never moves it again. So a trade can
travel to +1.9R and still come all the way back to zero without any rule
objecting. That is exactly what happened twice.

WHY A RATCHET RATHER THAN SCALING OUT
---------------------------------------
The obvious fix is to book half the position at 1.5R. Simulated on that
day's 26 trades it came to -Rs528: the five fades gained +Rs3,605 but
the twelve full winners lost -Rs4,132, because capping a runner at 1.5R
costs more than the fades save on a trending day.

A ratchet keeps the whole position and only raises the FLOOR. It cannot
clip a winner -- a trade that goes on to 2.0R still books 2.0R -- so its
entire effect falls on trades that peak and reverse. That asymmetry is
the reason to prefer it, and it is what this measures.

WHAT IS TESTED
--------------
Tiers of (peak_r_reached, stop_moves_to_r). The live rule is expressible
in the same form as [(0.5, 0.0)], so it is not a special case -- it runs
through identical code as one variant among many, which is the only way
the comparison is honest.

METHOD, AND ITS LIMITS
------------------------
Reuses shadow.py's reconstruction (the same machinery that validated
BREAKEVEN_ARM_R itself) so entries, scoring and gating are the live
logic, and only the EXIT rule differs between variants. Every variant
sees the identical set of entries -- run_policy is called once per day
per variant with the same Policy, so any difference in results is
attributable to the exit rule alone.

Inherits shadow.py's own documented blind spot: prices are checked at
recorded cycles only, so a spike that touched a level between two
snapshots is invisible. This biases stop-like rules OPTIMISTICALLY --
including the ratchet -- so a small improvement here should not be
trusted, only a large and consistent one.

Run: python -m research.ratchet_study
"""

import argparse
import json
import statistics
from collections import defaultdict

import config
import costs
import shadow
import snapshot_recorder

# name -> list of (peak_r_trigger, stop_moves_to_r), ascending by trigger.
# [(0.5, 0.0)] reproduces the CURRENT live rule exactly.
VARIANTS = {
    "live_breakeven_0.5":      [(0.5, 0.0)],
    "no_exit_rule":            [],
    "ratchet_1.0->0.5":        [(0.5, 0.0), (1.0, 0.5)],
    "ratchet_1.5->0.75":       [(0.5, 0.0), (1.5, 0.75)],
    "ratchet_1.5->1.0":        [(0.5, 0.0), (1.5, 1.0)],
    "ratchet_1.2->0.6":        [(0.5, 0.0), (1.2, 0.6)],
    "ratchet_1.0->0.5_1.5->1.0": [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
    "ratchet_tight_3tier":     [(0.5, 0.0), (1.0, 0.6), (1.5, 1.2)],
}


def walk_with_ratchet(index, key, entry_ts, trade, tiers, lots=1):
    """
    shadow.walk_trade_forward, but the stop RATCHETS upward as the trade's
    peak passes each tier. Target and original stop are unchanged.

    Deliberately mirrors trade_tracker.update_open_trades' ordering: the
    target is checked FIRST (an outright win is strictly better and takes
    priority), then the ratcheted floor, then the original stop.
    """
    series = index.get(key, [])
    peak = trough = trade.entry
    risk_unit = trade.entry - trade.stop
    if risk_unit <= 0:
        return trade
    floor_px = None          # ratcheted stop, in price terms

    for ts, bid, _ask, ltp in series:
        if ts <= entry_ts:
            continue
        px = shadow._closeable_price(bid, ltp)
        if px is None:
            continue
        peak = max(peak, px)
        trough = min(trough, px)
        peak_r = (peak - trade.entry) / risk_unit

        # raise the floor to the highest tier this trade's PEAK has cleared
        for trigger_r, stop_r in tiers:
            if peak_r >= trigger_r:
                cand = trade.entry + stop_r * risk_unit
                floor_px = cand if floor_px is None else max(floor_px, cand)

        outcome = None
        if px >= trade.target:
            outcome = "WIN"
        elif floor_px is not None and px <= floor_px:
            outcome = "BREAKEVEN_STOP" if floor_px <= trade.entry else "RATCHET_STOP"
        elif px <= trade.stop:
            outcome = "LOSS"
        if outcome:
            # Exit at the OBSERVED price, never at the floor level itself.
            # An earlier version of this used floor_px for ratchet/breakeven
            # exits, which is always >= the price actually seen and therefore
            # paid every one of those exits a fill nobody could have got. It
            # also disagreed with the live system, which records
            # trade["exit_ltp"] = current_ltp (trade_tracker.py) -- the price
            # observed on the cycle that noticed the breach, not the trigger.
            # With ~2,000 ratchet exits in a full run, that gap is the whole
            # result, so this must stay `px`.
            return shadow._finalise(trade, ts, px, outcome, peak, trough, lots)

    if series:
        last_ts, last_bid, _a, last_ltp = series[-1]
        px = shadow._closeable_price(last_bid, last_ltp)
        if px is not None:
            return shadow._finalise(trade, last_ts, px, "EOD_CLOSE", peak, trough, lots)
    return trade


def run_variant(days, tiers, verbose=False):
    """Same entries for every variant -- only walk_trade_forward differs."""
    original = shadow.walk_trade_forward
    shadow.walk_trade_forward = (
        lambda index, key, entry_ts, trade, lots=1:
        walk_with_ratchet(index, key, entry_ts, trade, tiers, lots))
    policy = shadow.Policy(name="ratchet-test", use_learned_adjustment=False)
    trades = []
    try:
        for day in days:
            try:
                trades.extend(shadow.run_policy(day, policy))
            except Exception as e:
                if verbose:
                    print(f"    {day} failed: {type(e).__name__}")
    finally:
        shadow.walk_trade_forward = original
    return trades


def summarise(trades, label):
    closed = [t for t in trades if t.outcome]
    if not closed:
        return {"label": label, "n": 0}
    nets = [t.net_inr or 0 for t in closed]
    total = sum(nets)

    # equity curve in trade order -> max drawdown
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(closed, key=lambda x: x.closed_at or ""):
        eq += t.net_inr or 0
        peak = max(peak, eq)
        dd = max(dd, peak - eq)

    outcomes = defaultdict(int)
    for t in closed:
        outcomes[t.outcome] += 1
    rs = [t.net_r for t in closed if t.net_r is not None]
    return {
        "label": label, "n": len(closed),
        "net_inr": round(total, 2),
        "mean_r": round(statistics.mean(rs), 4) if rs else None,
        "max_drawdown_inr": round(dd, 2),
        "win_pct": round(sum(1 for x in nets if x > 0) / len(nets) * 100, 1),
        "outcomes": dict(outcomes),
    }


def describe(rows):
    base = next((r for r in rows if r["label"] == "live_breakeven_0.5"), None)
    lines = [
        "Ratchet study -- does raising the stop as a trade matures beat the",
        "current 'move to breakeven at 0.5R and never again' rule?",
        "identical entries in every row; ONLY the exit rule differs",
        "",
        f"{'variant':<28}{'n':>6}{'net Rs':>13}{'meanR':>8}{'maxDD Rs':>12}{'win%':>7}  vs live",
    ]
    for r in sorted(rows, key=lambda r: -(r.get("net_inr") or 0)):
        if not r["n"]:
            continue
        delta = ""
        if base and r["label"] != "live_breakeven_0.5":
            d = r["net_inr"] - base["net_inr"]
            delta = f"{d:>+11,.0f}"
        tag = "  <-LIVE" if r["label"] == "live_breakeven_0.5" else ""
        lines.append(f"{r['label']:<28}{r['n']:>6,}{r['net_inr']:>13,.0f}"
                     f"{(r['mean_r'] or 0):>+8.3f}{r['max_drawdown_inr']:>12,.0f}"
                     f"{r['win_pct']:>6.1f}%{delta}{tag}")
    lines += ["", "outcome mix (how each rule actually closes trades):"]
    for r in sorted(rows, key=lambda r: -(r.get("net_inr") or 0)):
        if not r["n"]:
            continue
        mix = "  ".join(f"{k}:{v}" for k, v in sorted(r["outcomes"].items()))
        lines.append(f"   {r['label']:<28}{mix}")
    lines += [
        "",
        "Reconstructed prices are sampled at recorded cycles, so intra-cycle",
        "spikes are invisible -- this flatters every stop-like rule, ratchet",
        "included. Treat a small edge as noise; only a large, consistent one counts.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/ratchet_study.json")
    p.add_argument("--limit-days", type=int, default=None)
    args = p.parse_args()

    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    if args.limit_days:
        days = days[-args.limit_days:]
    print(f"{len(days)} reconstructed days\n", flush=True)

    rows = []
    for name, tiers in VARIANTS.items():
        print(f"running {name}...", flush=True)
        rows.append(summarise(run_variant(days, tiers), name))
    print()
    print(describe(rows))
    with open(args.out, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\nwritten to {args.out}")
