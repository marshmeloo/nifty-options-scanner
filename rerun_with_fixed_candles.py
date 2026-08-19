"""
Re-runs the candle-dependent backtests with the session's FIRST BAR
restored, and reports what changed.

WHY
---
Dhan's /charts/intraday `fromDate` is exclusive, so this project's
"09:15:00" default dropped every session's opening bar from every
candle series it ever fetched -- including the ones baked into all
1,485 reconstructed days in logs/snapshots/. Fixed 2026-08-19
(dhan_source.SESSION_FETCH_FROM_TIME); see README's entry for the full
description. Any conclusion drawn from a candle-dependent backtest was
therefore drawn on a session that began one bar late, and needs
re-checking against corrected data rather than assumed still valid.

HOW, AND WHY NOT BY REWRITING THE SNAPSHOTS
--------------------------------------------
The obvious route is historical_source.backfill_candles() to rewrite
the stored snapshots. Deliberately NOT doing that:

  - It mutates ~1,485 recorded-history files in place (~2 GB rewritten).
    .gitignore calls that directory "valuable data -- back it up
    somewhere", and it is not reproducible from anything else in the
    repo if a rewrite goes wrong midway.
  - It would cost ~1,485 sequential Dhan calls (~87 min at the shared
    rate limiter's interval).
  - It destroys the ability to answer "did the fix change the answer?",
    because the before-state would be gone.

Instead this patches snapshot_recorder.load_day() in memory to serve
CORRECTED candles from orb_candle_cache (which already holds all 1,506
days fetched from 09:14, so zero new API calls), replicating exactly
what backfill_candles does per cycle -- `[c for c in day_candles if
c.timestamp <= snap.timestamp]`, so no cycle ever sees a candle from
its own future. Every downstream consumer (component_study, shadow,
scan()) then reads corrected candles without knowing anything changed,
and the stored data is untouched.

Run:
    python rerun_with_fixed_candles.py --study component
    python rerun_with_fixed_candles.py --study gamma
"""

import argparse
import json
from datetime import datetime

import orb_candle_cache
import snapshot_recorder
from models import Candle

_CACHE = None


def _corrected_day_candles(day: str) -> list:
    """The day's real bars, opening bar included, as Candle objects."""
    global _CACHE
    if _CACHE is None:
        _CACHE = orb_candle_cache.load()
    rows = _CACHE.get(day) or []
    out = []
    for r in rows:
        ts = datetime.strptime(f"{day} {r['t']}", "%Y-%m-%d %H:%M")
        out.append(Candle(timestamp=ts, open=r["o"], high=r["h"],
                          low=r["l"], close=r["c"], volume=r.get("v") or 0))
    return out


def patch_loader():
    """
    Replace snapshot_recorder.load_day with a version that swaps in
    corrected candles. Returns the original so a caller can restore it.

    Falls back to the day's ORIGINAL candles when the cache has nothing
    for that date, rather than silently handing back an empty list --
    an empty candle series would look like "no price-action context"
    and quietly change what the study measures, which is exactly the
    class of silent-degradation bug this whole exercise is about.
    """
    original = snapshot_recorder.load_day

    def patched(day, snapshot_dir=None, symbol="NIFTY"):
        corrected = _corrected_day_candles(day if isinstance(day, str) else day.isoformat())
        for snap, candles, meta in original(day, snapshot_dir=snapshot_dir, symbol=symbol):
            if corrected:
                visible = [c for c in corrected if c.timestamp <= snap.timestamp]
                yield snap, visible, meta
            else:
                yield snap, candles, meta

    snapshot_recorder.load_day = patched
    return original


def historical_days() -> list:
    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    return days


def run_component_study(horizon: int, out: str):
    import component_study

    days = historical_days()
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}", flush=True)
    rows = []
    for i, day in enumerate(days):
        try:
            rows.extend(component_study.candidate_rows(day, horizon_minutes=horizon))
        except Exception as e:
            print(f"  {day} failed: {e}")
        if (i + 1) % 300 == 0:
            print(f"  ...{i+1}/{len(days)}, {len(rows):,} candidates", flush=True)

    summary = component_study.analyse(rows)
    print()
    print(component_study.describe(summary, horizon))
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {out}")
    return summary


def run_gamma_study(horizon: int, out: str):
    import gamma_exposure_study as gs

    days = historical_days()
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}", flush=True)
    rows = []
    for i, day in enumerate(days):
        try:
            rows.extend(gs.candidate_rows_with_gamma(day, horizon_minutes=horizon))
        except Exception as e:
            print(f"  {day} failed: {e}")
        if (i + 1) % 300 == 0:
            print(f"  ...{i+1}/{len(days)}", flush=True)

    summary = gs.analyse(rows)
    print()
    print(gs.describe(summary, horizon))
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {out}")
    return summary


