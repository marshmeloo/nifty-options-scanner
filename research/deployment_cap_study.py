"""
Should there be a cap on how much PREMIUM is committed at once?

WHY THIS IS NOT ALREADY COVERED. config.MAX_TOTAL_EXPOSURE_PCT is 20%
and sounds exactly like this, but trade_tracker.compute_risk_state sums
(entry - stop) x lot x lots -- RISK AT STOP, not money handed over. On
2026-08-27 the live book committed Rs 4,30,860 of premium at once, 86%
of the Rs 5,00,000 allocation, Anchor alone holding Rs 3,23,362 (65%),
while that guard read roughly 3%. Actual capital commitment has never
been bounded by anything.

This was only noticed because the dashboard's capital figure was being
SUMMED across trades rather than measured as peak-simultaneous, which
hid it entirely -- fixed the same day.

WHAT IS TESTED. A cap that refuses a new entry when premium already
committed to open positions, plus the candidate's own, would exceed N%
of TOTAL_CAPITAL. Swept on both strategies, since Anchor is the one that
hit 65% and Sentinel (now primary) peaked far lower -- the cap may
simply never bind on Sentinel, which is itself the answer.

THE BAR, and why it is worth stating up front. Two filters aimed at the
same session were tested yesterday and BOTH failed: an extension guard
(worse at every setting on 1,244 days) and a quiet-regime gate (good
aggregate, worse in 5 of 6 years). Per-year, not aggregate, decides.
A third failure would be an ordinary outcome.

    python -m research.deployment_cap_study --strategy anchor
"""

import argparse
import json
from collections import defaultdict

import shadow
from research.banknifty_directional_exposure_backtest import BN_SNAPSHOT_DIR, banknifty_days
from research.concurrent_direction_exposure_study import CAPITAL
from research.one_trade_per_day_study import institutional_metrics, to_rows

PCTS = [None, 60, 40, 30, 20]


def run(strategy, days, pct):
    ov = dict(shadow.BANKNIFTY_SENTINEL_OVERRIDES)
    kw = dict(symbol="BANKNIFTY", snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov)
    if strategy == "sentinel":
        kw.update(strike_adjacency_band_points=ov["CLUSTER_CAP_ADJACENCY_POINTS"],
                  cluster_window_minutes=ov["CLUSTER_CAP_WINDOW_MINUTES"])
    policy = shadow.Policy(
        name=f"{strategy}_dep{pct}", use_learned_adjustment=False,
        use_opposite_direction_gate=True, use_reversal_exit=True,
        max_deployed_pct=pct, **kw)
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
    p.add_argument("--strategy", default="anchor", choices=["anchor", "sentinel"])
    p.add_argument("--out", default=None)
    args = p.parse_args()
    out_path = args.out or f"logs/deployment_cap_{args.strategy}.json"

    days = banknifty_days()
    print(f"{args.strategy.upper()} on BANKNIFTY: {len(days)} days\n", flush=True)

    res, yr = {}, {}
    print(f"  {'cap':>7}{'trades':>8}{'win%':>7}{'return':>10}{'maxDD':>8}{'calmar':>8}{'PF':>7}{'expR':>9}")
    for pct in PCTS:
        ts = run(args.strategy, days, pct)
        m = institutional_metrics(to_rows(ts), capital=CAPITAL)
        key = "off" if pct is None else f"{pct}%"
        res[key], yr[key] = m, per_year(ts)
        print(f"  {key:>7}{m['n']:>8}{m['win_rate_pct']:>7.1f}{m['total_return_pct']:>9.1f}%"
              f"{m['max_dd_pct']:>7.1f}%{m['calmar']:>8.2f}{m['profit_factor']:>7.2f}"
              f"{m['expectancy_r']:>9.4f}", flush=True)

    base = res["off"]
    best = max((k for k in res if k != "off"),
               key=lambda k: res[k]["calmar"] or 0, default=None)
    print()
    for k in res:
        if k == "off":
            continue
        m = res[k]
        print(f"  {k}: trades {m['n']/base['n']*100:5.1f}% | return "
              f"{m['total_return_pct']-base['total_return_pct']:+7.1f}pp | drawdown "
              f"{m['max_dd_pct']-base['max_dd_pct']:+6.2f}pp | Calmar {base['calmar']:.2f} -> {m['calmar']:.2f}")

    if best:
        ya, yb = yr["off"], yr[best]
        print(f"\n  PER-YEAR, best cap ({best}) vs off -- this is the verdict, not the aggregate")
        print(f"  {'year':<6}{'return off':>12}{'return on':>12}{'calmar off':>12}{'calmar on':>12}   ")
        bt = ws = 0
        for y in sorted(set(ya) | set(yb)):
            x, z = ya.get(y), yb.get(y)
            if not x or not z:
                continue
            cx, cz = x["calmar"] or 0, z["calmar"] or 0
            bt += cz > cx
            ws += cz < cx
            print(f"  {y:<6}{x['total_return_pct']:>11.1f}%{z['total_return_pct']:>11.1f}%"
                  f"{cx:>12.2f}{cz:>12.2f}   {'BETTER' if cz > cx else 'worse'}")
        print(f"\n  Calmar better in {bt} of {bt+ws} years")

    with open(out_path, "w") as f:
        json.dump({"aggregate": res, "yearly": yr}, f, indent=2, default=str)
    print(f"\nwritten to {out_path}")


if __name__ == "__main__":
    main()
