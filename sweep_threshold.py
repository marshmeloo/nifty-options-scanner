"""
Sweep the momentum scanner's conviction threshold across all recorded
history, one full re-run per threshold.

WHY A RE-RUN PER THRESHOLD, NOT POST-HOC FILTERING
--------------------------------------------------
Filtering an existing trade list by score would give the wrong answer.
run_policy takes AT MOST ONE new position per cycle and breaks out of the
candidate loop as soon as one passes every gate, so raising the bar
doesn't just remove low-score trades -- it changes WHICH candidate gets
taken in cycles where a low-score setup previously won the slot. A
higher threshold can therefore produce trades that do not appear in the
lower-threshold run at all.

WHAT THIS CAN AND CANNOT SETTLE
-------------------------------
It measures how expectancy varies with the bar, over 493 reconstructed
days. It cannot prove an edge exists: the underlying data has no bid/ask
(fills are at LTP, so every number here is optimistic), and picking the
best-looking threshold from a sweep is exactly the kind of selection that
manufactures edges that don't survive contact with live trading. Treat a
monotonic trend across neighbouring thresholds as meaningful and a single
standout bucket as noise until a real forward sample says otherwise.
"""

import argparse
import json
import math
import statistics

import shadow
import snapshot_recorder


def historical_days() -> list:
    """Reconstructed days only -- live recordings are never pooled with them."""
    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    return days


def run_threshold(days: list, min_score: float) -> list:
    policy = shadow.Policy(
        name=f"min_score={min_score}",
        min_score=min_score,
        # Historical data predates the journal these statistics come from;
        # see Policy.use_learned_adjustment.
        use_learned_adjustment=False,
    )
    trades = []
    for day in days:
        try:
            trades.extend(shadow.run_policy(day, policy))
        except Exception as e:
            print(f"    {day} failed: {e}")
    return trades


def stats(trades: list) -> dict:
    usable = [t for t in trades if t.net_r is not None]
    if not usable:
        return {"n": 0}
    nets = [t.net_r for t in usable]
    mean = statistics.mean(nets)
    se = statistics.pstdev(nets) / math.sqrt(len(nets)) if len(nets) > 1 else float("nan")
    return {
        "n": len(usable),
        "win_pct": round(100 * sum(1 for v in nets if v > 0) / len(nets), 1),
        "expectancy_r": round(mean, 4),
        "ci_low": round(mean - 1.96 * se, 3),
        "ci_high": round(mean + 1.96 * se, 3),
        "z": round(mean / se, 2) if se and not math.isnan(se) and se > 0 else None,
        "total_inr": round(sum(t.net_inr for t in usable)),
        "median_r": round(statistics.median(nets), 3),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--thresholds", default="4.0,4.5,5.0,5.5,6.0,6.5",
                   help="comma-separated conviction scores to test")
    p.add_argument("--out", default="logs/threshold_sweep.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}\n")

    results = {}
    for raw in args.thresholds.split(","):
        threshold = float(raw)
        print(f"running min_score={threshold} ...", flush=True)
        results[threshold] = stats(run_threshold(days, threshold))
        r = results[threshold]
        if r["n"]:
            print(f"    n={r['n']} win={r['win_pct']}% exp={r['expectancy_r']:+.4f}R "
                  f"CI[{r['ci_low']:+.3f},{r['ci_high']:+.3f}] total=Rs {r['total_inr']:,}",
                  flush=True)
        else:
            print("    no trades", flush=True)

    print(f"\n{'score':>6} {'n':>5} {'win%':>7} {'expectancy':>12} "
          f"{'95% CI':>20} {'z':>6} {'total Rs':>12} {'trades/day':>11}")
    for threshold in sorted(results):
        r = results[threshold]
        if not r["n"]:
            print(f"{threshold:>6.1f} {0:>5}  -- no trades --")
            continue
        ci = f"[{r['ci_low']:+.3f},{r['ci_high']:+.3f}]"
        print(f"{threshold:>6.1f} {r['n']:>5} {r['win_pct']:>6.1f}% "
              f"{r['expectancy_r']:>+11.4f}R {ci:>20} {str(r['z']):>6} "
              f"{r['total_inr']:>12,} {r['n']/len(days):>11.2f}")

    with open(args.out, "w") as f:
        json.dump({"days": len(days), "from": days[0], "to": days[-1],
                   "results": {str(k): v for k, v in results.items()}}, f, indent=2)
    print(f"\nwritten to {args.out}")
    print("\nFills are at LTP (historical data has no bid/ask), so every "
          "expectancy above is an optimistic ceiling, not an estimate.")


if __name__ == "__main__":
    main()
