"""
Follow-up to the Hero-Zero study's finding: deliberately picking the
CHEAPEST candidate (Rs1-5) underperformed a same-band random pick,
plausibly because a near-worthless option has already lost the
gamma/vega sensitivity a tail spike needs. RESEARCH ONLY.

THE QUESTION
------------
If cheapness itself is the problem, does a HIGHER premium band do
better -- still deep enough OTM to be a directional bet, but not so
decayed it has nothing left to move with? Sweeps several premium bands
with the SAME selection rule the original study used (farthest-OTM
strike inside MAX_DISTANCE_PTS whose entry LTP falls in the band)
against a BAND-MATCHED random control (same band, same distance cap,
random strike within it) -- tighter than the original study's
any-premium random control, since the question here is specifically
about the effect of the PRICE LEVEL, at each level in turn.

Same data-quality caveat as the original study: MAX_DISTANCE_PTS=300
keeps selection inside the band the 2026-08-04 corruption study
measured as cleaner; higher premium bands naturally select closer-to-
spot strikes anyway (a given distance is cheaper in a low band, pricier
in a high one), so the cap bites less as the band rises.

Run: python -m research.hero_zero_premium_sweep
"""

import argparse
import json
import math
import statistics

from research.hero_zero_study import (
    build_candidates_for_bands, _apply_costs, _t_stat, _z_diff, _skew,
)
import snapshot_recorder
from component_study import _inv_norm

# Rupee bands, roughly log-spaced across what a NIFTY weekly's OTM
# strikes actually price at intraday. [1,5) reruns the original study's
# band through this sweep's own (band-matched, not any-premium) random
# control, so it's a valid point of comparison, not just a rerun.
BANDS = [(1, 5), (5, 10), (10, 20), (20, 40), (40, 80), (80, 150)]


def profile(records: list, label: str) -> dict:
    if not records:
        return {"label": label, "n": 0}
    rets = [r["ret_eod_pct"] for r in records]
    net = [r["net_pnl_inr_per_lot"] for r in records]
    n = len(records)
    return {
        "label": label, "n": n,
        "mean_ret_eod_pct": round(statistics.mean(rets), 2),
        "skew": round(_skew(rets), 3),
        "win_rate_pct": round(100 * sum(1 for r in rets if r > 0) / n, 1),
        "pct_reached_5x": round(100 * sum(1 for r in records if r["max_multiple"] >= 5) / n, 2),
        "mean_net_pnl_inr_per_lot": round(statistics.mean(net), 1),
        "t_stat_net_pnl": _t_stat(net),
    }


def analyse(records: list) -> dict:
    records = _apply_costs(records)
    by_band = {}
    for lo, hi in BANDS:
        band = f"{lo}-{hi}"
        deepest = [r for r in records if r["band"] == band and r["selection"] == "deepest" and r["is_expiry"]]
        rand = [r for r in records if r["band"] == band and r["selection"] == "random_in_band" and r["is_expiry"]]
        by_band[band] = {
            "deepest": profile(deepest, f"{band} deepest"),
            "random_in_band": profile(rand, f"{band} random"),
            "deepest_vs_random": _z_diff(deepest, rand, "ret_eod_pct"),
        }
    n_tests = len(BANDS)
    bonferroni_t = round(abs(_inv_norm(0.025 / n_tests)), 2)
    return {"n_total": len(records), "bands": by_band, "bonferroni_t_bar": bonferroni_t}


def describe(summary: dict) -> str:
    lines = [
        f"Hero-Zero premium-band sweep (EXPIRY DAY ONLY): {summary['n_total']:,} candidate legs",
        f"{len(BANDS)} bands tested -> Bonferroni bar |t| > {summary['bonferroni_t_bar']}",
        "",
        f"{'band(Rs)':<10}{'sel':<16}{'n':>6}{'meanRet%':>10}{'skew':>7}{'win%':>7}"
        f"{'>=5x':>7}{'netP&L/lot':>12}{'t':>7}   vsRandom(z)",
    ]
    for band, rows in summary["bands"].items():
        for sel_key in ("deepest", "random_in_band"):
            p = rows[sel_key]
            if not p.get("n"):
                lines.append(f"{band:<10}{sel_key:<16}{'0':>6}  (no legs)")
                continue
            zinfo = ""
            if sel_key == "deepest":
                d = rows["deepest_vs_random"]
                zinfo = f"   edge={d.get('edge', 0):+.2f}pp z={d.get('z', 0):+.2f}" if d else ""
            lines.append(
                f"{band:<10}{sel_key:<16}{p['n']:>6,}{p['mean_ret_eod_pct']:>+10.2f}{p['skew']:>+7.2f}"
                f"{p['win_rate_pct']:>7.1f}{p['pct_reached_5x']:>7.2f}"
                f"{p['mean_net_pnl_inr_per_lot']:>+12.1f}{(p['t_stat_net_pnl'] or 0):>+7.2f}{zinfo}"
            )
    lines += [
        "",
        "vsRandom = deepest-in-band vs a random strike in the SAME band/distance cap.",
        "Positive edge/z means picking the deepest-OTM strike in that band beats a",
        "  same-priced random pick; negative repeats the original study's finding.",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/hero_zero_premium_sweep.json")
    args = p.parse_args()

    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}", flush=True)

    records = []
    for i, day in enumerate(days):
        try:
            records.extend(build_candidates_for_bands(day, BANDS))
        except Exception as e:
            print(f"  {day} failed: {e}")
        if (i + 1) % 200 == 0:
            print(f"  ...{i+1}/{len(days)} days, {len(records):,} candidate legs", flush=True)

    summary = analyse(records)
    print()
    print(describe(summary))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
