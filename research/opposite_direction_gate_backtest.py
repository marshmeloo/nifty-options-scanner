"""
The REAL backtest of the opposite-direction exposure gate (config.
OPPOSITE_DIRECTION_GATE_ENABLED / trade_tracker.opposite_direction_blocks(),
shipped 2026-08-27), as opposed to research/concurrent_direction_exposure_study.py's
approximation (which just DROPS every trade that was ever caught in an
overlap -- not quite the same thing a real gate does, since a real gate
only blocks the LATER entrant; the first trade that later got "joined"
by an opposite-direction one would still have opened).

This runs shadow.py with Policy.use_opposite_direction_gate=True vs
False -- the actual gate logic wired into run_policy(), the same code
path a live process uses -- for both Anchor's and Sentinel's real
config, so the numbers here are what actually ships, not an
approximation of it.

Run: python -m research.opposite_direction_gate_backtest
"""

import argparse
import json

import shadow
from research.concurrent_direction_exposure_study import CAPITAL, historical_days, run_policy_over_history
from research.one_trade_per_day_study import institutional_metrics, to_rows


def annual_table(rows: list, capital: float) -> list:
    from collections import defaultdict
    by_year = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for day, net, _r, _risk in rows:
        by_year[day[:4]]["n"] += 1
        by_year[day[:4]]["pnl"] += net
    out = []
    for y in sorted(by_year):
        v = by_year[y]
        out.append({"year": y, "n_trades": v["n"], "net_inr": round(v["pnl"], 0),
                    "return_pct": round(v["pnl"] / capital * 100, 2)})
    return out


def run_pair(name: str, base_kwargs: dict, days: list) -> dict:
    gate_off = shadow.Policy(name=f"{name}_gate_off", use_opposite_direction_gate=False, **base_kwargs)
    gate_on = shadow.Policy(name=f"{name}_gate_on", use_opposite_direction_gate=True, **base_kwargs)

    trades_off = run_policy_over_history(gate_off, days)
    trades_on = run_policy_over_history(gate_on, days)

    rows_off = to_rows([t for t in trades_off if t.outcome])
    rows_on = to_rows([t for t in trades_on if t.outcome])

    return {
        "label": name,
        "metrics_gate_off": institutional_metrics(rows_off, capital=CAPITAL),
        "metrics_gate_on": institutional_metrics(rows_on, capital=CAPITAL),
        "annual_gate_off": annual_table(rows_off, CAPITAL),
        "annual_gate_on": annual_table(rows_on, CAPITAL),
    }


def describe(results: list) -> str:
    lines = [f"Opposite-direction gate: REAL backtest (Rs{CAPITAL:,.0f} capital base)", ""]
    for r in results:
        lines.append(f"-- {r['label']} --")
        lines.append(f"  {'metric':<22}{'gate OFF':>14}{'gate ON':>14}")
        mo, mn = r["metrics_gate_off"], r["metrics_gate_on"]
        for key, fmt in (
            ("n", "{:.0f}"), ("win_rate_pct", "{:.1f}%"),
            ("total_return_pct", "{:+.1f}%"), ("max_dd_pct", "{:.1f}%"),
            ("calmar", "{:.2f}"), ("profit_factor", "{:.2f}"),
            ("expectancy_r", "{:+.4f}"), ("avg_trades_per_day", "{:.2f}"),
        ):
            vo, vn = mo.get(key), mn.get(key)
            vo_s = fmt.format(vo) if vo is not None else "n/a"
            vn_s = fmt.format(vn) if vn is not None else "n/a"
            lines.append(f"  {key:<22}{vo_s:>14}{vn_s:>14}")
        lines.append("")
        lines.append(f"  {'year':<6}{'n(off)':>8}{'net(off)':>12}{'ret%(off)':>10}   "
                     f"{'n(on)':>8}{'net(on)':>12}{'ret%(on)':>10}")
        off_by_year = {row["year"]: row for row in r["annual_gate_off"]}
        on_by_year = {row["year"]: row for row in r["annual_gate_on"]}
        for y in sorted(set(off_by_year) | set(on_by_year)):
            o = off_by_year.get(y, {"n_trades": 0, "net_inr": 0, "return_pct": 0})
            n = on_by_year.get(y, {"n_trades": 0, "net_inr": 0, "return_pct": 0})
            lines.append(f"  {y:<6}{o['n_trades']:>8}{o['net_inr']:>12,.0f}{o['return_pct']:>9.1f}%   "
                         f"{n['n_trades']:>8}{n['net_inr']:>12,.0f}{n['return_pct']:>9.1f}%")
        lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/opposite_direction_gate_backtest.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} reconstructed NIFTY days\n", flush=True)

    results = []
    print("running Anchor (gate off vs on)...", flush=True)
    results.append(run_pair("Anchor", {"use_learned_adjustment": False}, days))
    print("running Sentinel (gate off vs on)...", flush=True)
    results.append(run_pair("Sentinel", {
        "use_learned_adjustment": False,
        "strike_adjacency_band_points": 200, "cluster_window_minutes": 30,
    }, days))

    print()
    print(describe(results))
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
