"""
Sweep the directional spread's structural config (premium band, hedge
distance) across all recorded history, one full re-run per combination.

Same reasoning and same caveats as sweep_condor_config.py -- see that
module's docstring. This one is lower-stakes: the spread's baseline
config already had 0% coverage gap and a positive result
(+Rs 64,236 / 2yr), so this sweep is asking "can it do better," not
"is it salvageable."

BIAS_STRONG_THRESHOLD and PROFIT_TARGET_PCT_OF_MAX_PROFIT /
STOP_LOSS_PCT_OF_MAX_LOSS are deliberately NOT included in this pass --
mixing exit-rule tuning with structural (premium/hedge) tuning in one
grid would make it impossible to tell which dimension drove a result.
Structural first, exit rules as a separate follow-up on whatever
structural config wins here.
"""

import argparse
import json
import math
import statistics

import config_directional_spread as dcfg
import shadow_directional_spread as sds
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
    monkeypatch_set(dcfg, "SHORT_PREMIUM_MIN", premium_min)
    monkeypatch_set(dcfg, "SHORT_PREMIUM_MAX", premium_max)
    monkeypatch_set(dcfg, "HEDGE_DISTANCE_POINTS", hedge_distance)

    spreads = sds.run_all(days, sds.SpreadPolicy())
    usable = [s for s in spreads if s.pnl_inr is not None]
    if not usable:
        return {"n": 0}

    pnls = [s.pnl_inr for s in usable]
    mean = statistics.mean(pnls)
    se = statistics.pstdev(pnls) / math.sqrt(len(pnls)) if len(pnls) > 1 else float("nan")
    eq = peak = dd = 0.0
    for s in sorted(usable, key=lambda x: x.opened_at):
        eq += s.pnl_inr
        peak = max(peak, eq)
        dd = min(dd, eq - peak)

    return {
        "n": len(usable),
        "win_pct": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 1),
        "avg_inr": round(mean),
        "total_inr": round(sum(pnls)),
        "z": round(mean / se, 2) if se and not math.isnan(se) and se > 0 else None,
        "max_drawdown_inr": round(dd),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--premium-bands", default="30:60,40:70,50:85,65:100",
                   help="comma-separated min:max pairs")
    p.add_argument("--hedge-distances", default="100,150,200",
                   help="comma-separated point values")
    p.add_argument("--out", default="logs/sweep_spread_config.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}\n", flush=True)

    bands = [tuple(float(x) for x in b.split(":")) for b in args.premium_bands.split(",")]
    hedges = [float(h) for h in args.hedge_distances.split(",")]

    orig = {"SHORT_PREMIUM_MIN": dcfg.SHORT_PREMIUM_MIN,
           "SHORT_PREMIUM_MAX": dcfg.SHORT_PREMIUM_MAX,
           "HEDGE_DISTANCE_POINTS": dcfg.HEDGE_DISTANCE_POINTS}

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
                          f"z={r['z']} maxDD=Rs {r['max_drawdown_inr']:,}", flush=True)
                else:
                    print("    no trades", flush=True)
    finally:
        for attr, val in orig.items():
            setattr(dcfg, attr, val)

    print(f"\n{'premium':>12} {'hedge':>7} {'n':>5} {'win%':>7} {'total Rs':>11} {'z':>7} {'maxDD Rs':>11}")
    for r in sorted(results, key=lambda x: -(x.get("total_inr") or -10**12)):
        band = f"{r['premium_min']:.0f}-{r['premium_max']:.0f}"
        if r["n"]:
            print(f"{band:>12} {r['hedge_distance']:>7.0f} {r['n']:>5} {r['win_pct']:>6.1f}% "
                  f"{r['total_inr']:>11,} {str(r['z']):>7} {r['max_drawdown_inr']:>11,}")
        else:
            print(f"{band:>12} {r['hedge_distance']:>7.0f}  -- no trades --")

    with open(args.out, "w") as f:
        json.dump({"days": len(days), "results": results}, f, indent=2)
    print(f"\nwritten to {args.out}")
    print("\nFills at LTP, in-sample, one grid pass -- read a trend across "
          "neighbouring cells as meaningful, a single standout cell as noise.")


if __name__ == "__main__":
    main()
