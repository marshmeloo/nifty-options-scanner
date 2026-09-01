"""
Should an overbought RSI stop the system buying calls?

THE OBSERVATION. On 2026-09-01 Anchor opened THIRTEEN adjacent Bank
Nifty CE strikes between 11:15:00 and 11:22:16 -- 0 wins, -Rs 29,943 --
at the top of a 314-point (0.55%) rally, immediately before a 500-point
decline. A 1-minute RSI(14) read ~77 at that moment.

TWO REASONS THE SYSTEM DID NOT SEE IT:

  1. RESOLUTION. Live computes RSI(14) on 5-MINUTE candles, a 70-minute
     lookback. It read 66.8 "neutral" at 11:15 and only crossed 70 at
     11:20, by which point NINE of the thirteen were already open.
  2. IT WOULD NOT HAVE MATTERED ANYWAY. Under SCORING_MODE =
     "momentum_only", scanner.py replaces the entire weighted score with
     one of three constants keyed on momentum alignment -- every live
     trade scores exactly 6.0. The -0.25 "CE momentum may be exhausted"
     penalty is computed, written into `reasons`, and discarded.

So the test is a GATE, not a faster indicator, and the sweep covers both
the PERIOD (how fast) and the THRESHOLD (how extreme). Reconstructed
history is 5-minute, so "faster" means a shorter period: RSI(14) on
5-min is 70 minutes, RSI(7) is 35, RSI(5) is 25.

THE BAR, stated before the result. Three filters aimed at these same
sessions have been tested and REJECTED this week -- an extension guard
(worse at every setting), a quiet-regime gate (good aggregate, worse in
5 of 6 years), and a deployment cap (made drawdown monotonically worse).
Per-year decides, not the aggregate. A fourth rejection is the base case.

One reason for slight optimism: unlike those three, this signal is not
invented here. scanner.py already computes it, already labels it
"momentum may be exhausted", and already penalises it in the legacy
scorer -- it is switched off by a scoring-mode decision, not by absence.

    python -m research.rsi_exhaustion_study
"""

import argparse
import json
from collections import defaultdict

import shadow
from research.banknifty_directional_exposure_backtest import BN_SNAPSHOT_DIR, banknifty_days
from research.concurrent_direction_exposure_study import CAPITAL
from research.one_trade_per_day_study import institutional_metrics, to_rows

# (period, overbought) -- oversold mirrored at 100-overbought
VARIANTS = [None, (14, 70), (7, 70), (7, 75), (5, 70), (5, 75), (5, 80)]


def run(days, variant):
    ov = shadow.BANKNIFTY_SENTINEL_OVERRIDES
    period, obought = (None, 70) if variant is None else variant
    policy = shadow.Policy(
        name=f"rsi{variant}", use_learned_adjustment=False,
        symbol="BANKNIFTY", snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov,
        strike_adjacency_band_points=ov["CLUSTER_CAP_ADJACENCY_POINTS"],
        cluster_window_minutes=ov["CLUSTER_CAP_WINDOW_MINUTES"],
        use_opposite_direction_gate=True, use_reversal_exit=True,
        rsi_exhaustion_period=period, rsi_overbought=obought,
        rsi_oversold=100 - obought)
    trades = []
    for d in days:
        try:
            trades.extend(shadow.run_policy(d, policy))
        except Exception:
            pass
    return [t for t in trades if t.outcome]


def per_year(trades):
    by = defaultdict(list)
    for t in trades:
        by[t.opened_at[:4]].append(t)
    return {y: institutional_metrics(to_rows(ts), capital=CAPITAL) for y, ts in by.items()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="logs/rsi_exhaustion_study.json")
    args = p.parse_args()

    days = banknifty_days()
    print(f"SENTINEL on BANKNIFTY: {len(days)} days | 5-min candles, "
          f"so RSI(14)=70min RSI(7)=35min RSI(5)=25min\n", flush=True)

    res, yr = {}, {}
    print(f"  {'gate':>12}{'trades':>8}{'win%':>7}{'return':>10}{'maxDD':>8}{'calmar':>8}{'PF':>7}{'expR':>9}")
    for v in VARIANTS:
        ts = run(days, v)
        m = institutional_metrics(to_rows(ts), capital=CAPITAL)
        key = "off" if v is None else f"RSI({v[0]})>={v[1]:.0f}"
        res[key], yr[key] = m, per_year(ts)
        print(f"  {key:>12}{m['n']:>8}{m['win_rate_pct']:>7.1f}{m['total_return_pct']:>9.1f}%"
              f"{m['max_dd_pct']:>7.1f}%{m['calmar']:>8.2f}{m['profit_factor']:>7.2f}"
              f"{m['expectancy_r']:>9.4f}", flush=True)

    base = res["off"]
    print()
    for k, m in res.items():
        if k == "off":
            continue
        print(f"  {k:>12}: trades {m['n']/base['n']*100:5.1f}% | return "
              f"{m['total_return_pct']-base['total_return_pct']:+7.1f}pp | drawdown "
              f"{m['max_dd_pct']-base['max_dd_pct']:+6.2f}pp | Calmar "
              f"{base['calmar']:.2f} -> {m['calmar']:.2f}")

    best = max((k for k in res if k != "off"), key=lambda k: res[k]["calmar"] or 0, default=None)
    if best:
        ya, yb = yr["off"], yr[best]
        print(f"\n  PER-YEAR, best ({best}) vs off -- the verdict")
        bt = ws = 0
        for y in sorted(set(ya) | set(yb)):
            x, z = ya.get(y), yb.get(y)
            if not x or not z:
                continue
            cx, cz = x["calmar"] or 0, z["calmar"] or 0
            bt += cz > cx
            ws += cz < cx
            print(f"  {y}  return {x['total_return_pct']:>7.1f}% -> {z['total_return_pct']:>7.1f}%"
                  f"   Calmar {cx:>7.2f} -> {cz:>7.2f}   {'BETTER' if cz > cx else 'worse'}")
        print(f"\n  Calmar better in {bt} of {bt+ws} years")

    with open(args.out, "w") as f:
        json.dump({"aggregate": res, "yearly": yr}, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
