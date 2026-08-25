"""
Rupee-level portfolio simulation of the RVOL>=6 early-trigger rule on a
real account size. RESEARCH ONLY -- nothing here trades.

WHY THIS IS NOT JUST "mean R x number of trades"
--------------------------------------------------
The sweeps report mean R, which deliberately ignores everything that
decides whether an account actually grows: how much is risked per
trade, what happens when five names trigger the same morning and the
capital cannot cover all of them, and what the equity curve does
between the good years. A +0.13 mean R can be a comfortable account or
a ruined one depending entirely on sizing.

So this walks the three years day by day, with capital as a hard
constraint.

DECISIONS MADE HERE, AND WHY (all of them affect the answer)
--------------------------------------------------------------
  - RISK-BASED SIZING. Position size = (equity x risk_pct) / stop
    distance, so every trade risks the same fraction of the account
    regardless of the stock's price or volatility. This is standard and
    is the only sizing that makes trades comparable; sizing by fixed
    rupee value would risk wildly different amounts per trade because
    the stop distance varies with the opening range.
  - COMPOUNDING. Sizing uses CURRENT equity, not the starting 1 lakh.
    Reported alongside a non-compounding run, because compounding
    flatters a good period and punishes a bad one, and the difference
    is worth seeing rather than hiding.
  - LEVERAGE 5x, the typical Indian intraday (MIS) allowance. Total
    open position value is capped at equity x leverage. Without a cap
    the risk-based formula will happily "buy" positions the account
    could never fund, which is the most common way a backtest like this
    silently becomes fiction.
  - CONCURRENCY BY CONVICTION. When several names trigger the same day
    and capital runs out, they are taken in descending RVOL order --
    known at trigger time, so no look-ahead. Whatever does not fit is
    skipped and counted, not quietly dropped.
  - SLIPPAGE IS SWEPT, NOT ASSUMED. The measured universe median is
    ~3.3 bps per leg (2026-08-24 recording) but this reports 0 / 3.3 /
    7 / 10 / 15 so the reader can see where the strategy dies rather
    than trusting one number. Statutory costs are always deducted.

WHAT THIS STILL CANNOT TELL YOU
---------------------------------
  - Survivorship bias: today's F&O list applied to three years of
    history. Names that were delisted or dropped from F&O are absent,
    and that bias is unquantified here.
  - Fill feasibility: it assumes the whole position fills at one price.
    A large order in a thin mid-cap moves the market against itself,
    and no OHLCV backfill can model that.
  - It is a backtest of a rule found BY searching this same dataset.
    The per-year consistency check in hybrid_trigger_sweep is what
    guards against curve-fitting, not this simulation.

Run: python -m research.portfolio_sim
"""

import argparse
import json
import statistics
from collections import defaultdict

from research import stock_costs, stock_data

RVOL_LOOKBACK_DAYS = 20
MIN_LOOKBACK_DAYS = 10
RVOL_MIN = 6.0
MOVE_MAX = -1.0
MAX_TRIGGER_BAR = 2
TARGET_R = 2.0

START_CAPITAL = 100_000.0
RISK_PCT = 0.01          # 1% of equity risked per trade
LEVERAGE = 5.0           # typical Indian intraday (MIS)
SLIPPAGE_GRID = [0.0, 3.3, 7.0, 10.0, 15.0]


def cumulative_volumes(bars, upto):
    out, run = [], 0.0
    for b in bars[:upto + 1]:
        run += (b[5] or 0)
        out.append(run)
    return out


def collect_signals(symbols=None, verbose=True):
    """{day: [signal, ...]} for every RVOL>=6 early trigger. Look-ahead-safe."""
    symbols = symbols or [u["symbol"] for u in stock_data.universe()]
    by_day = defaultdict(list)

    for n, sym in enumerate(symbols, 1):
        data = stock_data.load(sym)
        if not data:
            continue
        hist = []
        for day in sorted(data):
            bars = data[day]
            if len(bars) < 40:
                hist.append(None)
                continue
            cums = cumulative_volumes(bars, MAX_TRIGGER_BAR)
            prior = [h for h in hist[-RVOL_LOOKBACK_DAYS:] if h]
            hist.append(cums)
            if len(prior) < MIN_LOOKBACK_DAYS:
                continue
            day_open = bars[0][1]
            if not day_open:
                continue

            for i in range(0, min(MAX_TRIGGER_BAR, len(bars) - 2) + 1):
                base_vals = [p[i] for p in prior if len(p) > i and p[i]]
                if len(base_vals) < MIN_LOOKBACK_DAYS:
                    continue
                baseline = statistics.mean(base_vals)
                if baseline <= 0:
                    continue
                rvol = cums[i] / baseline
                if rvol < RVOL_MIN:
                    continue
                if (bars[i][4] - day_open) / day_open * 100 > MOVE_MAX:
                    continue
                e_i = i + 1
                hi = max(b[2] for b in bars[:i + 1])
                lo = min(b[3] for b in bars[:i + 1])
                risk = hi - lo
                if risk <= 0:
                    break
                by_day[day].append({
                    "symbol": sym, "day": day, "rvol": rvol,
                    "entry": bars[e_i][1], "risk_per_share": risk,
                    "bars": bars[e_i:],
                })
                break
        if verbose and n % 40 == 0:
            print(f"  ...{n}/{len(symbols)} symbols", flush=True)
    return by_day


