"""
What do bid-ask spreads actually cost, measured from recorded live order
books?

WHY THIS MATTERS MORE THAN IT SOUNDS
------------------------------------
Every backtest number in this project prices fills at LTP, because
historical reconstruction carries no order book. The momentum
cost-sensitivity analysis assumed 0.2-0.6% spreads and showed that at
0.6% the strategy's edge shrinks materially -- from +0.104R/trade net to
roughly +0.062R. The assumption was never measured.

This turns orderflow_recorder.py's session recordings into that
measurement, stratified along the two dimensions that actually move it:

  - MARKET PHASE. The single capture that prompted this work was taken
    post-close and read 0.6-1.6%, which is not a usable estimate --
    spreads widen when nobody is quoting. Reporting one blended average
    across the whole recording would repeat that error at larger scale.
  - PREMIUM LEVEL. The strategies trade specific premium bands
    (momentum's PREMIUM_MIN/MAX, the spread's SHORT_PREMIUM_MIN/MAX), and
    a Rs 20 option and a Rs 200 option do not have comparable percentage
    spreads. A chain-wide average would describe neither.

WHAT IT STILL CANNOT TELL YOU
-----------------------------
Where inside the spread a fill actually lands. This measures the quoted
spread, i.e. the cost of crossing it in full. A resting limit order may
do better; a large order sweeping several levels will do worse. Treat the
figure as the cost of an immediate marketable fill of one lot, which is
what the backtests assume the strategies do.
"""

import argparse
import json
import statistics
from collections import defaultdict

import config
import orderflow_recorder


def _pct(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]


def _summarise(values) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "mean": round(statistics.mean(values), 3),
        "p25": round(_pct(values, 25), 3),
        "p75": round(_pct(values, 75), 3),
        "p90": round(_pct(values, 90), 3),
    }


def collect(days: list = None) -> dict:
    """
    Gather every recorded spread sample, bucketed by phase and by premium
    band. Returns raw buckets so a caller can slice them differently.
    """
    days = days or orderflow_recorder.available_days()

    by_phase = defaultdict(list)
    by_premium = defaultdict(list)
    by_distance = defaultdict(list)
    regular_only = []
    rows = 0

    for day in days:
        for row in orderflow_recorder.load_day(day):
            rows += 1
            phase = row.get("phase", "unknown")
            for s in row.get("samples", []):
                spread = s.get("spread_pct")
                if spread is None:
                    continue
                by_phase[phase].append(spread)
                if phase != "regular":
                    # Everything below is about what trading actually
                    # costs, so it is restricted to the regular session.
                    # Opening/closing/post-close spreads are real but
                    # describe a market the strategies mostly do not
                    # trade in.
                    continue
                regular_only.append(spread)

                mid = s.get("mid") or 0
                if mid < 20:
                    band = "under 20"
                elif mid < 50:
                    band = "20-50"
                elif mid < 100:
                    band = "50-100"
                elif mid < 200:
                    band = "100-200"
                else:
                    band = "200+"
                by_premium[band].append(spread)

                dist = s.get("distance_from_spot")
                if dist is not None:
                    bucket = f"{int(dist // 100) * 100}-{int(dist // 100) * 100 + 100}"
                    by_distance[bucket].append(spread)

    return {
        "days": days,
        "rows": rows,
        "by_phase": dict(by_phase),
        "by_premium": dict(by_premium),
        "by_distance": dict(by_distance),
        "regular": regular_only,
    }


def strategy_band_spread(collected: dict) -> dict:
    """
    Median spread inside each live strategy's own premium band -- the
    figure that actually applies to its backtested results, rather than a
    chain-wide average that applies to nothing.
    """
    import config_directional_spread as dcfg

    bands = {
        "momentum (config.PREMIUM_MIN/MAX)": (
            getattr(config, "PREMIUM_MIN", 0), getattr(config, "PREMIUM_MAX", 10**9)),
        "directional spread (SHORT_PREMIUM_MIN/MAX)": (
            dcfg.SHORT_PREMIUM_MIN, dcfg.SHORT_PREMIUM_MAX),
    }

    out = {}
    for label, (lo, hi) in bands.items():
        values = []
        for day in collected["days"]:
            for row in orderflow_recorder.load_day(day):
                if row.get("phase") != "regular":
                    continue
                for s in row.get("samples", []):
                    mid, spread = s.get("mid"), s.get("spread_pct")
                    if mid is None or spread is None:
                        continue
                    if lo <= mid <= hi:
                        values.append(spread)
        out[label] = {**_summarise(values), "band": f"Rs {lo:.0f}-{hi:.0f}"}
    return out


