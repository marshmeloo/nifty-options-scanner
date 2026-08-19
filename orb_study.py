"""
Backtest of Opening Range Breakout variations on the NIFTY index.

RESEARCH ONLY -- nothing here is wired into a live decision.

WHAT IS BEING ASKED
-------------------
Does ANY ORB variation have a real directional edge on NIFTY? Measured
in R-multiples (R = distance from entry to stop, so days are comparable
across a sample where NIFTY ranges from ~11,000 to ~26,000), gross of
costs, on the index itself -- see orb.py's docstring on why index-level
comes before the options version.

THE THREE BARS A VARIANT HAS TO CLEAR
--------------------------------------
1. MEAN R > 0 with a t-stat that survives Bonferroni correction for the
   number of variants tested at once. With ~20 variants, a nominal
   |t| > 1.96 means nothing -- one in twenty clears that by luck.
2. BEAT THE RANDOM BENCHMARK at the same opening-range length. This is
   the bar most ORB writeups skip entirely, and it is the one that
   matters most. A stop at the opposite side of the range with an
   end-of-day exit has its own built-in R distribution (frequent small
   -1R losses, occasional large open-ended wins) that has NOTHING to do
   with the opening range predicting anything. A coin flip entered at
   the same bar with the same stop inherits that same geometry, so
   whatever it scores is the "free" part. Only the DIFFERENCE is signal.
3. HOLD UP OUT OF SAMPLE. The sample is split by date; a variant that
   only works in the first period is a fitted artefact.

Run:
    python orb_study.py                    # core variant set
    python orb_study.py --out logs/x.json
"""

import argparse
import json
import math
import statistics
from collections import defaultdict

import orb
import orb_candle_cache
from component_study import _inv_norm   # Acklam inverse-normal, already used for the same purpose

# Splits the sample for the out-of-sample check. Deliberately a fixed
# calendar date rather than "last N%": a percentage moves every time the
# cache grows, which would silently change what "out of sample" means
# between runs.
OOS_SPLIT_DATE = "2024-08-01"


# Minimum stop distance as a % of price, applied to EVERY variant. See
# ORBVariant.min_risk_pct for the measurement that forced this in. 0.1%
# is ~24 NIFTY points at 24,000 -- about the least that survives spread
# and slippage on an intraday index trade.
MIN_RISK_PCT = 0.1


def core_variants() -> list:
    """
    The published ORB forms, crossed with the four opening-range lengths
    the literature actually argues about, plus THREE benchmarks per
    length. Stop is the opposite side of the range and the exit is
    end-of-day throughout -- that is the common core of every cited
    version, and holding it fixed means the comparison isolates the
    ENTRY RULE rather than confounding it with exit tuning.

    The benchmarks are the point of the whole exercise:
      RANDOM      -- coin-flip direction, same bar, same stop geometry.
                     Isolates how much of a variant's mean R is just the
                     payoff SHAPE (capped -1R loss, uncapped EOD win)
                     rather than the opening range predicting anything.
      ALWAYS_LONG -- NIFTY roughly doubled over this sample. Any
                     long-leaning rule inherits that drift for free, so
                     without this benchmark a variant can look skilful
                     when it is just long a rising market.
      ALWAYS_SHORT-- the same control in the other direction; it should
                     be clearly negative, which also sanity-checks that
                     the simulator isn't producing free money from the
                     stop/exit geometry alone.
    """
    out = []
    for or_min in (5, 15, 30, 60):
        for entry in ("or_direction", "breakout", "breakout_or_direction", "close_confirm"):
            out.append(orb.ORBVariant(name=f"{entry}_{or_min}min", or_minutes=or_min,
                                      entry=entry, min_risk_pct=MIN_RISK_PCT))
        for bench in ("random", "always_long", "always_short"):
            out.append(orb.ORBVariant(name=f"{bench.upper()}_{or_min}min", or_minutes=or_min,
                                      entry=bench, seed=42, min_risk_pct=MIN_RISK_PCT))
    return out


def run_variant(variant, days: dict) -> list:
    trades = []
    for day in sorted(days):
        bars = days[day]
        if not bars:
            continue
        t = orb.simulate_day(bars, variant, day=day)
        if t:
            trades.append(t)
    return trades


def summarize(trades: list, n_days: int) -> dict:
    if not trades:
        return {"n_trades": 0, "n_days": n_days}
    rs = [t["r_multiple"] for t in trades]
    mean_r = statistics.mean(rs)
    sd = statistics.pstdev(rs)
    se = sd / math.sqrt(len(rs)) if rs else 0
    wins = sum(1 for r in rs if r > 0)
    return {
        "n_trades": len(trades),
        "n_days": n_days,
        "participation_pct": round(len(trades) / n_days * 100, 1) if n_days else 0,
        "mean_r": round(mean_r, 4),
        "sd_r": round(sd, 4),
        "se_r": round(se, 4),
        "t_stat": round(mean_r / se, 2) if se > 0 else None,
        "total_r": round(sum(rs), 1),
        "win_rate_pct": round(wins / len(rs) * 100, 1),
        "stop_rate_pct": round(sum(1 for t in trades if t["exit_reason"] == "stop") / len(trades) * 100, 1),
        "ambiguous_bars": sum(t["ambiguous_bars"] for t in trades),
    }


