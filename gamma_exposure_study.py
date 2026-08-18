"""
Does the gamma-exposure regime predict forward returns for THIS
project's own momentum-aligned candidates?

WHY THIS EXISTS
---------------
Comparing this project against neogreeks.in's dashboard (2026-08-18)
surfaced one genuinely new idea worth testing, as opposed to a
repackaging of signals already computed here: Gamma Exposure (GEX).
The specific, falsifiable hypothesis is that this project's core edge
(momentum ROC alignment, see config.SCORING_MODE's "momentum_only"
docstring) should work BEST in a SHORT_GAMMA regime (dealer hedging
amplifies moves) and WORST in LONG_GAMMA (hedging damps/pins price) --
if that's true at all, momentum-aligned candidates should show a
measurable forward-return lift in SHORT_GAMMA versus LONG_GAMMA.
RESEARCH ONLY: this reports a finding, it does not change any live
scoring, filtering, or entry decision. See gamma_exposure.py's own
docstring for the GEX methodology and its honestly-stated limits
(the sign convention is an assumption, not a verified fact).

WHY IT MEASURES CANDIDATES, NOT TRADES, AND FOLLOWS component_study.py's
SHAPE
-------------------------------------------------------------------------
Same reasoning as component_study.py (see that module's own docstring):
analysing only TAKEN trades is selection-biased (shadow.run_policy opens
at most one position per cycle and skips already-traded strikes), so
this evaluates every candidate scanner.scan() actually flags each
cycle, the same counterfactual approach replay.forward_returns and
component_study.py both take. This is deliberately built as a close
sibling of component_study.py rather than a generalisation of it: the
row-building loop is similar because the underlying question shape is
similar, but this needs extra per-row data (gamma regime, momentum-
alignment flag) that module doesn't expose, so it isn't simply reused.

Run: python gamma_exposure_study.py [--horizon 30] [--out logs/gamma_exposure_study.json]
"""

import argparse
import json
import math
import statistics

import config
import gamma_exposure
import oi_analytics
import price_action
import snapshot_recorder
from scanner import scan

LOT_SIZE = config.NIFTY_LOT_SIZE   # NIFTY only for this first pass, matching component_study.py's own scope


def _is_momentum_aligned(reasons: list) -> bool:
    """Exactly the condition scanner.py itself checks under
    SCORING_MODE="momentum_only" to award MOMENTUM_ONLY_ALIGNED_SCORE --
    reusing the same test (not a re-derivation of it) so "momentum
    aligned" means precisely what it means live."""
    return any(r.startswith("Momentum aligned") for r in reasons)


