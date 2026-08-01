"""
Compare scoring variants over recorded history, at matched trade counts.

WHY NOT A SINGLE SHARED THRESHOLD
---------------------------------
Each variant shifts the score distribution -- momentum_weighted adds up
to +1.5, iv_neutral removes +/-1.0 -- so the same numeric bar selects
wildly different numbers of trades under each. Comparing them at one
fixed threshold would mostly measure how many trades each happened to
take, not whether its ranking is better. Every variant is therefore swept
across its own range and compared at COMPARABLE trade counts.

WHAT DECIDES A WINNER
---------------------
Gross expectancy, not net and not total rupees. Net folds in costs, which
scale with trade count and would flatter whichever variant simply trades
less; total rupees rewards trading more. Gross expectancy per trade
isolates the only thing a re-weighting can actually improve: the quality
of the ranking.

A variant is only interesting if it beats baseline at a SIMILAR trade
count. Beating it while taking a quarter as many trades is the threshold
result already established, not evidence the re-weighting did anything.
"""

import argparse
import json
import math
import statistics

import scorer_variants
import shadow
import sweep_threshold


# Per-variant threshold grids, chosen so each spans a comparable range of
# trade counts rather than a comparable range of score values.
GRIDS = {
    "baseline": [5.0, 5.5, 6.0],
    "momentum_weighted": [5.5, 6.0, 6.5],
    "iv_neutral": [4.5, 5.0, 5.5],
    "support_fixed": [4.5, 5.0, 5.5],
    "combined": [5.0, 5.5, 6.0],
    "momentum_only": [5.0],
}


def stats(trades: list) -> dict:
    usable = [t for t in trades if t.net_r is not None and t.gross_r is not None]
    if not usable:
        return {"n": 0}
    gross = [t.gross_r for t in usable]
    net = [t.net_r for t in usable]
    mg = statistics.mean(gross)
    seg = statistics.pstdev(gross) / math.sqrt(len(gross)) if len(gross) > 1 else float("nan")
    return {
        "n": len(usable),
        "win_pct": round(100 * sum(1 for v in net if v > 0) / len(net), 1),
        "gross_r": round(mg, 4),
        "gross_z": round(mg / seg, 2) if seg and seg > 0 and not math.isnan(seg) else None,
        "net_r": round(statistics.mean(net), 4),
        "net_inr": round(sum(t.net_inr for t in usable)),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/variant_comparison.json")
    args = p.parse_args()

    days = sweep_threshold.historical_days()
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}\n", flush=True)

    results = []
    for name, fn in scorer_variants.ALL.items():
        for threshold in GRIDS[name]:
            policy = shadow.Policy(
                name=f"{name}@{threshold}",
                min_score=threshold,
                use_learned_adjustment=False,   # journal postdates this history
                rescore=None if name == "baseline" else fn,
            )
            trades = []
            for day in days:
                try:
                    trades.extend(shadow.run_policy(day, policy))
                except Exception:
                    pass
            row = stats(trades)
            row["variant"] = name
            row["threshold"] = threshold
            results.append(row)
            if row["n"]:
                print(f"  {name:<18} @{threshold:<4} n={row['n']:>5}  "
                      f"gross={row['gross_r']:+.4f}R (z={row['gross_z']:+.2f})  "
                      f"net={row['net_r']:+.4f}R  Rs {row['net_inr']:>9,}", flush=True)
            else:
                print(f"  {name:<18} @{threshold:<4} no trades", flush=True)

    print(f"\n{'variant':<18} {'thr':>5} {'n':>6} {'win%':>7} "
          f"{'GROSS/trade':>13} {'z':>7} {'net/trade':>11} {'net Rs':>11}")
    for r in sorted(results, key=lambda x: -(x.get("gross_r") or -99)):
        if not r["n"]:
            continue
        print(f"{r['variant']:<18} {r['threshold']:>5.1f} {r['n']:>6} {r['win_pct']:>6.1f}% "
              f"{r['gross_r']:>+12.4f}R {r['gross_z']:>+7.2f} {r['net_r']:>+10.4f}R "
              f"{r['net_inr']:>11,}")

    with open(args.out, "w") as f:
        json.dump({"days": len(days), "results": results}, f, indent=2)
    print(f"\nwritten to {args.out}")
    print("\nCompare rows at SIMILAR n. Beating baseline while trading far less "
          "is the threshold effect already known, not evidence of a better ranking.")
    print("Fills are at LTP (no bid/ask in historical data) -- all figures optimistic.")


if __name__ == "__main__":
    main()
