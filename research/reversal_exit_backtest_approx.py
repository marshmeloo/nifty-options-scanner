"""
Institutional metrics for the reversal-exit idea, BEFORE building it for
real -- same "approximate first, then decide whether to build the real
thing" step the opposite-direction gate itself went through.

WHAT THIS DOES, AND THE SAME CAVEAT AS LAST TIME
-----------------------------------------------------
Takes Anchor v1.1's REAL trade sequence exactly as it happened (gate ON,
what's actually live), and for every trade that research/reversal_exit_study.py
found got a genuine reversal signal, SWAPS that trade's outcome for what
closing right at the signal would have realised -- everything else about
the sequence (every other trade, every other outcome) stays untouched.

This is a real improvement in method over concurrent_direction_exposure_study.py's
first pass at the gate itself, which DELETED trades outright (including
ones a real rule would never have touched) -- here, only the specific
trades that actually got a real signal are touched, and only their exit
is changed, not whether they existed. But the SAME caveat that made the
gate's first approximation overstate its effect still applies in a
smaller way: this doesn't capture that exiting a position early frees
capital/exposure sooner, which could let a DIFFERENT trade open earlier
in a real simulation and cascade differently. The real answer needs an
actual exit rule built into shadow.py and the full policy re-run --
this is the "is it worth building that" estimate, not the final number.

Run: python -m research.reversal_exit_backtest_approx
"""

import argparse
import json
from collections import defaultdict

import config
import shadow
from research.concurrent_direction_exposure_study import CAPITAL, annual_table, historical_days, run_policy_over_history
from research.one_trade_per_day_study import institutional_metrics
from research.reversal_exit_study import collect as collect_reversal_events


def to_rows_actual(trades):
    return [[t.opened_at[:10], round(t.net_inr or 0), round(t.net_r or 0, 3),
             round((t.entry - t.stop) * config.NIFTY_LOT_SIZE)]
            for t in trades if t.outcome]


def to_rows_with_reversal_exit(trades, reversal_records):
    by_id = {}
    for r in reversal_records:
        rid = (r["day"], tuple(r["key"]), r["opened_at"])
        by_id[rid] = r

    rows, n_replaced = [], 0
    for t in trades:
        if not t.outcome:
            continue
        day = t.opened_at[:10]
        rid = (day, (t.strike, t.option_type), t.opened_at)
        rec = by_id.get(rid)
        if rec is not None and rec.get("close_now_inr_approx") is not None:
            net_inr, net_r = rec["close_now_inr_approx"], rec["close_now_r"]
            n_replaced += 1
        else:
            net_inr, net_r = (t.net_inr or 0), (t.net_r or 0)
        rows.append([day, round(net_inr), round(net_r, 3),
                    round((t.entry - t.stop) * config.NIFTY_LOT_SIZE)])
    return rows, n_replaced


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/reversal_exit_backtest_approx.json")
    args = p.parse_args()

    days = historical_days()
    print(f"{len(days)} reconstructed NIFTY days\n", flush=True)

    print("running Anchor v1.1 (gate on, the real live policy)...", flush=True)
    policy = shadow.Policy(name="anchor_v1_1", use_learned_adjustment=False)
    trades = run_policy_over_history(policy, days)
    print(f"  {len(trades)} trades", flush=True)

    print("collecting reversal-signal events...", flush=True)
    reversal_records = collect_reversal_events(days)
    print(f"  {len(reversal_records)} events", flush=True)

    rows_actual = to_rows_actual(trades)
    rows_exit, n_replaced = to_rows_with_reversal_exit(trades, reversal_records)
    print(f"  {n_replaced} of {len(rows_actual)} trades had their outcome swapped\n", flush=True)

    m_actual = institutional_metrics(rows_actual, capital=CAPITAL)
    m_exit = institutional_metrics(rows_exit, capital=CAPITAL)
    annual = annual_table(rows_actual, rows_exit, CAPITAL)

    lines = [f"Reversal-exit APPROXIMATION -- Rs{CAPITAL:,.0f} capital base", "",
             f"{'metric':<22}{'v1.1 as-is':>14}{'+ reversal exit':>18}"]
    for key, fmt in (
        ("n", "{:.0f}"), ("win_rate_pct", "{:.1f}%"),
        ("total_return_pct", "{:+.1f}%"), ("max_dd_pct", "{:.1f}%"),
        ("calmar", "{:.2f}"), ("profit_factor", "{:.2f}"),
        ("expectancy_r", "{:+.4f}"), ("avg_trades_per_day", "{:.2f}"),
    ):
        va, ve = m_actual.get(key), m_exit.get(key)
        lines.append(f"{key:<22}{fmt.format(va) if va is not None else 'n/a':>14}"
                     f"{fmt.format(ve) if ve is not None else 'n/a':>18}")
    lines.append("")
    lines.append(f"{'year':<6}{'net Rs (as-is)':>16}{'ret% (as-is)':>14}   "
                 f"{'net Rs (+exit)':>16}{'ret% (+exit)':>14}")
    for row in annual:
        lines.append(f"{row['year']:<6}{row['net_inr_all']:>16,.0f}{row['return_pct_all']:>13.1f}%   "
                     f"{row['net_inr_excl_overlap']:>16,.0f}{row['return_pct_excl_overlap']:>13.1f}%")

    print()
    print("\n".join(lines))
    with open(args.out, "w") as f:
        json.dump({"metrics_as_is": m_actual, "metrics_with_reversal_exit": m_exit,
                   "annual": annual, "n_replaced": n_replaced}, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
