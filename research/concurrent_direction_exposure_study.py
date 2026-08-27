"""
2026-08-27: Bank Nifty Anchor stacked 5 simultaneous CE positions, then --
while the CE side was STILL OPEN -- opened 12 simultaneous PE positions on
top, as spot round-tripped ~1,700pts through the chain. Sentinel's cluster
cap cut the day's loss by 74% (19 trades / -Rs15,014 for Anchor vs 8 trades
/ -Rs3,926 for Sentinel), but re-reading trade_tracker.cluster_cap_blocks()
found it only ever compares a new candidate against ALREADY-OPEN SAME-
DIRECTION positions (`if t["option_type"] != option_type: continue`) --
it has NO awareness of the opposite direction at all. So the specific
thing asked about (a CE position open, and a PE position opened on top of
it while the CE is still live) is NOT something the cluster cap defends
against, in EITHER Anchor or Sentinel, live or backtest -- confirmed by
reading shadow.py's identical counterpart, correlated_cluster_blocked(),
which has the same same-direction-only filter (`p["key"][1] ==
setup.option_type`).

THIS SCRIPT ANSWERS: has this specific pattern -- a same-day, opposite-
direction, OVERLAPPING-in-time position pair -- shown up in the full
historical backtest too, using the identical trade_tracker/shadow logic
that produced today's live trades? RESEARCH ONLY.

WHY NIFTY, NOT BANK NIFTY, EVEN THOUGH TODAY'S INCIDENT WAS BANK NIFTY
--------------------------------------------------------------------------
shadow.py's replay engine is NIFTY-only (no Bank Nifty equivalent exists).
But the code path in question -- open_keys keyed on (strike, option_type)
with no cross-direction check, and cluster_cap_blocks/
correlated_cluster_blocked's identical same-direction-only filter -- is
IDENTICAL between the two indices; nothing about it is NIFTY- or
BankNifty-specific. A NIFTY measurement directly tests whether the SHARED
LOGIC produces this pattern, which is the question asked ("as the logic
is same"), even though it can't reproduce today's specific instrument.

METHOD
------
Replays the full NIFTY history through shadow.run_policy() twice: once
with Anchor's real config (no cluster cap) and once with Sentinel's real
config (strike_adjacency_band_points=200, cluster_window_minutes=30,
NIFTY's actual live values -- see main_live_sentinel.py). For each
resulting trade list, every CE/PE pair with overlapping [opened_at,
closed_at) intervals is a same-day opposite-direction overlap. Reports
how often it happens and whether trades caught in one perform
differently (a real risk signal) or not (just noise).

Run: python -m research.concurrent_direction_exposure_study
"""

import argparse
import json
import math
import statistics
from datetime import datetime

import shadow
import snapshot_recorder


def historical_days() -> list:
    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    return days


def run_policy_over_history(policy: shadow.Policy, days: list) -> list:
    trades = []
    for day in days:
        try:
            trades.extend(shadow.run_policy(day, policy))
        except Exception as e:
            print(f"    {day} failed: {type(e).__name__}", flush=True)
    return trades


def _interval(t) -> tuple:
    return (datetime.fromisoformat(t.opened_at), datetime.fromisoformat(t.closed_at))


def find_opposite_direction_overlaps(trades: list) -> dict:
    """
    Groups trades by day, then checks every CE/PE pair that day for
    overlapping open intervals. Returns per-trade overlap flags plus
    summary counts. O(n^2) within a day, fine at real daily trade counts.
    """
    by_day = {}
    for t in trades:
        if not t.closed_at:
            continue
        by_day.setdefault(t.opened_at[:10], []).append(t)

    overlapped_trades = set()
    overlap_days = set()
    overlap_episodes = 0

    for day, day_trades in by_day.items():
        ce = [t for t in day_trades if t.option_type == "CE"]
        pe = [t for t in day_trades if t.option_type == "PE"]
        for a in ce:
            a_open, a_close = _interval(a)
            for b in pe:
                b_open, b_close = _interval(b)
                if a_open < b_close and b_open < a_close:
                    overlapped_trades.add(id(a))
                    overlapped_trades.add(id(b))
                    overlap_days.add(day)
                    overlap_episodes += 1

    return {
        "overlapped_ids": overlapped_trades,
        "overlap_days": overlap_days,
        "overlap_episodes": overlap_episodes,
    }


