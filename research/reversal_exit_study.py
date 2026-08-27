"""
The opposite-direction gate (Anchor v1.1) blocks a new candidate when it's
on the OPPOSITE side of an already-open position. Right now that blocked
signal is just discarded. Follow-up idea raised after the gate shipped:
if the scanner is producing a genuine, fully-qualified signal in the
OPPOSITE direction to a position we're already holding, isn't that
itself evidence the market has turned -- worth exiting the open position
early on, rather than letting it ride to its original stop/target/EOD?
RESEARCH ONLY -- nothing here trades, and nothing here is wired into any
live process regardless of the result.

WHY THIS IS A DIFFERENT QUESTION FROM THE GATE ITSELF
---------------------------------------------------------
The gate only ever protects the SECOND trade (the one that never opens).
It does nothing for the FIRST trade, which keeps running on its original
thesis even after the scanner has produced fresh, real evidence the
market's character may have flipped. If a meaningful part of why
opposite-direction-overlap trades underperform (see BACKLOG.md,
research/concurrent_direction_exposure_study.py) is the FIRST leg
bleeding out while spot reverses against it -- not just the blocked
second leg being redundant -- then the gate alone is leaving value on
the table.

METHOD
------
shadow.run_policy() now accepts blocked_reversal_sink (added 2026-08-27
alongside this study, moving the opposite-direction check to AFTER every
other gate so only a fully-qualified signal -- one that would have opened
in every other respect -- gets recorded, not noise that would have been
rejected anyway regardless of direction). Replays Anchor's real v1.1
policy (gate ON, matching what's actually live) across the full 6-year
NIFTY history. For every recorded block, finds the specific already-open
position(s) it was blocked by, and for each (the FIRST such block per
trade only, to avoid double-counting one trade against several later
signals) computes what closing it RIGHT THEN would have realised, using
the exact same price index the backtest itself uses -- no lookahead,
just reading the price that was actually on screen at that moment.

Compares that hypothetical "close on the reversal signal" R against the
trade's own ACTUAL outcome (real stop/target/EOD close) -- a PAIRED
comparison, same trades, two hypothetical futures from the same branch
point. This is exactly the same "measure before deciding" step the gate
itself got before shipping.

Run: python -m research.reversal_exit_study
"""

import argparse
import json
import math
import statistics
from datetime import datetime

import shadow
import snapshot_recorder
from research.concurrent_direction_exposure_study import historical_days


def collect(days: list) -> list:
    """One record per (blocked position, first reversal signal against it)."""
    policy = shadow.Policy(name="anchor_reversal_study", use_learned_adjustment=False)
    records = []

    for day in days:
        cycles = list(snapshot_recorder.load_day(day))
        if not cycles:
            continue

        events = []
        try:
            trades = shadow.run_policy(day, policy, blocked_reversal_sink=events)
        except Exception as e:
            print(f"  {day} failed: {type(e).__name__}", flush=True)
            continue
        if not events:
            continue

        index = shadow.build_price_index(cycles)
        by_key = {}
        for t in trades:
            by_key.setdefault((t.strike, t.option_type), []).append(t)

        seen = set()
        for ev in events:
            ts = ev["ts"]
            for bkey in ev["blocking_keys"]:
                if bkey in seen:
                    continue  # only the FIRST reversal signal per open position
                match = None
                for t in by_key.get(bkey, []):
                    o = datetime.fromisoformat(t.opened_at)
                    c = datetime.fromisoformat(t.closed_at) if t.closed_at else None
                    if o <= ts and (c is None or ts < c):
                        match = t
                        break
                if match is None or match.net_r is None or match.stop is None:
                    continue
                risk_unit = match.entry - match.stop
                if risk_unit <= 0:
                    continue
                close_now_px = shadow._price_at(index, bkey, ts)
                if close_now_px is None:
                    continue
                seen.add(bkey)

                close_now_r = round((close_now_px - match.entry) / risk_unit, 4)
                inr_per_r = (match.net_inr / match.net_r) if match.net_r else None
                closed_ts = datetime.fromisoformat(match.closed_at) if match.closed_at else None

                records.append({
                    "day": day, "key": bkey,
                    "opened_at": match.opened_at, "reversal_ts": ts.isoformat(),
                    "minutes_into_trade": round((ts - datetime.fromisoformat(match.opened_at)).total_seconds() / 60, 1),
                    "minutes_left_if_held": round((closed_ts - ts).total_seconds() / 60, 1) if closed_ts else None,
                    "close_now_r": close_now_r,
                    "close_now_inr_approx": round(close_now_r * inr_per_r, 1) if inr_per_r else None,
                    "actual_r": match.net_r,
                    "actual_inr": match.net_inr,
                    "actual_outcome": match.outcome,
                })
    return records