def describe(collected: dict) -> str:
    lines = []
    days = collected["days"]
    if not days:
        return ("No order-flow recordings yet. Run orderflow_feed.py through a "
                "session first -- recording is on by default.")

    lines.append(f"Spread study: {len(days)} day(s), {collected['rows']:,} sampled cycles")
    lines.append(f"days: {', '.join(days)}")
    lines.append("")

    lines.append("BY MARKET PHASE (spread % of mid)")
    lines.append(f"  {'phase':<12} {'n':>8} {'median':>8} {'mean':>8} {'p75':>8} {'p90':>8}")
    phase_order = ["pre_open", "opening", "regular", "closing", "post_close"]
    for phase in phase_order:
        values = collected["by_phase"].get(phase)
        if not values:
            continue
        s = _summarise(values)
        lines.append(f"  {phase:<12} {s['n']:>8,} {s['median']:>8.3f} {s['mean']:>8.3f} "
                     f"{s['p75']:>8.3f} {s['p90']:>8.3f}")

    if collected["regular"]:
        lines.append("")
        lines.append("REGULAR SESSION ONLY, by premium level (what the strategies trade)")
        lines.append(f"  {'premium':<12} {'n':>8} {'median':>8} {'mean':>8} {'p75':>8} {'p90':>8}")
        for band in ["under 20", "20-50", "50-100", "100-200", "200+"]:
            values = collected["by_premium"].get(band)
            if not values:
                continue
            s = _summarise(values)
            lines.append(f"  {band:<12} {s['n']:>8,} {s['median']:>8.3f} {s['mean']:>8.3f} "
                         f"{s['p75']:>8.3f} {s['p90']:>8.3f}")

    return "\n".join(lines)


def describe_impact(collected: dict) -> str:
    """
    Restate the measurement as what it does to the backtested numbers,
    since that is the decision it informs.
    """
    band_stats = strategy_band_spread(collected)
    lines = ["", "PER-STRATEGY, inside each one's own premium band"]
    for label, s in band_stats.items():
        if not s.get("n"):
            lines.append(f"  {label}: no samples in {s['band']}")
            continue
        lines.append(f"  {label}")
        lines.append(f"    band {s['band']}  n={s['n']:,}  median {s['median']:.3f}%  "
                     f"p75 {s['p75']:.3f}%  p90 {s['p90']:.3f}%")

    momentum = band_stats.get("momentum (config.PREMIUM_MIN/MAX)", {})
    if momentum.get("median"):
        lines.append("")
        lines.append("  Against the momentum cost-sensitivity analysis, which assumed")
        lines.append("  0.2-0.6% and showed net expectancy falling from +0.104R to")
        lines.append(f"  ~+0.062R across that range: measured median is {momentum['median']:.3f}%.")
        if momentum["median"] > 0.6:
            lines.append("  ABOVE the assumed range -- backtested totals are more optimistic")
            lines.append("  than currently documented, and every strategy figure needs revisiting.")
        elif momentum["median"] < 0.2:
            lines.append("  BELOW the assumed range -- backtested totals are conservative.")
        else:
            lines.append("  INSIDE the assumed range; the documented sensitivity still applies.")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--day", help="ISO date; omit for every recorded day")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="emit raw bucket summaries as JSON")
    args = p.parse_args()

    days = [args.day] if args.day else None
    collected = collect(days)

    if args.as_json:
        print(json.dumps({
            "days": collected["days"],
            "by_phase": {k: _summarise(v) for k, v in collected["by_phase"].items()},
            "by_premium": {k: _summarise(v) for k, v in collected["by_premium"].items()},
            "by_distance": {k: _summarise(v) for k, v in collected["by_distance"].items()},
            "strategy_bands": strategy_band_spread(collected),
        }, indent=2))
        return

    print(describe(collected))
    if collected["regular"]:
        print(describe_impact(collected))
    else:
        print("\nNo regular-session samples yet -- run the feed during market hours "
              "(09:15-15:30) for a usable number. Post-close spreads are real but "
              "describe a market the strategies do not trade in.")


if __name__ == "__main__":
    main()
