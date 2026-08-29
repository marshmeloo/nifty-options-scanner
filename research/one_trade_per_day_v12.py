"""
Sentinel v1.2 capped to ONE TRADE PER DAY, both indices, full metrics.

Asked while planning a live start on small capital, and the capital angle
is the point: one entry per day means one position at a time, so peak
simultaneous premium collapses to a single contract.

WATCH FOR: shadow.run_policy breaks out of the day once the cap is hit,
exactly as trade_tracker.try_open_new_trade returns early on
MAX_NEW_TRADES_PER_DAY before it ever reaches the opposite-direction
gate. So under a 1-trade/day cap the v1.2 mechanisms have nothing to act
on -- no second signal to gate, no blocked signal to trigger a reversal
exit. This script runs baseline AND gate+exit precisely to show whether
that is true rather than assert it.

    python -m research.one_trade_per_day_v12
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

import shadow
import snapshot_recorder
from research.banknifty_directional_exposure_backtest import BN_SNAPSHOT_DIR, banknifty_days
from research.concurrent_direction_exposure_study import CAPITAL, historical_days
from research.directional_exposure_backtest import annual_table
from research.one_trade_per_day_study import institutional_metrics, to_rows


def build(index: str, gate: bool, exit_: bool, cap):
    if index == "NIFTY":
        return shadow.Policy(
            name=f"nifty_cap{cap}", use_learned_adjustment=False,
            strike_adjacency_band_points=200, cluster_window_minutes=30,
            max_trades_per_day=cap,
            use_opposite_direction_gate=gate, use_reversal_exit=exit_), 65
    ov = shadow.BANKNIFTY_SENTINEL_OVERRIDES
    return shadow.Policy(
        name=f"bn_cap{cap}", use_learned_adjustment=False, symbol="BANKNIFTY",
        snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov,
        strike_adjacency_band_points=ov["CLUSTER_CAP_ADJACENCY_POINTS"],
        cluster_window_minutes=ov["CLUSTER_CAP_WINDOW_MINUTES"],
        max_trades_per_day=cap,
        use_opposite_direction_gate=gate, use_reversal_exit=exit_), 30


def run(index, days, gate, exit_, cap):
    policy, lot = build(index, gate, exit_, cap)
    trades = []
    for day in days:
        try:
            trades.extend(shadow.run_policy(day, policy))
        except Exception:
            pass
    closed = [t for t in trades if t.outcome]
    rows = to_rows(closed)
    m = institutional_metrics(rows, capital=CAPITAL)
    prem = [t.entry * lot for t in closed]
    risk = sorted((t.entry - t.stop) * lot for t in closed)
    m["_peak_premium"] = max(prem) if prem else 0          # 1 position at a time under the cap
    m["_median_premium"] = statistics.median(prem) if prem else 0
    m["_max_risk_per_lot"] = max(risk) if risk else 0
    m["_capital_for_100pct"] = (max(risk) if risk else 0) * 100
    m["_annual"] = annual_table(rows, CAPITAL)
    return m


def show(label, capped, uncapped):
    keys = (("n", "{:.0f}"), ("win_rate_pct", "{:.1f}%"), ("total_return_pct", "{:+.1f}%"),
            ("max_dd_pct", "{:.1f}%"), ("calmar", "{:.2f}"), ("profit_factor", "{:.2f}"),
            ("expectancy_r", "{:+.4f}"), ("avg_trades_per_day", "{:.2f}"))
    print(f"\n-- {label} --")
    print(f"  {'metric':<22}{'1 trade/day':>16}{'uncapped v1.2':>16}")
    for k, fmt in keys:
        a, b = capped.get(k), uncapped.get(k)
        print(f"  {k:<22}{(fmt.format(a) if a is not None else 'n/a'):>16}"
              f"{(fmt.format(b) if b is not None else 'n/a'):>16}")
    print(f"  {'peak premium (Rs)':<22}{capped['_peak_premium']:>16,.0f}{'--':>16}")
    print(f"  {'median premium (Rs)':<22}{capped['_median_premium']:>16,.0f}{'--':>16}")
    print(f"  {'capital for 100% (Rs)':<22}{capped['_capital_for_100pct']:>16,.0f}{'--':>16}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="logs/one_trade_per_day_v12.json")
    args = p.parse_args()

    nd, bd = historical_days(), banknifty_days()
    print(f"NIFTY {len(nd)} days | BANK NIFTY {len(bd)} days\n", flush=True)

    out = {}
    for index, days in (("NIFTY", nd), ("BANKNIFTY", bd)):
        print(f"  {index}: 1/day baseline...", flush=True)
        base1 = run(index, days, False, False, 1)
        print(f"  {index}: 1/day gate+exit...", flush=True)
        full1 = run(index, days, True, True, 1)
        print(f"  {index}: uncapped gate+exit...", flush=True)
        unc = run(index, days, True, True, None)
        out[index] = {"cap1_baseline": base1, "cap1_gate_exit": full1, "uncapped_gate_exit": unc}
        show(f"{index}  (1 trade/day, gate+exit)", full1, unc)
        same = (round(base1["total_return_pct"], 4) == round(full1["total_return_pct"], 4)
                and base1["n"] == full1["n"])
        print(f"  v1.2 mechanisms inert under the cap? {'YES -- identical to baseline' if same else 'NO -- they still change the result'}")
        print(f"     baseline 1/day: {base1['total_return_pct']:+.1f}% n={base1['n']:.0f}   "
              f"gate+exit 1/day: {full1['total_return_pct']:+.1f}% n={full1['n']:.0f}")

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