def difference(a: list, b: list) -> dict:
    """Mean-R difference between two trade sets with its own z, used for
    variant-vs-random. Independent-samples z: the two sets trade
    overlapping days but are different positions, and treating them as
    independent is the conservative choice (a paired test on the same
    days would generally give a SMALLER standard error, i.e. a larger z,
    so this cannot manufacture significance that isn't there)."""
    if not a or not b:
        return {}
    ra, rb = [t["r_multiple"] for t in a], [t["r_multiple"] for t in b]
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    sea = statistics.pstdev(ra) / math.sqrt(len(ra))
    seb = statistics.pstdev(rb) / math.sqrt(len(rb))
    sed = math.sqrt(sea ** 2 + seb ** 2)
    return {
        "edge_vs_random": round(ma - mb, 4),
        "z_vs_random": round((ma - mb) / sed, 2) if sed > 0 else None,
    }


def analyse(days: dict, variants: list) -> dict:
    n_days = sum(1 for d in days if days[d])
    in_sample = {d: b for d, b in days.items() if d < OOS_SPLIT_DATE}
    out_sample = {d: b for d, b in days.items() if d >= OOS_SPLIT_DATE}

    all_trades = {v.name: run_variant(v, days) for v in variants}
    results = {}
    for v in variants:
        trades = all_trades[v.name]
        row = summarize(trades, n_days)
        row["or_minutes"] = v.or_minutes
        row["entry"] = v.entry

        if v.entry not in ("random", "always_long", "always_short"):
            rand_name = f"RANDOM_{v.or_minutes}min"
            if rand_name in all_trades:
                row.update(difference(trades, all_trades[rand_name]))

        ins = summarize(run_variant(v, in_sample), sum(1 for d in in_sample if in_sample[d]))
        oos = summarize(run_variant(v, out_sample), sum(1 for d in out_sample if out_sample[d]))
        row["in_sample_mean_r"] = ins.get("mean_r")
        row["in_sample_n"] = ins.get("n_trades")
        row["out_sample_mean_r"] = oos.get("mean_r")
        row["out_sample_n"] = oos.get("n_trades")
        results[v.name] = row

    tested = [v for v in variants if v.entry not in ("random", "always_long", "always_short")]
    n_tests = max(len(tested), 1)
    return {
        "n_days": n_days,
        "date_range": [min(days), max(days)] if days else None,
        "oos_split": OOS_SPLIT_DATE,
        "n_variants_tested": n_tests,
        "bonferroni_t_bar": round(abs(_inv_norm(0.025 / n_tests)), 2),
        "variants": results,
    }


def describe(summary: dict) -> str:
    rows = summary["variants"]
    bar = summary["bonferroni_t_bar"]
    lines = [
        f"ORB study: {summary['n_days']:,} trading days, {summary['date_range'][0]} .. {summary['date_range'][1]}",
        f"{summary['n_variants_tested']} variants tested -> Bonferroni bar |t| > {bar}",
        f"out-of-sample split at {summary['oos_split']}",
        "",
        f"{'variant':<28} {'n':>6} {'part%':>6} {'meanR':>8} {'t':>6} {'vsRand':>8} {'zR':>6} {'win%':>6} {'IS':>7} {'OOS':>7}",
    ]
    order = sorted(rows.items(), key=lambda kv: -(kv[1].get("mean_r") or -99))
    for name, r in order:
        if not r.get("n_trades"):
            lines.append(f"{name:<28} {'0':>6}  (no trades)")
            continue
        t = r.get("t_stat")
        ev = r.get("edge_vs_random")
        zv = r.get("z_vs_random")
        mark = ""
        if t is not None and abs(t) > bar:
            mark = " ***" if (zv is not None and abs(zv) > 1.96) else " *"
        lines.append(
            f"{name:<28} {r['n_trades']:>6,} {r['participation_pct']:>6.1f} {r['mean_r']:>+8.4f} "
            f"{(t if t is not None else 0):>+6.2f} "
            f"{(ev if ev is not None else 0):>+8.4f} {(zv if zv is not None else 0):>+6.2f} "
            f"{r['win_rate_pct']:>6.1f} {(r.get('in_sample_mean_r') or 0):>+7.3f} "
            f"{(r.get('out_sample_mean_r') or 0):>+7.3f}{mark}"
        )
    lines += [
        "",
        "meanR = mean R-multiple per trade (gross, index-level, no costs)",
        "vsRand/zR = edge over a coin-flip entry with the SAME stop geometry at the same OR length",
        "IS/OOS = mean R in-sample vs out-of-sample",
        "*** clears Bonferroni AND beats random   * clears Bonferroni only",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/orb_study.json")
    args = p.parse_args()

    days = orb_candle_cache.load()
    if not days:
        print("No candle cache. Run: python orb_candle_cache.py")
        return

    variants = core_variants()
    summary = analyse(days, variants)
    print(describe(summary))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
