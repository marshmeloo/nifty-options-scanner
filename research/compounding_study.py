"""
NIFTY Sentinel, 1 trade/day, WITH COMPOUNDING -- capital grows (and
shrinks) with realised P&L, and every later trade is sized off the new
balance.

WHY IT NEEDS ITS OWN SCRIPT. Every other backtest here sizes against a
FIXED config.TOTAL_CAPITAL, so profits never fund larger positions and a
"+282%" figure is a flat sum on a static base, not a growth rate.
Compounding changes the causal structure: today's profit changes
tomorrow's position size, so the run has to advance day by day, updating
capital between days. It cannot be recovered by scaling a fixed-base
result afterwards.

THE CEILING TO WATCH FOR. lots = min(capital*RISK% // risk_per_lot,
MAX_LOTS_PER_TRADE). With a hard lot cap, compounding stops mattering
the moment capital is large enough to afford the cap on every trade --
after that, growth is flat absolute income and the % return DECLINES as
the base grows. Each variant below reports the day it stops sizing up,
so that ceiling is visible rather than implied.

Intraday strategy: every position closes the same day, so capital is
updated end-of-day with no open-position accounting.

    python -m research.compounding_study
"""

import argparse
import json

import shadow
from research.banknifty_directional_exposure_backtest import (
    BN_SNAPSHOT_DIR, banknifty_days)
from research.concurrent_direction_exposure_study import historical_days


def run_compounding(days, start_capital, max_lots, risk_pct, lot=65, max_trades_per_day=1,
                    index="NIFTY"):
    capital = float(start_capital)
    peak = capital
    max_dd_pct = 0.0
    equity = []
    n_trades = 0
    lots_hist = []
    capped_since = None

    for day in days:
        ov = {"TOTAL_CAPITAL": round(capital, 2),
              "MAX_LOTS_PER_TRADE": max_lots,
              "MAX_RISK_PER_TRADE_PCT": risk_pct}
        extra = {}
        if index == "BANKNIFTY":
            # Bank Nifty needs its live process's WHOLE config patch set,
            # not just the lot size -- see shadow.BANKNIFTY_SENTINEL_OVERRIDES.
            bn = shadow.BANKNIFTY_SENTINEL_OVERRIDES
            ov = {**bn, **ov}
            extra = {"symbol": "BANKNIFTY",
                     "snapshot_dir": str(BN_SNAPSHOT_DIR),
                     "strike_adjacency_band_points": bn["CLUSTER_CAP_ADJACENCY_POINTS"],
                     "cluster_window_minutes": bn["CLUSTER_CAP_WINDOW_MINUTES"]}
        else:
            extra = {"strike_adjacency_band_points": 200, "cluster_window_minutes": 30}
        policy = shadow.Policy(
            name="compound", use_learned_adjustment=False,
            max_trades_per_day=max_trades_per_day, use_opposite_direction_gate=True,
            use_reversal_exit=True, config_overrides=ov, **extra)
        try:
            trades = [t for t in shadow.run_policy(day, policy) if t.outcome]
        except Exception:
            trades = []
        for t in trades:
            n_trades += 1
            # net_inr is already lots-aware inside _finalise
            capital += t.net_inr
            lots_hist.append(getattr(t, "lots", None))
        peak = max(peak, capital)
        dd = (peak - capital) / peak * 100 if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)
        equity.append({"day": day, "capital": round(capital, 2)})
        if capped_since is None and capital * risk_pct / 100 >= max_lots * 3624:
            capped_since = day          # 3624 = widest NIFTY risk/lot observed
        if capital <= 0:
            print(f"    RUINED on {day}")
            break

    # Calendar span, not month arithmetic: a short run (a smoke test over a
    # handful of days in one month) used to give years == 0 and divide by
    # zero in the CAGR below.
    from datetime import date as _date
    d0, d1 = _date.fromisoformat(days[0]), _date.fromisoformat(days[-1])
    years = max((d1 - d0).days / 365.25, 1 / 365.25)
    cagr = ((capital / start_capital) ** (1 / years) - 1) * 100 if capital > 0 else -100.0
    return {
        "start_capital": start_capital, "final_capital": round(capital, 2),
        "profit": round(capital - start_capital, 2),
        "total_return_pct": round((capital / start_capital - 1) * 100, 2),
        "cagr_pct": round(cagr, 2), "max_dd_pct": round(max_dd_pct, 2),
        "n_trades": n_trades, "years": round(years, 2),
        "sizing_ceiling_reached": capped_since,
        "equity": equity,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--capital", type=float, default=100_000)
    p.add_argument("--max-trades-per-day", type=int, default=1,
                   help="1 = the one-trade-a-day variant; 0 = UNCAPPED, the full v1.2")
    p.add_argument("--out", default="logs/compounding_study.json")
    args = p.parse_args()

    days = historical_days()
    cap_per_day = args.max_trades_per_day or None      # 0 -> None -> uncapped, the full v1.2
    print(f"{len(days)} days, {days[0]} -> {days[-1]}, start Rs {args.capital:,.0f}, "
          f"trades/day = {cap_per_day or 'UNCAPPED (full v1.2)'}\n", flush=True)

    variants = [
        ("2 lots, 2% risk", 2, 2.0),
        ("2 lots, 1% risk", 2, 1.0),
        ("no lot cap, 2% risk", 999, 2.0),
    ]
    out = {}
    for label, max_lots, risk in variants:
        print(f"  running {label} (compounding)...", flush=True)
        out[label] = run_compounding(days, args.capital, max_lots, risk,
                                     max_trades_per_day=cap_per_day)

    print()
    print(f"  {'variant':<22}{'final':>14}{'profit':>14}{'CAGR':>9}{'max DD':>9}{'trades':>8}")
    for label, _m, _r in variants:
        r = out[label]
        print(f"  {label:<22}{r['final_capital']:>14,.0f}{r['profit']:>14,.0f}"
              f"{r['cagr_pct']:>8.1f}%{r['max_dd_pct']:>8.1f}%{r['n_trades']:>8}")
    print()
    for label, _m, _r in variants:
        r = out[label]
        print(f"  {label}: stops sizing up around {r['sizing_ceiling_reached'] or 'never (never hit the cap)'}")

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