def _t_stat_diff(a: list, b: list):
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    sea = statistics.pstdev(a) / math.sqrt(len(a))
    seb = statistics.pstdev(b) / math.sqrt(len(b))
    sed = math.sqrt(sea ** 2 + seb ** 2)
    return round((ma - mb) / sed, 2) if sed > 0 else None


def summarise(label: str, trades: list, days: list) -> dict:
    closed = [t for t in trades if t.outcome]
    overlap = find_opposite_direction_overlaps(closed)
    overlapped = [t for t in closed if id(t) in overlap["overlapped_ids"]]
    clean = [t for t in closed if id(t) not in overlap["overlapped_ids"]]

    rs_overlapped = [t.net_r for t in overlapped if t.net_r is not None]
    rs_clean = [t.net_r for t in clean if t.net_r is not None]

    return {
        "label": label,
        "n_trading_days": len(days),
        "n_trades": len(closed),
        "n_overlap_days": len(overlap["overlap_days"]),
        "pct_days_with_overlap": round(100 * len(overlap["overlap_days"]) / len(days), 2) if days else 0,
        "n_overlap_episodes": overlap["overlap_episodes"],
        "n_trades_in_an_overlap": len(overlapped),
        "pct_trades_in_an_overlap": round(100 * len(overlapped) / len(closed), 2) if closed else 0,
        "mean_r_overlapped": round(statistics.mean(rs_overlapped), 4) if rs_overlapped else None,
        "mean_r_clean": round(statistics.mean(rs_clean), 4) if rs_clean else None,
        "t_overlapped_vs_clean": _t_stat_diff(rs_overlapped, rs_clean),
        "net_inr_overlapped": round(sum(t.net_inr or 0 for t in overlapped), 2),
        "net_inr_clean": round(sum(t.net_inr or 0 for t in clean), 2),
    }


def describe(results: list) -> str:
    lines = ["Same-day opposite-direction (CE+PE) OVERLAP study -- NIFTY, full reconstructed history", ""]
    for r in results:
        lines += [
            f"-- {r['label']} --",
            f"  {r['n_trading_days']} trading days, {r['n_trades']} trades",
            f"  days with an overlap: {r['n_overlap_days']} ({r['pct_days_with_overlap']}% of trading days)",
            f"  overlap episodes (CE/PE pairs that were simultaneously open): {r['n_overlap_episodes']}",
            f"  trades caught in at least one overlap: {r['n_trades_in_an_overlap']} ({r['pct_trades_in_an_overlap']}% of all trades)",
            f"  mean R -- overlapped: {r['mean_r_overlapped']}   clean: {r['mean_r_clean']}   "
            f"t(overlapped-clean): {r['t_overlapped_vs_clean']}",
            f"  net Rs -- overlapped trades: {r['net_inr_overlapped']:,.0f}   clean trades: {r['net_inr_clean']:,.0f}",
            "",
        ]
    lines.append("t(overlapped-clean): negative means trades caught in an opposite-direction overlap")
    lines.append("  did WORSE than trades that weren't -- a real risk signal, not just double the trade count.")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/concurrent_direction_exposure_study.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} reconstructed NIFTY days\n", flush=True)

    anchor_policy = shadow.Policy(name="anchor", use_learned_adjustment=False)
    sentinel_policy = shadow.Policy(name="sentinel", use_learned_adjustment=False,
                                    strike_adjacency_band_points=200, cluster_window_minutes=30)

    results = []
    for policy, label in ((anchor_policy, "Anchor (no cluster cap)"),
                          (sentinel_policy, "Sentinel (200pt/30min cluster cap, NIFTY live values)")):
        print(f"running {label}...", flush=True)
        trades = run_policy_over_history(policy, days)
        print(f"  {len(trades)} trades", flush=True)
        results.append(summarise(label, trades, days))

    print()
    print(describe(results))
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