def exit_price(sig, entry_px):
    """Short trade: stop above, target 2R below, else close. Stop assumed
    on any bar that could have hit both (same convention as elsewhere)."""
    risk = sig["risk_per_share"]
    stop = entry_px + risk
    target = entry_px - risk * TARGET_R
    for b in sig["bars"]:
        if b[2] >= stop:
            return stop, "stop"
        if b[3] <= target:
            return target, "target"
    return sig["bars"][-1][4], "eod"


def simulate(by_day, slippage_bps, compounding=True, risk_pct=RISK_PCT):
    equity = START_CAPITAL
    peak, max_dd = equity, 0.0
    trades, skipped = [], 0
    equity_curve = []
    per_year = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0})

    for day in sorted(by_day):
        sigs = sorted(by_day[day], key=lambda s: -s["rvol"])
        base = equity if compounding else START_CAPITAL
        capital_used = 0.0
        day_pnl = 0.0

        for s in sigs:
            qty = int((base * risk_pct) // s["risk_per_share"])
            if qty < 1:
                skipped += 1
                continue
            notional = qty * s["entry"]
            if capital_used + notional > base * LEVERAGE:
                skipped += 1
                continue
            capital_used += notional

            # short: sell lower than quoted, cover higher than quoted
            slip = slippage_bps / 10_000
            fill_in = s["entry"] * (1 - slip)
            raw_out, reason = exit_price(s, s["entry"])
            fill_out = raw_out * (1 + slip)

            gross = (fill_in - fill_out) * qty
            costs = stock_costs.statutory_costs(fill_in, fill_out, qty)["total"]
            net = gross - costs
            day_pnl += net

            y = int(day[:4]) - (1 if day[5:7] < "08" else 0)
            per_year[y]["pnl"] += net
            per_year[y]["n"] += 1
            per_year[y]["wins"] += 1 if net > 0 else 0
            trades.append({"day": day, "symbol": s["symbol"], "qty": qty,
                           "net": net, "reason": reason})

        equity += day_pnl
        equity_curve.append((day, equity))
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100 if peak > 0 else 0)
        if equity <= 0:
            break

    wins = sum(1 for t in trades if t["net"] > 0)
    return {
        "slippage_bps": slippage_bps, "compounding": compounding,
        "risk_pct": risk_pct,
        "final_equity": round(equity, 2),
        "total_return_pct": round((equity / START_CAPITAL - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "n_trades": len(trades), "n_skipped_no_capital": skipped,
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0,
        "per_year": {str(y): {"pnl": round(v["pnl"], 2), "n": v["n"],
                              "win_pct": round(v["wins"] / v["n"] * 100, 1) if v["n"] else 0}
                     for y, v in sorted(per_year.items())},
        "equity_curve": equity_curve[::20],
    }


def describe(results):
    lines = [
        f"RVOL>={RVOL_MIN} early-trigger rule, Rs{START_CAPITAL:,.0f} account, "
        f"{RISK_PCT*100:.0f}% risk/trade, {LEVERAGE:.0f}x intraday leverage",
        "statutory costs always deducted; slippage swept because it is the real unknown",
        "",
        f"{'slippage':>9} {'final equity':>14} {'return':>9} {'max DD':>8} "
        f"{'trades':>7} {'win%':>6}",
    ]
    for r in results:
        lines.append(f"{r['slippage_bps']:>8.1f}b Rs{r['final_equity']:>12,.0f} "
                     f"{r['total_return_pct']:>+8.1f}% {r['max_drawdown_pct']:>7.1f}% "
                     f"{r['n_trades']:>7,} {r['win_rate_pct']:>5.1f}%")

    mid = next((r for r in results if r["slippage_bps"] == 3.3), results[0])
    lines += ["", f"per-year at the MEASURED {mid['slippage_bps']} bps slippage:",
              f"{'year':>6} {'net P&L':>14} {'trades':>8} {'win%':>7}"]
    for y, v in mid["per_year"].items():
        lines.append(f"{y:>6} Rs{v['pnl']:>12,.0f} {v['n']:>8,} {v['win_pct']:>6.1f}%")
    if mid["n_skipped_no_capital"]:
        lines.append(f"\n{mid['n_skipped_no_capital']:,} signals skipped -- "
                     f"capital or size limit (real constraint, not a filter)")
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/portfolio_sim.json")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    syms = [u["symbol"] for u in stock_data.universe()]
    if args.limit:
        syms = syms[:args.limit]
    print("collecting signals...", flush=True)
    by_day = collect_signals(syms)
    print(f"{sum(len(v) for v in by_day.values()):,} signals across {len(by_day):,} days\n")

    results = [simulate(by_day, s) for s in SLIPPAGE_GRID]
    print(describe(results))

    flat = simulate(by_day, 3.3, compounding=False)
    print(f"\nnon-compounding (fixed Rs{START_CAPITAL:,.0f} sizing) at 3.3 bps: "
          f"Rs{flat['final_equity']:,.0f} ({flat['total_return_pct']:+.1f}%), "
          f"max DD {flat['max_drawdown_pct']:.1f}%")

    with open(args.out, "w") as f:
        json.dump({"compounding": results, "non_compounding_3.3bps": flat}, f, indent=2)
    print(f"\nwritten to {args.out}")
