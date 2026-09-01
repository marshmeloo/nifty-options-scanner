"""
Should ANCHOR run the correlated-cluster cap that Sentinel already has?

THE CASE FOR ASKING. August 2026 live: -Rs 81,835 over 199 trades, of
which Anchor is -Rs 80,902 (99%). Every other strategy is flat or
positive. On the same market, same signals:

    Anchor   Bank Nifty  62 trades   9.7% win rate   -Rs 62,843
    Sentinel Bank Nifty  40 trades  32.5% win rate   -Rs  3,892

For most of August the only structural difference was Sentinel's cluster
cap. Applying that cap retrospectively to Anchor's own August journal
(keeping the FIRST of each burst, blocking the followers) removes
Rs 63,680 of the loss; blocked trades ran 12% win rate against 21% for
the kept ones. Same-direction bursts reached 14, 10, 9, 9, 6, 5 trades
inside a 30-minute bucket.

WHY THAT IS NOT THE ANSWER. That replay removes trades from a FIXED
list. It cannot see that blocking a trade frees capital and changes
which candidate opens next -- run_policy is greedy and sequential. This
project has been caught by that exact shortcut twice: the
opposite-direction approximation read +622% where the real forward run
gave +362%, and the retrospective reversal-exit estimate predicted
drawdown would halve when it actually got worse. So this runs the cap
forward through shadow.py instead.

THE BAR. Not an aggregate. The cluster cap was originally chosen across
5 independent ~1-year windows; v1.2 was adopted on being better in 6 of
7 years. Yesterday the quiet-regime gate posted an aggregate Calmar of
6.11 -> 7.13 and was WORSE in 5 of 6 years once split -- the aggregate
came from one multi-year drawdown episode, not from per-year risk
reduction. Per-year is therefore the only reading that counts here.

    python -m research.anchor_cluster_cap_study --index BANKNIFTY
"""

import argparse
import json
from collections import defaultdict

import shadow
from research.banknifty_directional_exposure_backtest import BN_SNAPSHOT_DIR, banknifty_days
from research.concurrent_direction_exposure_study import CAPITAL, historical_days
from research.one_trade_per_day_study import institutional_metrics, to_rows

# Each index's own live band -- Bank Nifty's 500pt was picked on its own
# history (sweep_banknifty_cluster_cap.py), NIFTY's 200pt on NIFTY's.
BANDS = {"BANKNIFTY": [None, 500.0], "NIFTY": [None, 200.0]}
WINDOW_MIN = 30.0


def run(index, days, band):
    if index == "BANKNIFTY":
        ov = dict(shadow.BANKNIFTY_SENTINEL_OVERRIDES)
        kw = dict(symbol="BANKNIFTY", snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov)
    else:
        kw = {}
    policy = shadow.Policy(
        name=f"anchor_cap{band}", use_learned_adjustment=False,
        # ANCHOR's live shape: gate + reversal exit on (v1.2), and NO
        # cluster cap unless this study switches it on.
        use_opposite_direction_gate=True, use_reversal_exit=True,
        strike_adjacency_band_points=band,
        cluster_window_minutes=WINDOW_MIN if band else None, **kw)
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
    out = {}
    for y, ts in by.items():
        m = institutional_metrics(to_rows(ts), capital=CAPITAL)
        out[y] = {"n": m["n"], "return_pct": m["total_return_pct"],
                  "max_dd_pct": m["max_dd_pct"], "calmar": m["calmar"],
                  "win_rate_pct": m["win_rate_pct"], "expectancy_r": m["expectancy_r"]}
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--index", default="BANKNIFTY", choices=["BANKNIFTY", "NIFTY"])
    p.add_argument("--out", default=None)
    args = p.parse_args()
    out_path = args.out or f"logs/anchor_cluster_cap_{args.index.lower()}.json"

    days = banknifty_days() if args.index == "BANKNIFTY" else historical_days()
    print(f"ANCHOR on {args.index}: {len(days)} days | cap window {WINDOW_MIN:.0f}min\n", flush=True)

    results, yearly = {}, {}
    print(f"  {'cap':>8}{'trades':>8}{'win%':>7}{'return':>10}{'maxDD':>8}{'calmar':>8}{'PF':>7}{'expR':>9}")
    for band in BANDS[args.index]:
        ts = run(args.index, days, band)
        m = institutional_metrics(to_rows(ts), capital=CAPITAL)
        key = "off" if band is None else f"{band:.0f}pt"
        results[key] = m
        yearly[key] = per_year(ts)
        print(f"  {key:>8}{m['n']:>8}{m['win_rate_pct']:>7.1f}"
              f"{m['total_return_pct']:>9.1f}%{m['max_dd_pct']:>7.1f}%"
              f"{m['calmar']:>8.2f}{m['profit_factor']:>7.2f}{m['expectancy_r']:>9.4f}", flush=True)

    keys = list(results)
    if len(keys) == 2:
        a, b = keys
        ya, yb = yearly[a], yearly[b]
        print()
        print(f"  {'year':<6}{'trades':>14}{'return ' + a:>13}{'return ' + b:>13}"
              f"{'calmar ' + a:>13}{'calmar ' + b:>13}   verdict")
        better = worse = 0
        for y in sorted(set(ya) | set(yb)):
            x, z = ya.get(y), yb.get(y)
            if not x or not z:
                continue
            cx, cz = x["calmar"] or 0, z["calmar"] or 0
            v = "BETTER" if cz > cx else ("worse" if cz < cx else "same")
            better += cz > cx
            worse += cz < cx
            print(f"  {y:<6}{f'{x[chr(110)]}/{z[chr(110)]}':>14}"
                  f"{x['return_pct']:>12.1f}%{z['return_pct']:>12.1f}%"
                  f"{cx:>13.2f}{cz:>13.2f}   {v}")
        print()
        print(f"  Calmar better in {better} of {better+worse} years")
        print(f"  (bar: cluster cap needed 5 independent windows; v1.2 needed 6 of 7 years.")
        print(f"   The quiet-regime gate posted a good AGGREGATE and failed 5 of 6 per-year.)")

    with open(out_path, "w") as f:
        json.dump({"aggregate": results, "yearly": yearly}, f, indent=2, default=str)
    print(f"\nwritten to {out_path}")


if __name__ == "__main__":
    main()