def candidate_rows_with_gamma(day: str, horizon_minutes: int = 30) -> list:
    """
    One row per (cycle, candidate): its score, whether it was momentum-
    aligned, the chain-wide gamma regime AT THAT CYCLE, and what the
    contract actually did over the next `horizon_minutes`.

    Gamma regime is computed ONCE per cycle via
    gamma_exposure.net_gex_and_regime() (the cheap fast-path -- see that
    function's docstring for why the full compute()'s zero-gamma grid
    search would be wasted work here) and reused for every candidate
    that cycle produced, the same way oi_analysis is computed once per
    snapshot below and shared across that cycle's setups.
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

        try:
            net_gex, regime = gamma_exposure.net_gex_and_regime(snapshot, LOT_SIZE, ts)
        except Exception:
            net_gex, regime = None, None

        for setup in scan(snapshot, price_levels=levels, context=context):
            key = (setup.strike, setup.option_type)
            now_px, later_px = prices.get(key), future[1].get(key)
            if not now_px or not later_px:
                continue
            rows.append({
                "score": setup.score,
                "ret": (later_px - now_px) / now_px * 100,
                "momentum_aligned": _is_momentum_aligned(setup.reasons),
                "gamma_regime": regime,
                "net_gex": net_gex,
            })
    return rows


def analyse(rows: list) -> dict:
    """
    Forward-return comparison between SHORT_GAMMA and LONG_GAMMA cycles,
    restricted to momentum-aligned candidates (the stated hypothesis --
    see module docstring). Requires at least 30 candidates on EACH side
    of the split before reporting a comparison at all -- same threshold
    this project applied to trade_tracker.apply_learned_adjustment's
    MIN_TRADES_FOR_ANY_ADJUSTMENT (2026-08-18) for the identical reason:
    a thin sample either side is not evidence, it is noise with a
    confident-looking label on it.
    """
    aligned = [r for r in rows if r["momentum_aligned"] and r["gamma_regime"] in ("SHORT_GAMMA", "LONG_GAMMA")]
    if not aligned:
        return {"n_momentum_aligned": 0, "note": "no momentum-aligned candidates with a computed gamma regime"}

    short_g = [r["ret"] for r in aligned if r["gamma_regime"] == "SHORT_GAMMA"]
    long_g = [r["ret"] for r in aligned if r["gamma_regime"] == "LONG_GAMMA"]

    result = {
        "n_momentum_aligned": len(aligned),
        "n_short_gamma": len(short_g),
        "n_long_gamma": len(long_g),
    }
    if len(short_g) < 30 or len(long_g) < 30:
        result["note"] = (
            f"too few momentum-aligned candidates on one side to compare "
            f"(short={len(short_g)}, long={len(long_g)}, need >=30 each) -- no comparison reported"
        )
        return result

    mean_short, mean_long = statistics.mean(short_g), statistics.mean(long_g)
    se_short = statistics.pstdev(short_g) / math.sqrt(len(short_g))
    se_long = statistics.pstdev(long_g) / math.sqrt(len(long_g))
    se_diff = math.sqrt(se_short ** 2 + se_long ** 2)

    result.update({
        "mean_ret_short_gamma_pct": round(mean_short, 4),
        "mean_ret_long_gamma_pct": round(mean_long, 4),
        "lift_short_minus_long_pct": round(mean_short - mean_long, 4),
        "z": round((mean_short - mean_long) / se_diff, 2) if se_diff > 0 else None,
    })
    return result


def describe(summary: dict, horizon_minutes: int) -> str:
    if summary.get("n_momentum_aligned", 0) == 0:
        return summary.get("note", "No momentum-aligned candidates found.")
    if "lift_short_minus_long_pct" not in summary:
        return (
            f"{summary['n_momentum_aligned']:,} momentum-aligned candidates "
            f"(short_gamma={summary['n_short_gamma']}, long_gamma={summary['n_long_gamma']}).\n"
            f"{summary.get('note', '')}"
        )

    z = summary["z"] or 0
    sig = "significant (|z|>1.96)" if abs(z) > 1.96 else "not significant"
    hypothesis_direction = (
        "supports the hypothesis (short-gamma candidates did better)" if summary["lift_short_minus_long_pct"] > 0
        else "OPPOSES the hypothesis (long-gamma candidates did better)"
    )
    return (
        f"Gamma-regime study: {summary['n_momentum_aligned']:,} momentum-aligned candidates, "
        f"{horizon_minutes}min horizon\n"
        f"  SHORT_GAMMA: n={summary['n_short_gamma']:,}  mean fwd return={summary['mean_ret_short_gamma_pct']:+.4f}%\n"
        f"  LONG_GAMMA:  n={summary['n_long_gamma']:,}  mean fwd return={summary['mean_ret_long_gamma_pct']:+.4f}%\n"
        f"  lift (short - long): {summary['lift_short_minus_long_pct']:+.4f}%  z={z:+.2f}  {sig}\n"
        f"  This {hypothesis_direction}."
    )


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--horizon", type=int, default=30, help="minutes ahead to measure")
    p.add_argument("--out", default="logs/gamma_exposure_study.json")
    p.add_argument("--limit-days", type=int, default=None, help="cap on number of historical days, for a quick run")
    args = p.parse_args()

    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    if args.limit_days:
        days = days[-args.limit_days:]
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}" if days else "0 historical days", flush=True)

    rows = []
    for i, day in enumerate(days):
        try:
            rows.extend(candidate_rows_with_gamma(day, horizon_minutes=args.horizon))
        except Exception as e:
            print(f"  {day} failed: {e}")
        if (i + 1) % 100 == 0:
            print(f"  ...{i+1}/{len(days)} days, {len(rows):,} candidate rows", flush=True)

    summary = analyse(rows)
    print()
    print(describe(summary, args.horizon))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