def _paired_stats(records: list, key_a: str, key_b: str) -> dict:
    diffs = [r[key_a] - r[key_b] for r in records if r.get(key_a) is not None and r.get(key_b) is not None]
    if len(diffs) < 2:
        return {"n": len(diffs), "mean_diff": None, "t_stat": None}
    mean_diff = statistics.mean(diffs)
    se = statistics.pstdev(diffs) / math.sqrt(len(diffs))
    return {
        "n": len(diffs),
        "mean_diff": round(mean_diff, 4),
        "t_stat": round(mean_diff / se, 2) if se > 0 else None,
    }


def summarise(records: list) -> dict:
    close_now_r = [r["close_now_r"] for r in records]
    actual_r = [r["actual_r"] for r in records]
    early = [r for r in records if r["minutes_into_trade"] < 90]
    late = [r for r in records if r["minutes_into_trade"] >= 90]

    return {
        "n": len(records),
        "mean_close_now_r": round(statistics.mean(close_now_r), 4) if close_now_r else None,
        "mean_actual_r": round(statistics.mean(actual_r), 4) if actual_r else None,
        "win_rate_close_now_pct": round(100 * sum(1 for r in close_now_r if r > 0) / len(close_now_r), 1) if close_now_r else None,
        "win_rate_actual_pct": round(100 * sum(1 for r in actual_r if r > 0) / len(actual_r), 1) if actual_r else None,
        "paired": _paired_stats(records, "close_now_r", "actual_r"),
        "median_minutes_left_if_held": round(statistics.median(
            [r["minutes_left_if_held"] for r in records if r["minutes_left_if_held"] is not None]
        ), 1) if records else None,
        "early_signal_lt_90min": {
            "n": len(early),
            "mean_close_now_r": round(statistics.mean([r["close_now_r"] for r in early]), 4) if early else None,
            "mean_actual_r": round(statistics.mean([r["actual_r"] for r in early]), 4) if early else None,
        },
        "late_signal_ge_90min": {
            "n": len(late),
            "mean_close_now_r": round(statistics.mean([r["close_now_r"] for r in late]), 4) if late else None,
            "mean_actual_r": round(statistics.mean([r["actual_r"] for r in late]), 4) if late else None,
        },
    }


def describe(summary: dict) -> str:
    lines = [
        f"Reversal-exit study: {summary['n']:,} blocked-position events (first reversal signal per open trade)",
        "",
        f"mean R  close-now: {summary['mean_close_now_r']:+.4f}   actual (held to stop/target/EOD): {summary['mean_actual_r']:+.4f}",
        f"win%    close-now: {summary['win_rate_close_now_pct']}%   actual: {summary['win_rate_actual_pct']}%",
        f"paired difference (close_now - actual): mean {summary['paired']['mean_diff']:+.4f}R, "
        f"t={summary['paired']['t_stat']}, n={summary['paired']['n']}",
        f"median minutes left in the trade if held to its real close, from the reversal moment: "
        f"{summary['median_minutes_left_if_held']}",
        "",
        "By how early the reversal signal arrived:",
        f"  <90min into the trade   n={summary['early_signal_lt_90min']['n']:>5}  "
        f"close-now {summary['early_signal_lt_90min']['mean_close_now_r']}   "
        f"actual {summary['early_signal_lt_90min']['mean_actual_r']}",
        f"  >=90min into the trade  n={summary['late_signal_ge_90min']['n']:>5}  "
        f"close-now {summary['late_signal_ge_90min']['mean_close_now_r']}   "
        f"actual {summary['late_signal_ge_90min']['mean_actual_r']}",
        "",
        "Positive paired difference (close_now - actual) means exiting ON THE REVERSAL SIGNAL",
        "  would have beaten holding to the trade's real stop/target/EOD outcome -- evidence",
        "  the signal is informative enough to act on, not just noise.",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/reversal_exit_study.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} reconstructed NIFTY days\n", flush=True)

    records = collect(days)
    print(f"{len(records):,} blocked-position events collected\n", flush=True)

    summary = summarise(records)
    print(describe(summary))
    with open(args.out, "w") as f:
        json.dump({"summary": summary, "records": records}, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
