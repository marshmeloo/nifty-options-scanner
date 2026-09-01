"""
Should the system refuse to trade a QUIET day?

ORIGIN. market_regime.py has computed a live "is today unusually
quiet/wild" read since 2026-07-30, for an unrelated reason, and NOTHING
GATES ON IT -- grep finds only logging. On 2026-08-31 it printed QUIET
every single cycle (Bank Nifty p0 at the open, p7 by 10% elapsed, p9 by
20%, flat at p13 all afternoon; NIFTY p1). The day used less range by
11am than 93% of days use in total. The system traded it 27 times and
lost Rs 37,875.

WHY THIS AND NOT THE EXTENSION GUARD. research/extension_guard_study.py
tested the other reading of that session -- that entries were bad
because they CHASED a move already made -- and it failed decisively on
1,244 Bank Nifty days: every threshold cut return by 437-632pp, dropped
win rate at all five settings, and only improved drawdown at one (by
0.68pp, for 543pp of return). Chasing continuation IS the edge. So the
defect is not HOW entries are picked but WHETHER the day is worth
trading, which leaves entry logic untouched.

NO LOOK-AHEAD. market_regime.get_range_distribution() fetches a LIVE
180-day window; applied to 2021 history that is look-ahead. This builds
a ROLLING baseline instead: for day D, the trailing N days' full ranges,
strictly before D. Within the day, only the range up to the current
cycle is used, with an elapsed floor -- market_regime's own docstring
warns an in-progress range is partial, so at 09:30 every day looks like
the quietest on record.

READ THE RESULT SCEPTICALLY. Quiet days may be where the edge LIVES;
nobody has checked. This project has killed three plausible ideas on
exactly this kind of test (the extension guard above, the untimed
cluster cap, the expiry-day rule), and a fourth would be an ordinary
outcome, not a surprise.

    python -m research.quiet_regime_study
"""

import argparse
import json

import shadow
import snapshot_recorder
from research.banknifty_directional_exposure_backtest import BN_SNAPSHOT_DIR, banknifty_days
from research.concurrent_direction_exposure_study import CAPITAL
from research.one_trade_per_day_study import institutional_metrics, to_rows

BASELINE_DAYS = 121          # matches what market_regime reports live
PCTILES = [None, 10, 20, 25, 33]
MIN_ELAPSED = [20.0]


def day_range_pct(candles):
    if not candles or not candles[0].open:
        return None
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    return (hi - lo) / candles[0].open * 100.0 if hi > lo else None


def build_day_ranges(days, snapshot_dir, symbol):
    """Full-session range % per day, for the rolling baseline."""
    out = {}
    for d in days:
        best = []
        for _s, candles, _m in snapshot_recorder.load_day(d, snapshot_dir=snapshot_dir, symbol=symbol):
            if candles and len(candles) > len(best):
                best = candles
        r = day_range_pct(best)
        if r is not None:
            out[d] = r
    return out


def run(days, day_ranges, pctile, min_elapsed):
    ov = shadow.BANKNIFTY_SENTINEL_OVERRIDES
    trades = []
    ordered = [d for d in days if d in day_ranges]
    for i, d in enumerate(ordered):
        # STRICTLY trailing: days before this one only.
        prior = [day_ranges[x] for x in ordered[max(0, i - BASELINE_DAYS):i]]
        policy = shadow.Policy(
            name=f"quiet{pctile}", use_learned_adjustment=False,
            symbol="BANKNIFTY", snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov,
            strike_adjacency_band_points=ov["CLUSTER_CAP_ADJACENCY_POINTS"],
            cluster_window_minutes=ov["CLUSTER_CAP_WINDOW_MINUTES"],
            use_opposite_direction_gate=True, use_reversal_exit=True,
            quiet_regime_block_pctile=pctile,
            quiet_regime_min_elapsed_pct=min_elapsed,
            regime_baseline=sorted(prior) if len(prior) >= 30 else None)
        try:
            trades.extend(shadow.run_policy(d, policy))
        except Exception:
            pass
    closed = [t for t in trades if t.outcome]
    return institutional_metrics(to_rows(closed), capital=CAPITAL)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="logs/quiet_regime_study.json")
    args = p.parse_args()

    days = banknifty_days()
    print(f"BANKNIFTY: {len(days)} days | trailing baseline {BASELINE_DAYS}d, "
          f"elapsed floor {MIN_ELAPSED[0]:.0f}%\n", flush=True)
    print("  building per-day ranges...", flush=True)
    day_ranges = build_day_ranges(days, BN_SNAPSHOT_DIR, "BANKNIFTY")
    print(f"  {len(day_ranges)} days with a usable range\n", flush=True)

    out = {}
    print(f"  {'block<=p':>9}{'trades':>8}{'win%':>7}{'return':>10}{'maxDD':>8}{'calmar':>8}{'PF':>7}{'expR':>9}")
    for pct in PCTILES:
        m = run(days, day_ranges, pct, MIN_ELAPSED[0])
        key = "off" if pct is None else f"p{pct}"
        out[key] = m
        print(f"  {key:>9}{m['n']:>8}{m['win_rate_pct']:>7.1f}"
              f"{m['total_return_pct']:>9.1f}%{m['max_dd_pct']:>7.1f}%"
              f"{m['calmar']:>8.2f}{m['profit_factor']:>7.2f}{m['expectancy_r']:>9.4f}", flush=True)

    base = out["off"]
    print()
    for k, m in out.items():
        if k == "off":
            continue
        print(f"  {k}: trades {m['n']/base['n']*100:5.1f}% of baseline | "
              f"return {m['total_return_pct']-base['total_return_pct']:+7.1f}pp | "
              f"drawdown {m['max_dd_pct']-base['max_dd_pct']:+6.2f}pp | "
              f"Calmar {base['calmar']:.2f} -> {m['calmar']:.2f}")

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
