"""
The same "Liquidity Sniper" model, run on MCX GOLD futures.

WHY GOLD IS A FAIRER TEST THAN BANK NIFTY. The model is a 15-min
daytrading system that needs room for a swing to form, be swept, produce
a BOS and then retrace 71%. An NSE session is 6h15m -- 25 fifteen-minute
bars. MCX gold trades ~09:00-23:30, about 175 five-minute bars a day, so
~58 fifteen-minute bars: 2.3x the structure per session. If the rules
were starved for bars on Bank Nifty (measured: ~4 trades/year), gold is
where they get their best chance.

Gold is also a FUTURE, so the SELL side is directly tradeable -- no
option-premium translation, no strike selection, and the model's
index-level stop and target apply literally.

DATA: logs/commodities/gold_intraday_5m.json, backfilled by
commodity_source.py from Dhan /v2/charts/historical with
exchangeSegment=MCX_COMM. Note the sample is SHORT -- ~70 trading days
against Bank Nifty's 1,244 -- so this measures FREQUENCY credibly and
edge only weakly.

All strategy logic, parameters and the grid are imported unchanged from
research.liquidity_sniper_study, so the two instruments are compared on
identical rules.

    python -m research.liquidity_sniper_gold
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from research.liquidity_sniper_study import (
    BARS_PER_15MIN, BARS_PER_4H, DEFAULT_PRM, GRID, Candle,
    bias_at, describe, find_setups, resample, simulate, summarise,
)

GOLD_5M = Path(__file__).parent.parent / "logs" / "commodities" / "gold_intraday_5m.json"


def load_gold_series(path=GOLD_5M):
    raw = json.loads(path.read_text())
    candles = [
        Candle(timestamp=datetime.fromisoformat(c["timestamp"]), open=c["open"],
               high=c["high"], low=c["low"], close=c["close"],
               volume=c.get("volume", 0) or 0)
        for c in raw["candles"]
    ]
    candles.sort(key=lambda c: c.timestamp)

    per_day = {}
    for c in candles:
        per_day.setdefault(c.timestamp.date().isoformat(), []).append(c)

    five_all, m15, day_of = [], [], []
    for day in sorted(per_day):
        bars = per_day[day]
        five_all.extend(bars)
        fifteen = resample(bars, BARS_PER_15MIN)
        m15.extend(fifteen)
        day_of.extend([day] * len(fifteen))

    four_h = resample(five_all, BARS_PER_4H)
    per_4h = BARS_PER_4H // BARS_PER_15MIN
    bias_of = [bias_at(four_h, k // per_4h) for k in range(len(m15))]
    return m15, day_of, bias_of, len(per_day), len(four_h)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/liquidity_sniper_gold.json")
    args = p.parse_args()

    m15, day_of, bias_of, n_days, n_4h = load_gold_series()
    print(f"MCX GOLD: {n_days} days, {len(m15)} 15-min bars "
          f"({len(m15) / max(n_days,1):.0f}/day), {n_4h} 4H bars")
    print(f"          vs Bank Nifty's 25 fifteen-min bars per NSE session\n", flush=True)

    out = []
    print(f"  {'swing':>6}{'wick%':>7}{'limit':>7}{'trades':>8}{'per yr':>8}"
          f"{'win%':>7}{'expR':>9}{'totalR':>9}{'PF':>8}{'maxDDr':>8}")
    for g in GRID:
        prm = dict(DEFAULT_PRM, **g)
        setups = find_setups(m15, bias_of, prm)
        r = summarise(simulate(m15, day_of, bias_of, prm), "gold", n_days)
        r["params"] = g
        r["n_setups"] = len(setups)
        out.append(r)
        print(f"  {g['swing_lookback']:>6}{g['min_wick_pct']:>7}{g['limit_max_bars']:>7}"
              f"{r['n_trades']:>8}{r.get('trades_per_year', 0):>8}"
              f"{r.get('win_rate_pct', 0):>7}{r.get('expectancy_r', 0):>9}"
              f"{r.get('total_r', 0):>9}{str(r.get('profit_factor')):>8}"
              f"{r.get('max_dd_r', 0):>8}", flush=True)

    best = max((r for r in out if r["n_trades"] >= 20),
               key=lambda r: r.get("total_r", 0), default=None)
    print()
    if best:
        print(f"  best config with >=20 trades: {best['params']}")
        print(f"    {best['n_trades']} trades from {best['n_setups']} setups, "
              f"{best['win_rate_pct']}% win, expectancy {best['expectancy_r']:+}R, "
              f"total {best['total_r']:+}R, PF {best['profit_factor']}, "
              f"maxDD {best['max_dd_r']}R")
    else:
        most = max(out, key=lambda r: r["n_trades"])
        print(f"  NO configuration reached 20 trades. Most active: {most['params']} "
              f"-> {most['n_trades']} trades ({most.get('trades_per_year')}/yr) "
              f"from {most['n_setups']} setups")

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