def run_variant_comparison(out: str):
    """
    Re-runs compare_variants' grid on corrected candles.

    Only the FIXED arm is run, deliberately. The decision this backtest
    made -- adopting SCORING_MODE="momentum_only" -- rested on how the
    variants rank AGAINST EACH OTHER, not on any absolute number, so the
    question "does the fix change the decision?" is answered by whether
    momentum_only still wins on corrected data. A baseline arm would
    only re-measure a ranking that is already recorded.

    Note the sample is now 1,485 days against the original study's 493,
    so absolute figures are NOT comparable to logs/variant_comparison.json
    -- see this module's --baseline flag comment.

    FORCES config.SCORING_MODE = "legacy" for the duration. Not
    cosmetic: the first attempt at this re-run left it at today's live
    value, "momentum_only", and produced nonsense -- every variant came
    back with n within 0.4% of every other (11003 / 11002 / 11042 /
    11046) and thresholds 5.5, 6.0 and 6.5 gave byte-identical results.
    The cause is that scanner.scan() under "momentum_only" OVERRIDES
    every candidate's score to MOMENTUM_ONLY_ALIGNED/AGAINST/NEUTRAL
    (6.0 / 0.0 / 3.0) before any variant rescorer runs, so the
    rescorers could only add an offset to an already-flattened score and
    every variant ended up selecting the same aligned candidates.

    "momentum_only" is the mode this very comparison ADOPTED, so
    re-running the comparison under it is circular -- it assumes the
    conclusion. The original study necessarily ran under "legacy", which
    is also why tests/conftest.py forces "legacy" for the whole suite.
    """
    import config
    import scorer_variants
    import shadow
    import sweep_threshold
    import compare_variants as cv

    previous_mode = config.SCORING_MODE
    config.SCORING_MODE = "legacy"
    print(f"config.SCORING_MODE forced {previous_mode!r} -> 'legacy' for this comparison "
          f"(see run_variant_comparison's docstring)\n", flush=True)

    days = sweep_threshold.historical_days()
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}\n", flush=True)

    results = []
    for name, fn in scorer_variants.ALL.items():
        for threshold in cv.GRIDS[name]:
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
            row = cv.stats(trades)
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

    with open(out, "w") as f:
        json.dump({"days": len(days), "scoring_mode": "legacy", "results": results}, f, indent=2)
    print(f"\nwritten to {out}")
    config.SCORING_MODE = previous_mode
    return results


def compare_component(baseline_path: str, fixed_path: str) -> str:
    """
    Side-by-side of the two arms. What matters is not whether individual
    numbers moved -- with ~1.5M candidates they will move slightly from
    any change -- but whether any CONCLUSION moved: does the same
    component still lead, does it still survive Bonferroni, and did any
    component change sign.
    """
    base = json.load(open(baseline_path))
    fixed = json.load(open(fixed_path))
    bc, fc = base["components"], fixed["components"]

    lines = [
        f"{'':<52} {'BASELINE (bar-short)':>22} {'FIXED (bar restored)':>22}",
        f"{'candidates':<52} {base['n']:>22,} {fixed['n']:>22,}",
        f"{'base rate %':<52} {base['base_rate']:>22.4f} {fixed['base_rate']:>22.4f}",
        "",
        f"{'component':<52} {'lift':>9} {'z':>7} | {'lift':>9} {'z':>7}  {'sign?':>6}",
    ]
    shared = sorted(set(bc) & set(fc), key=lambda k: -abs(fc[k]["z"] or 0))
    for k in shared[:14]:
        b, f = bc[k], fc[k]
        flipped = "FLIP" if (b["lift"] or 0) * (f["lift"] or 0) < 0 else ""
        lines.append(
            f"{k[:52]:<52} {b['lift']:>+9.4f} {(b['z'] or 0):>+7.2f} | "
            f"{f['lift']:>+9.4f} {(f['z'] or 0):>+7.2f}  {flipped:>6}"
        )
    only_base = set(bc) - set(fc)
    only_fixed = set(fc) - set(bc)
    if only_base or only_fixed:
        lines.append("")
        lines.append(f"components only in baseline: {sorted(only_base)}")
        lines.append(f"components only in fixed:    {sorted(only_fixed)}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # Not required, because --compare is a standalone mode that reads two
    # finished result files and runs no study of its own.
    p.add_argument("--study", choices=["component", "gamma", "variants"])
    p.add_argument("--horizon", type=int, default=30)
    p.add_argument("--out", default=None)
    # The stored logs/*.json baselines are NOT a valid comparison point:
    # they were produced when only 493 historical days existed, against
    # today's 1,485. Comparing a fresh corrected run to them would
    # measure "4x more data" far more than "opening bar restored". This
    # flag runs the SAME code over the SAME day set with the ORIGINAL
    # (bar-short) candles, so the only difference between the two arms is
    # the fix itself.
    p.add_argument("--baseline", action="store_true",
                   help="run with the ORIGINAL bar-short candles, for an A/B against --study")
    p.add_argument("--compare", nargs=2, metavar=("BASELINE_JSON", "FIXED_JSON"),
                   help="print the A/B diff of two finished component runs and exit")
    args = p.parse_args()

    if args.compare:
        print(compare_component(*args.compare))
        return
    if not args.study:
        p.error("--study is required unless --compare is given")

    if args.baseline:
        print("BASELINE arm: original candles, session starts one bar late\n")
    else:
        patch_loader()
        print("FIXED arm: load_day patched -> corrected candles (opening bar restored)\n")

    if args.study == "component":
        run_component_study(args.horizon, args.out or "logs/component_study_fixed_candles.json")
    elif args.study == "variants":
        run_variant_comparison(args.out or "logs/variant_comparison_fixed_candles.json")
    else:
        run_gamma_study(args.horizon, args.out or "logs/gamma_exposure_study_fixed_candles.json")


if __name__ == "__main__":
    main()
