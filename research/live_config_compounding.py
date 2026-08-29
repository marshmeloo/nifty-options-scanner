"""
Does compounding do anything for the SHIPPED live Sentinel v1.2 config?

The live config is MAX_LOTS_PER_TRADE = 1 and MAX_RISK_PER_TRADE_PCT = 1.
lots = min(capital*1% // risk_per_lot, 1) -- so once capital clears one
lot's risk, MORE capital cannot buy a bigger position. The obvious
prediction is that compounding is nearly inert here, and that the growth
seen in research/compounding_study.py came from the 2-lot cap it used,
not from compounding as such.

Nearly, not entirely: a growing balance still loosens two things that
are expressed as PERCENTAGES of capital -- MAX_TOTAL_EXPOSURE_PCT (20%)
and the MAX_DAILY_LOSS_PCT (3%) circuit breaker -- and it lifts the
sizing floor that silently drops trades whose risk_per_lot exceeds the
1% budget. So a compounding run can still take MORE TRADES over time
even though each one stays a single lot.

This measures which of those it is, on both indices, rather than
asserting it. Compare `n_trades` against the fixed-capital run: if
compounding only loosens the filter, trade COUNT rises while per-trade
size does not.

    python -m research.live_config_compounding
"""

import argparse
import json

from research.banknifty_directional_exposure_backtest import banknifty_days
from research.compounding_study import run_compounding
from research.concurrent_direction_exposure_study import historical_days

# The real shipped values -- config.MAX_LOTS_PER_TRADE / MAX_RISK_PER_TRADE_PCT
LIVE_MAX_LOTS = 1
LIVE_RISK_PCT = 1.0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="logs/live_config_compounding.json")
    args = p.parse_args()

    nd, bd = historical_days(), banknifty_days()
    print(f"NIFTY {len(nd)} days | BANK NIFTY {len(bd)} days")
    print(f"live config: {LIVE_MAX_LOTS} lot max, {LIVE_RISK_PCT}% risk/trade, "
          f"uncapped trades/day (full v1.2)\n", flush=True)

    # Bank Nifty cannot place a trade below ~Rs 1.35L (premium band 300-800
    # x lot 30), so starting it at Rs 1L would simply never trade.
    runs = [
        ("NIFTY", nd, 100_000), ("NIFTY", nd, 500_000),
        ("BANKNIFTY", bd, 150_000), ("BANKNIFTY", bd, 500_000),
    ]
    out = {}
    for index, days, start in runs:
        key = f"{index} @ Rs {start:,}"
        print(f"  running {key} (compounding, live config)...", flush=True)
        out[key] = run_compounding(days, start, LIVE_MAX_LOTS, LIVE_RISK_PCT,
                                   max_trades_per_day=None, index=index)
        r = out[key]
        print(f"     -> final Rs {r['final_capital']:,.0f}  CAGR {r['cagr_pct']:.1f}%  "
              f"maxDD {r['max_dd_pct']:.1f}%  trades {r['n_trades']}", flush=True)

    print()
    print(f"  {'run':<24}{'start':>12}{'final':>14}{'CAGR':>9}{'max DD':>9}{'trades':>8}")
    for key in out:
        r = out[key]
        print(f"  {key:<24}{r['start_capital']:>12,.0f}{r['final_capital']:>14,.0f}"
              f"{r['cagr_pct']:>8.1f}%{r['max_dd_pct']:>8.1f}%{r['n_trades']:>8}")

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
