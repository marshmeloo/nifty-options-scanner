"""
Does refusing to CHASE a move already made actually help, or does it just
remove trades?

ORIGIN. Every losing entry in the 2026-08-31 session gave one reason:
"Momentum aligned: X% ROC supports this direction". ROC is backward-
looking -- it can only turn negative after price has already fallen -- so
a momentum-confirmation entry is late by construction. Measured on that
session, ALL 13 Bank Nifty entries were in the direction the previous 30
minutes had already moved (PE after -57..-146 pts, CE after +97..+158),
and the worst pair never went one point favourable. The index ranged
0.66% that day: the move that fired the signal WAS the whole move.

THE GUARD. Refuse a fully-qualified candidate when spot already sits at
the extreme of its recent range in that contract's own direction -- no CE
above the Nth percentile of the lookback range, no PE below the (100-N)th.

WHY THIS SCRIPT EXISTS RATHER THAN JUST SHIPPING IT. On the session it
was designed from, the guard blocks 15-18 of 18 trades and "saves"
Rs 30-35k. That number is worthless as evidence: a filter fitted to one
day will always block that day. This project has been caught by exactly
this shape twice in a week -- an approximation that looked like a clean
win for the opposite-direction gate, and a retrospective reversal-exit
estimate that got drawdown backwards -- so the only figure worth quoting
is a forward replay over history the rule never saw.

    python -m research.extension_guard_study
"""

import argparse
import json

import shadow
from research.banknifty_directional_exposure_backtest import BN_SNAPSHOT_DIR, banknifty_days
from research.concurrent_direction_exposure_study import CAPITAL, historical_days
from research.directional_exposure_backtest import annual_table
from research.one_trade_per_day_study import institutional_metrics, to_rows

PCTILES = [None, 0.95, 0.90, 0.85, 0.80, 0.70]
LOOKBACKS = [30.0]


def run(index, days, pctile, lookback):
    if index == "BANKNIFTY":
        ov = shadow.BANKNIFTY_SENTINEL_OVERRIDES
        kw = dict(symbol="BANKNIFTY", snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov,
                  strike_adjacency_band_points=ov["CLUSTER_CAP_ADJACENCY_POINTS"],
                  cluster_window_minutes=ov["CLUSTER_CAP_WINDOW_MINUTES"])
    else:
        kw = dict(strike_adjacency_band_points=200, cluster_window_minutes=30)
    policy = shadow.Policy(
        name=f"{index}_ext{pctile}", use_learned_adjustment=False,
        use_opposite_direction_gate=True, use_reversal_exit=True,
        extension_guard_pctile=pctile, extension_lookback_minutes=lookback, **kw)
    trades = []
    for d in days:
        try:
            trades.extend(shadow.run_policy(d, policy))
        except Exception:
            pass
    closed = [t for t in trades if t.outcome]
    rows = to_rows(closed)
    m = institutional_metrics(rows, capital=CAPITAL)
    m["annual"] = annual_table(rows, CAPITAL)
    return m


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--index", default="BANKNIFTY", choices=["BANKNIFTY", "NIFTY"])
    p.add_argument("--out", default="logs/extension_guard_study.json")
    args = p.parse_args()

    days = banknifty_days() if args.index == "BANKNIFTY" else historical_days()
    print(f"{args.index}: {len(days)} reconstructed days, "
          f"lookback {LOOKBACKS[0]:.0f}min\n", flush=True)

    out = {}
    hdr = f"  {'guard':>8}{'trades':>8}{'win%':>7}{'return':>10}{'maxDD':>8}{'calmar':>8}{'PF':>7}{'expR':>9}"
    print(hdr)
    for pct in PCTILES:
        m = run(args.index, days, pct, LOOKBACKS[0])
        key = "off" if pct is None else f"{pct:.2f}"
        out[key] = m
        print(f"  {key:>8}{m['n']:>8}{m['win_rate_pct']:>7.1f}"
              f"{m['total_return_pct']:>9.1f}%{m['max_dd_pct']:>7.1f}%"
              f"{m['calmar']:>8.2f}{m['profit_factor']:>7.2f}{m['expectancy_r']:>9.4f}", flush=True)

    base = out["off"]
    print()
    for k, m in out.items():
        if k == "off":
            continue
        print(f"  {k}: trades {m['n']/base['n']*100:5.1f}% of baseline | "
              f"return {m['total_return_pct'] - base['total_return_pct']:+7.1f}pp | "
              f"drawdown {m['max_dd_pct'] - base['max_dd_pct']:+6.2f}pp | "
              f"Calmar {base['calmar']:.2f} -> {m['calmar']:.2f}")

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
