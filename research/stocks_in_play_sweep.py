"""
Full sweep: selection x entry x exit, cost-gated, over the backfilled
Indian stock history. RESEARCH ONLY.

THE HEADLINE NUMBER IS BREAK-EVEN SLIPPAGE, NOT PROFIT
--------------------------------------------------------
Gross profitability is the easy part and is nearly meaningless on its
own here. The failure mode this whole track was scoped against is that
the edge lives inside the bid-ask spread -- which is exactly what an
independent replication found for the published US version, on QQQ, one
of the most liquid instruments in the world.

So every cell reports the per-leg slippage in basis points at which it
nets zero. Read it against what the selected names plausibly trade at.
A cell whose break-even slippage is comparable to a realistic spread is
dead however good its gross figures look, and no amount of further
parameter tuning fixes that.

Statutory costs ARE deducted (research/stock_costs.py). Slippage is not
-- it is the free variable being solved for.

WHAT IS SWEPT, AND WHAT IS HELD FIXED
--------------------------------------
Swept: RVOL threshold (the reference system used 1.2, the US literature
2.0+, so it is swept rather than assumed), direction filter, entry rule,
exit family. Held fixed: the 15-minute selection window and the
stop-at-opening-range-height, because sweeping everything at once
multiplies the comparison count into meaninglessness. If a cell looks
alive, those become the next thing to vary, not the first.

CONTROLS ARE IN THE SWEEP ON PURPOSE. `always_long`, `always_short` and
`random` run alongside the real rules so a positive result can be
attributed. Two alternative explanations have to be ruled out before
any cell counts as a finding:

  - "it is just the intraday short bias" -- the index ORB study found a
    large one that beat every real ORB variant, so always_short is a
    live rival hypothesis, not a formality.
  - "it is just the payoff geometry" -- a stop plus an end-of-day exit
    produces positive mean R with no directional information at all,
    which `random` measures directly.

A cell that does not beat its own controls is not a strategy.

Run: python -m research.stocks_in_play_sweep
"""

import argparse
import json
import math
import statistics
from collections import defaultdict

from research import stock_costs, stock_data, stock_strategies as ss, stocks_in_play as sip

# Swept. 1.2 is the value the reference signal system uses; 2.0/3.0 are
# closer to the US literature's "stocks in play" bar.
RVOL_THRESHOLDS = [1.2, 2.0, 3.0]
DIRECTIONS = ["gainers", "losers"]
ENTRIES = ["momentum", "orb", "pullback", "always_long", "always_short", "random"]
EXITS = ["eod", "fixed_r", "runner"]

MIN_OPEN_MOVE_PCT = 1.0    # a "gainer"/"loser" must have moved this much by selection
ASSUMED_QTY = 1            # per-share figures; position sizing is a later question


def _variant(entry, exit_) -> ss.StockVariant:
    return ss.StockVariant(name=f"{entry}/{exit_}", entry=entry, exit=exit_,
                           selection_minutes=sip.SELECTION_MINUTES)


def run(symbols: list = None, rvol_thresholds=None, verbose=True) -> dict:
    symbols = symbols or [u["symbol"] for u in stock_data.universe()]
    rvol_thresholds = rvol_thresholds or RVOL_THRESHOLDS

    # cell key -> list of per-trade dicts
    cells = defaultdict(list)

    for n, sym in enumerate(symbols, 1):
        data = stock_data.load(sym)
        if not data:
            continue
        rows = sip.day_rows(sym, data)
        for r in rows:
            day = r["day"]
            bars = data[day]
            for thr in rvol_thresholds:
                if r["rvol"] < thr:
                    continue
                if r["open_ret_pct"] >= MIN_OPEN_MOVE_PCT:
                    direction_bucket = "gainers"
                elif r["open_ret_pct"] <= -MIN_OPEN_MOVE_PCT:
                    direction_bucket = "losers"
                else:
                    continue
                for entry in ENTRIES:
                    for exit_ in EXITS:
                        t = ss.simulate(bars, _variant(entry, exit_), day=day, symbol=sym)
                        if not t:
                            continue
                        gross = t["gross_per_share"] * ASSUMED_QTY
                        turnover = t["turnover_per_share"] * ASSUMED_QTY
                        stat = stock_costs.statutory_costs(
                            t["entry"], t["entry"] + t["gross_per_share"], ASSUMED_QTY)["total"]
                        cells[(thr, direction_bucket, entry, exit_)].append({
                            "gross_inr": gross - stat,
                            "turnover_inr": turnover,
                            "r_multiple": t["r_multiple"],
                            "entry": t["entry"],
                        })
        if verbose and n % 40 == 0:
            print(f"  ...{n}/{len(symbols)} symbols", flush=True)

    results = {}
    for key, trades in cells.items():
        thr, bucket, entry, exit_ = key
        if len(trades) < 100:
            continue
        rs = [t["r_multiple"] for t in trades]
        gross_total = sum(t["gross_inr"] for t in trades)
        turnover = sum(t["turnover_inr"] for t in trades)
        mean_r = statistics.mean(rs)
        se = statistics.pstdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else 0
        results[f"rvol{thr}|{bucket}|{entry}|{exit_}"] = {
            "rvol_threshold": thr, "direction": bucket, "entry": entry, "exit": exit_,
            "n": len(trades),
            "mean_r": round(mean_r, 4),
            "t_stat": round(mean_r / se, 2) if se > 0 else None,
            "net_per_share_pct": round(gross_total / sum(t["entry"] for t in trades) * 100, 4),
            "break_even_slippage_bps": round(stock_costs.break_even_slippage_bps(trades), 2),
        }
    return {"n_cells": len(results), "cells": results}


def describe(summary: dict, top: int = 25) -> str:
    cells = summary["cells"]
    if not cells:
        return "No cell reached the 100-trade minimum."
    n_tested = len([c for c in cells.values() if c["entry"] not in ("always_long", "always_short", "random")])
    bar = 1.96 if n_tested <= 1 else abs(_inv_norm(0.025 / max(n_tested, 1)))

    lines = [
        f"Stocks-in-play sweep: {summary['n_cells']} cells with >=100 trades",
        f"{n_tested} non-control cells -> Bonferroni bar |t| > {bar:.2f}",
        "",
        f"{'cell':<44} {'n':>7} {'meanR':>8} {'t':>7} {'net%':>8} {'BE slip bps':>12}",
    ]
    order = sorted(cells.items(), key=lambda kv: -kv[1]["break_even_slippage_bps"])
    for name, c in order[:top]:
        mark = ""
        if c["entry"] in ("always_long", "always_short", "random"):
            mark = "  <-CONTROL"
        elif c["t_stat"] is not None and abs(c["t_stat"]) > bar:
            mark = "  *"
        lines.append(
            f"{name:<44} {c['n']:>7,} {c['mean_r']:>+8.4f} "
            f"{(c['t_stat'] or 0):>+7.2f} {c['net_per_share_pct']:>+8.4f} "
            f"{c['break_even_slippage_bps']:>12.2f}{mark}")
    lines += [
        "",
        "BE slip bps = per-leg slippage at which the cell nets zero, AFTER statutory costs.",
        "Judge it against the spread these names actually trade at. Comparable = dead.",
        "* clears Bonferroni on mean R. CONTROL rows are not strategies.",
    ]
    return "\n".join(lines)


def _inv_norm(p: float) -> float:
    from component_study import _inv_norm as f
    return f(p)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/stocks_in_play_sweep.json")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    syms = [u["symbol"] for u in stock_data.universe()]
    if args.limit:
        syms = syms[:args.limit]
    summary = run(syms)
    print()
    print(describe(summary))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")
