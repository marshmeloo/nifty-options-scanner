"""
Sweep the iron condor's structural config (premium band, hedge distance)
across all recorded history, one full re-run per combination.

WHY THIS EXISTS
---------------
The baseline config (SHORT_PREMIUM_MIN/MAX=23-30, HEDGE_DISTANCE_POINTS=300)
lost money over 2 years (-Rs 18,941) and 64% of every skipped cycle was a
DATA coverage gap (a leg existed but fell outside the ~500pt reconstructed
window), not a real rejection. Both of those numbers -- SHORT_PREMIUM band
and HEDGE_DISTANCE -- directly control how far from spot the legs sit, so
before concluding "condor doesn't work," it's worth checking whether a
different structural config both (a) fits inside the data we can actually
see and (b) performs better.

WHY EACH COMBINATION IS A FULL RE-RUN
--------------------------------------
Same reasoning as sweep_threshold.py: run_all() takes at most one new
condor per day and the FIRST candidate that clears every gate wins the
slot, so changing the premium band changes WHICH candidate is found, not
just whether it survives afterward. Post-hoc filtering of a single run's
trades would give the wrong answer.

WHAT THIS CAN AND CANNOT SETTLE
--------------------------------
It measures how gross expectancy AND coverage-gap rate vary with these
two structural parameters, over 493 reconstructed days. It cannot prove
an edge exists (LTP fills, in-sample, and see historical_source.py's own
caveats), and picking the best-looking cell out of a grid is exactly the
kind of selection that manufactures results that don't survive contact
with live trading -- read a monotonic trend across neighbouring cells as
meaningful, a single standout cell as noise, same rule as
sweep_threshold.py.
"""

import argparse
import json
import math
import statistics

import config_condor as ccfg
import shadow_condor as sc
import snapshot_recorder


def historical_days() -> list:
    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    return days


def run_variant(days: list, premium_min: float, premium_max: float,
                hedge_distance: float, monkeypatch_set) -> dict:
    """
    `monkeypatch_set(attr, value)` is a plain setattr-style callable --
    passed in rather than imported so this can be driven from a test with
    pytest's monkeypatch, or standalone with a tiny local shim (see main).
    """
    monkeypatch_set(ccfg, "SHORT_PREMIUM_MIN", premium_min)
    monkeypatch_set(ccfg, "SHORT_PREMIUM_MAX", premium_max)
    monkeypatch_set(ccfg, "HEDGE_DISTANCE_POINTS", hedge_distance)

    condors, cov = sc.run_all(days, sc.CondorPolicy())
    usable = [c for c in condors if c.pnl_inr is not None]

    total_skips = cov["no_candidate"] + cov["coverage_gap"] + cov["risk_rejected"]
    coverage_gap_pct = round(100 * cov["coverage_gap"] / total_skips, 1) if total_skips else None

    if not usable:
        return {"n": 0, "coverage_gap_pct": coverage_gap_pct, "skips": cov}

    pnls = [c.pnl_inr for c in usable]
    mean = statistics.mean(pnls)
    se = statistics.pstdev(pnls) / math.sqrt(len(pnls)) if len(pnls) > 1 else float("nan")
    eq = peak = dd = 0.0
    for c in sorted(usable, key=lambda x: x.opened_at):
        eq += c.pnl_inr
        peak = max(peak, eq)
        dd = min(dd, eq - peak)

    return {
        "n": len(usable),
        "win_pct": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 1),
        "avg_inr": round(mean),
        "total_inr": round(sum(pnls)),
        "z": round(mean / se, 2) if se and not math.isnan(se) and se > 0 else None,
        "max_drawdown_inr": round(dd),
        "coverage_gap_pct": coverage_gap_pct,
        "skips": cov,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--premium-bands", default="23:30,35:45,50:65,70:90",
                   help="comma-separated min:max pairs")
    p.add_argument("--hedge-distances", default="150,200,300",
                   help="comma-separated point values")
    p.add_argument("--out", default="logs/sweep_condor_config.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}\n", flush=True)

    bands = [tuple(float(x) for x in b.split(":")) for b in args.premium_bands.split(",")]
    hedges = [float(h) for h in args.hedge_distances.split(",")]

    orig = {"SHORT_PREMIUM_MIN": ccfg.SHORT_PREMIUM_MIN,
           "SHORT_PREMIUM_MAX": ccfg.SHORT_PREMIUM_MAX,
           "HEDGE_DISTANCE_POINTS": ccfg.HEDGE_DISTANCE_POINTS}

    def _set(mod, attr, value):
        setattr(mod, attr, value)

    results = []
    try:
        for pmin, pmax in bands:
            for hedge in hedges:
                label = f"premium {pmin:.0f}-{pmax:.0f}, hedge {hedge:.0f}"
                print(f"running {label} ...", flush=True)
                r = run_variant(days, pmin, pmax, hedge, _set)
                r.update({"premium_min": pmin, "premium_max": pmax, "hedge_distance": hedge})
                results.append(r)
                if r["n"]:
                    print(f"    n={r['n']:>4} win={r['win_pct']:.1f}% total=Rs {r['total_inr']:,} "
                          f"z={r['z']} maxDD=Rs {r['max_drawdown_inr']:,} "
                          f"coverage_gap={r['coverage_gap_pct']}%", flush=True)
                else:
                    print(f"    no trades (skips={r['skips']})", flush=True)
    finally:
        for attr, val in orig.items():
            setattr(ccfg, attr, val)

    print(f"\n{'premium':>12} {'hedge':>7} {'n':>5} {'win%':>7} {'total Rs':>11} "
          f"{'z':>7} {'maxDD Rs':>11} {'gap%':>6}")
    for r in sorted(results, key=lambda x: -(x.get("total_inr") or -10**12)):
        band = f"{r['premium_min']:.0f}-{r['premium_max']:.0f}"
        if r["n"]:
            print(f"{band:>12} {r['hedge_distance']:>7.0f} {r['n']:>5} {r['win_pct']:>6.1f}% "
                  f"{r['total_inr']:>11,} {str(r['z']):>7} {r['max_drawdown_inr']:>11,} "
                  f"{str(r['coverage_gap_pct']):>6}")
        else:
            print(f"{band:>12} {r['hedge_distance']:>7.0f}  -- no trades --")

    with open(args.out, "w") as f:
        json.dump({"days": len(days), "results": results}, f, indent=2)
    print(f"\nwritten to {args.out}")
    print("\nFills at LTP, in-sample, one grid pass -- read a trend across "
          "neighbouring cells as meaningful, a single standout cell as noise.")


if __name__ == "__main__":
    main()
