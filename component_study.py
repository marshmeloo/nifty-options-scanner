"""
Which individual scoring components actually predict a forward return?

WHY THIS EXISTS
---------------
The threshold sweep showed gross expectancy rising with the conviction
score (-0.044R at a 3.0 bar to +0.726R at 6.0), but that says nothing
about WHICH of the ~15 reasons feeding that score carry the signal. A
composite that works on average can easily be one or two predictive
components dragging along a dozen that are noise -- or worse, several
that are actively wrong and cancelling out. Tuning the threshold without
knowing which is which is tuning the wrong dial.

WHY IT MEASURES CANDIDATES, NOT TRADES
--------------------------------------
Analysing taken trades cannot answer this. shadow.run_policy opens at
most one position per cycle and blocks strikes already traded that day
(`traded_keys`), so the trades on record are a selection-biased sample of
what the scanner actually saw -- an earlier disjoint-bucket analysis on
that sample produced a NEGATIVE score/return correlation purely as an
artifact of it. This module instead evaluates EVERY candidate the scanner
flagged, the same counterfactual approach replay.forward_returns takes,
which is exactly what recording full chains was for.

WHAT "PREDICTIVE" MEANS HERE
----------------------------
Forward return of the contract itself over a fixed horizon, versus the
base rate across all candidates in the same sample. A component is
interesting when contracts carrying it beat that base by more than
sampling noise explains. This deliberately ignores stop/target path: it
measures whether the SIGNAL is informative, separately from whether the
trade construction around it is any good. Those are different failures
and conflating them is how you end up retuning stops to fix a signal
problem.

Reported lifts are raw. With ~15 components examined at once, some will
clear a nominal 5% bar by chance, so a Bonferroni-corrected z is printed
alongside and single standouts should be read against it.
"""

import argparse
import json
import math
import re
import statistics
from collections import defaultdict

import oi_analytics
import price_action
import snapshot_recorder
from scanner import scan

# Strips the variable parts of a reason string so the same component
# aggregates across trades: numeric ranges, percentiles, prices. The
# non-numeric parentheticals ("(bullish for this contract)") are the
# component's actual meaning and are deliberately KEPT -- a bullish and a
# bearish buildup are different signals, not one signal with noise.
_NUMERIC_PAREN = re.compile(r"\s*\([^)]*\d[^)]*\)")


def normalise_reason(reason: str) -> str:
    text = _NUMERIC_PAREN.sub("", reason)
    head, sep, tail = text.partition(" -- ")
    head = head.split(":")[0].strip()
    return f"{head} -- {tail.strip()}" if sep else head


def candidate_rows(day: str, horizon_minutes: int = 30) -> list:
    """
    One row per (cycle, candidate): its score, its normalised components,
    and what the contract actually did over the next `horizon_minutes`.
    """
    cycles = list(snapshot_recorder.load_day(day))
    if not cycles:
        return []

    indexed = [
        (snap.timestamp, {(q.strike, q.option_type): q.ltp for q in snap.chain}, snap, candles)
        for snap, candles, _m in cycles
    ]

    rows = []
    for i, (ts, prices, snapshot, candles) in enumerate(indexed):
        future = None
        for j in range(i + 1, len(indexed)):
            if (indexed[j][0] - ts).total_seconds() / 60 >= horizon_minutes:
                future = indexed[j]
                break
        if future is None:
            break  # no cycle far enough ahead; the rest of the day can't be scored

        levels, context = [], None
        if candles:
            try:
                levels, context = price_action.analyze_with_context(candles)
            except Exception:
                pass
        try:
            snapshot.oi_analysis = oi_analytics.analyze(snapshot.chain, snapshot.spot)
        except Exception:
            snapshot.oi_analysis = None

        for setup in scan(snapshot, price_levels=levels, context=context):
            key = (setup.strike, setup.option_type)
            now_px, later_px = prices.get(key), future[1].get(key)
            if not now_px or not later_px:
                continue
            rows.append({
                "score": setup.score,
                "ret": (later_px - now_px) / now_px * 100,
                "tags": sorted({normalise_reason(r) for r in setup.reasons}),
            })
    return rows


def analyse(rows: list) -> dict:
    """Per-component lift over the base rate of every candidate in the sample."""
    if not rows:
        return {}

    all_rets = [r["ret"] for r in rows]
    base = statistics.mean(all_rets)

    with_tag = defaultdict(list)
    without_tag = defaultdict(list)
    tags = {t for r in rows for t in r["tags"]}
    for r in rows:
        present = set(r["tags"])
        for tag in tags:
            (with_tag if tag in present else without_tag)[tag].append(r["ret"])

    results = {}
    for tag in tags:
        w, wo = with_tag[tag], without_tag[tag]
        if len(w) < 30 or len(wo) < 30:
            continue  # too thin on either side to compare
        mw, mwo = statistics.mean(w), statistics.mean(wo)
        sew = statistics.pstdev(w) / math.sqrt(len(w))
        sewo = statistics.pstdev(wo) / math.sqrt(len(wo))
        sed = math.sqrt(sew ** 2 + sewo ** 2)
        results[tag] = {
            "n_with": len(w),
            "mean_with": round(mw, 4),
            "mean_without": round(mwo, 4),
            "lift": round(mw - mwo, 4),
            "z": round((mw - mwo) / sed, 2) if sed > 0 else None,
        }
    return {"base_rate": round(base, 4), "n": len(rows), "components": results}


def describe(summary: dict, horizon_minutes: int) -> str:
    if not summary:
        return "No candidate rows."

    comps = summary["components"]
    n_tests = len(comps)
    # Bonferroni: the bar a single component must clear when this many were
    # examined at once, so a lucky standout isn't mistaken for a finding.
    alpha_z = 1.96 if n_tests <= 1 else abs(_inv_norm(0.025 / n_tests))

    lines = [
        f"Forward-return study: {summary['n']:,} candidates, {horizon_minutes}min horizon",
        f"base rate across all candidates: {summary['base_rate']:+.4f}%",
        f"{n_tests} components tested -> Bonferroni bar |z| > {alpha_z:.2f}",
        "",
        f"{'component':<58} {'n':>7} {'lift':>9} {'z':>7}  sig",
    ]
    for tag, r in sorted(comps.items(), key=lambda kv: -abs(kv[1]["z"] or 0)):
        z = r["z"] or 0
        mark = "***" if abs(z) > alpha_z else ("*" if abs(z) > 1.96 else "")
        lines.append(f"{tag[:58]:<58} {r['n_with']:>7,} {r['lift']:>+8.4f}% {z:>+7.2f}  {mark}")
    lines.append("")
    lines.append("*** survives Bonferroni   * nominally significant only")
    return "\n".join(lines)


def _inv_norm(p: float) -> float:
    """Acklam's inverse normal CDF -- avoids a scipy dependency for one number."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--horizon", type=int, default=30, help="minutes ahead to measure")
    p.add_argument("--out", default="logs/component_study.json")
    args = p.parse_args()

    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}", flush=True)

    rows = []
    for i, day in enumerate(days):
        try:
            rows.extend(candidate_rows(day, horizon_minutes=args.horizon))
        except Exception as e:
            print(f"  {day} failed: {e}")
        if (i + 1) % 100 == 0:
            print(f"  ...{i+1}/{len(days)} days, {len(rows):,} candidates", flush=True)

    summary = analyse(rows)
    print()
    print(describe(summary, args.horizon))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
