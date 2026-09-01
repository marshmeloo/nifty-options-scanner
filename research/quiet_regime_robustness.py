"""
Is the quiet-regime gate's result a mechanism, or one lucky stretch?

research/quiet_regime_study.py found p25 improves Bank Nifty Calmar
6.11 -> 7.13 and cuts max drawdown 9.6% -> 7.4% over 1,244 days. Two
things stop that being enough on its own:

  * The sweep is NON-MONOTONIC. p33 is WORSE than no gate at all
    (drawdown 10.5%, Calmar 4.80). A real effect should degrade
    gracefully, so p20-p25 looking good while p33 flips is exactly the
    shape a fortunate window would take.
  * This project's own bar is per-period, never an aggregate. The
    cluster cap was chosen across 5 independent ~1-year windows; v1.2
    was adopted on being better in 6 of 7 years. Today already produced
    a filter (the extension guard) that looked decisive on ONE session
    and failed on 1,244 days.

So: same two policies, broken out by calendar year, plus max drawdown
computed WITHIN each year rather than across the whole run -- a single
whole-period drawdown can hide a year where the gate made things worse.

    python -m research.quiet_regime_robustness
"""

import argparse
import json
from collections import defaultdict

import shadow
from research.banknifty_directional_exposure_backtest import BN_SNAPSHOT_DIR, banknifty_days
from research.concurrent_direction_exposure_study import CAPITAL
from research.one_trade_per_day_study import institutional_metrics, to_rows
from research.quiet_regime_study import BASELINE_DAYS, build_day_ranges

GATE_PCTILE = 25
MIN_ELAPSED = 20.0


def run(days, day_ranges, pctile):
    ov = shadow.BANKNIFTY_SENTINEL_OVERRIDES
    trades = []
    ordered = [d for d in days if d in day_ranges]
    for i, d in enumerate(ordered):
        prior = [day_ranges[x] for x in ordered[max(0, i - BASELINE_DAYS):i]]
        policy = shadow.Policy(
            name=f"q{pctile}", use_learned_adjustment=False,
            symbol="BANKNIFTY", snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov,
            strike_adjacency_band_points=ov["CLUSTER_CAP_ADJACENCY_POINTS"],
            cluster_window_minutes=ov["CLUSTER_CAP_WINDOW_MINUTES"],
            use_opposite_direction_gate=True, use_reversal_exit=True,
            quiet_regime_block_pctile=pctile, quiet_regime_min_elapsed_pct=MIN_ELAPSED,
            regime_baseline=sorted(prior) if len(prior) >= 30 else None)
        try:
            trades.extend(shadow.run_policy(d, policy))
        except Exception:
            pass
    return [t for t in trades if t.outcome]


def per_year(trades):
    """Metrics computed WITHIN each year, drawdown included."""
    by_year = defaultdict(list)
    for t in trades:
        by_year[t.opened_at[:4]].append(t)
    out = {}
    for y, ts in by_year.items():
        rows = to_rows(ts)
        m = institutional_metrics(rows, capital=CAPITAL)
        out[y] = {"n": m["n"], "return_pct": m["total_return_pct"],
                  "max_dd_pct": m["max_dd_pct"], "calmar": m["calmar"],
                  "win_rate_pct": m["win_rate_pct"], "expectancy_r": m["expectancy_r"]}
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="logs/quiet_regime_robustness.json")
    args = p.parse_args()

    days = banknifty_days()
    print(f"BANKNIFTY {len(days)} days -- building per-day ranges...", flush=True)
    day_ranges = build_day_ranges(days, BN_SNAPSHOT_DIR, "BANKNIFTY")

    print("  running gate OFF...", flush=True)
    off = per_year(run(days, day_ranges, None))
    print(f"  running gate p{GATE_PCTILE}...", flush=True)
    on = per_year(run(days, day_ranges, GATE_PCTILE))

    years = sorted(set(off) | set(on))
    print()
    print(f"  {'year':<6}{'trades off/on':>16}{'return off':>12}{'return on':>12}"
          f"{'maxDD off':>11}{'maxDD on':>10}{'calmar off':>12}{'calmar on':>11}   verdict")
    better = worse = 0
    for y in years:
        a, b = off.get(y), on.get(y)
        if not a or not b:
            continue
        ca, cb = a["calmar"] or 0, b["calmar"] or 0
        v = "BETTER" if cb > ca else ("worse" if cb < ca else "same")
        if cb > ca:
            better += 1
        elif cb < ca:
            worse += 1
        print(f"  {y:<6}{f'{a[chr(110)]}/{b[chr(110)]}':>16}"
              f"{a['return_pct']:>11.1f}%{b['return_pct']:>11.1f}%"
              f"{a['max_dd_pct']:>10.1f}%{b['max_dd_pct']:>9.1f}%"
              f"{ca:>12.2f}{cb:>11.2f}   {v}")
    print()
    print(f"  Calmar better in {better} of {better+worse} years, worse in {worse}")
    print(f"  (this project's bar: the cluster cap needed 5 independent windows;")
    print(f"   v1.2 was adopted on 6 of 7 years better)")

    with open(args.out, "w") as f:
        json.dump({"off": off, f"p{GATE_PCTILE}": on}, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
