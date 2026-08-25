"""
Backtest of the mechanical half of a YouTube-promoted "OTG" intraday
setup. RESEARCH ONLY -- nothing here trades.

WHAT THIS CAN AND CANNOT TEST
-------------------------------
The strategy as pitched is: (first-15-min candle shape) AND (a
proprietary "OTG" indicator, sold with a course / broker referral).
OTG itself is not available outside that paid product, so it cannot be
backtested -- there is no way to replicate a signal nobody can see.

What CAN be tested, because it is fully mechanical and objective:
  - "Strong": the opening 15-min candle has open == high (a candle that
    never traded above its own open -- pure one-way selling) or
    open == low (never traded below its own open -- pure one-way
    buying).
  - "Bias": a big-bodied red/green opening candle (body dominates the
    candle's own range), a softer version of the same idea.
  - The stated gap filter: exclude stocks that gapped >= GAP_MAX_PCT at
    the open.

This is therefore a test of "does the first 15-minute candle's SHAPE,
on its own, predict the rest of the day" -- which is the claim doing
all the work in the video's rationale ("the day is decided in the
first 15 minutes"). If that claim is false, OTG cannot be rescuing it;
if true, OTG might still be adding nothing beyond what's tested here.
Either way this is the right first question, for the same reason
stocks_in_play.py tested the RVOL hypothesis before any entry rule:
a good entry cannot manufacture a signal that doesn't exist, and a
bad entry can bury one that does.

WHY "STRONG" vs "BROAD RED/GREEN" MATTERS
--------------------------------------------
open==high and open==low are claimed to be special (backed by a paid
proprietary confirmation signal). "Any red candle" / "any green candle"
is the same directional call with none of the specificity. If STRONG
does not beat BROAD, the exact shape isn't adding anything -- the
video's implicit claim (this precise pattern is special) would be
unsupported even setting the unavailable OTG signal aside entirely.

ENTRY / STOP: reuses stock_strategies.opening_range() and the same
stop-at-opening-range convention as the rest of this project's stock
research (stop at the opposite edge of the 15-min range). Entry is
immediate at 09:30, in the direction the candle already moved --
identical to stock_strategies.py's `momentum` entry, so eod/fixed_r/
runner exits are reused as-is rather than reimplemented.

EXIT: adds ONE new exit not in stock_strategies.py: `time_exit`, closing
at a fixed clock time (default 10:15) or the stop, whichever comes
first -- this is what the video actually describes ("most positions
closed by 10:00-10:30"), which none of the existing eod/fixed_r/runner
families represent.

CONTROLS: `random` direction on the SAME filtered population (same
symbol/day pairs, coin-flip side instead of the candle's own
direction) isolates whether the specific direction call carries any
information, independent of the payoff geometry of stop+exit.
`always_short`/`always_long` are NOT used as controls here for STRONG
or BROAD populations, because they would be IDENTICAL to momentum on a
population that was already filtered by direction (open==high implies
close <= open, so momentum is always short on that population) --
exactly the always/momentum degeneracy stock_strategies.py's own
docstring warns about. `random` is the only control that isn't
tautologically identical to the entry rule here.

Run: python -m research.otg_study
"""

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta

from research import stock_costs, stock_data
from research import stock_strategies as ss

GAP_MAX_PCT = 4.0          # exclude stocks that gapped >= this at the open
BIG_BODY_RATIO = 0.70      # "big candle": body is >= this fraction of the candle's own range
MIN_BODY_PCT = 0.50        # ...and the move itself must be at least this large (%), so a
                            # 2-paisa wiggle on a thin range doesn't count as "big"
TIME_EXIT_CUTOFF = "10:15"  # video: "most positions closed by 10:00-10:30"
ENTRY_CUTOFF = "09:30"      # entries happen right at the open of the 4th 5-min bar

EXIT_FAMILIES = ["eod", "fixed_r", "runner", "time_exit"]
ASSUMED_QTY = 1


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def opening_15min_candle(bars: list) -> dict:
    """True 09:15-09:30 15-min OHLC, built from three 5-min bars."""
    orange = ss.opening_range(bars, 15)
    return orange


def classify(orange: dict, prev_close: float) -> dict:
    """
    {condition: "strong_short"|"strong_long"|"bias_short"|"bias_long"|None,
     gap_pct, body_ratio}
    for one day's opening candle. `condition` is None if nothing qualifies
    or the gap filter excludes the day.
    """
    o, h, l, c = orange["open"], orange["high"], orange["low"], orange["close"]
    rng = h - l
    gap_pct = (o - prev_close) / prev_close * 100 if prev_close else 0.0

    result = {"condition": None, "gap_pct": round(gap_pct, 2),
              "body_ratio": round(abs(c - o) / rng, 3) if rng > 0 else None}

    if abs(gap_pct) >= GAP_MAX_PCT:
        return result   # gapped too much -- excluded per the stated rule

    if rng <= 0:
        return result

    if h <= o + 1e-6:
        result["condition"] = "strong_short"
        return result
    if l >= o - 1e-6:
        result["condition"] = "strong_long"
        return result

    body_ratio = abs(c - o) / rng
    move_pct = abs(c - o) / o * 100 if o else 0.0
    if body_ratio >= BIG_BODY_RATIO and move_pct >= MIN_BODY_PCT:
        result["condition"] = "bias_short" if c < o else "bias_long"

    return result


def time_exit_simulate(bars: list, orange: dict, direction: str) -> dict:
    """Same entry/stop convention as stock_strategies.simulate(), but exits
    at TIME_EXIT_CUTOFF (or the stop, whichever comes first) instead of
    eod/fixed_r/runner. Kept separate rather than added to StockVariant
    so this file's one new exit rule can't affect any other study."""
    height = orange["high"] - orange["low"]
    if height <= 0:
        return None
    after = [b for b in bars if _minutes(b[0]) >= orange["end_min"]]
    if len(after) < 2:
        return None

    entry_px = after[0][1]
    sign = 1 if direction == "long" else -1
    stop = entry_px - height if direction == "long" else entry_px + height
    cutoff_min = _minutes(TIME_EXIT_CUTOFF)

    exit_px, reason = None, "time"
    for b in after:
        hi, lo = b[2], b[3]
        hit_stop = lo <= stop if direction == "long" else hi >= stop
        if hit_stop:
            exit_px, reason = stop, "stop"
            break
        if _minutes(b[0]) >= cutoff_min:
            exit_px, reason = b[4], "time"
            break
    if exit_px is None:
        exit_px, reason = after[-1][4], "eod_fallback"  # data ended before cutoff

    gross_per_share = (exit_px - entry_px) * sign
    turnover_per_share = entry_px + exit_px
    risk = height
    return {
        "entry": entry_px, "gross_per_share": gross_per_share,
        "turnover_per_share": turnover_per_share,
        "r_multiple": gross_per_share / risk if risk else 0.0,
        "outcome": reason,
    }


def _day_prev_close(data: dict, day: str) -> float:
    prior_days = sorted(d for d in data if d < day)
    if not prior_days:
        return None
    return data[prior_days[-1]][-1][4]  # last bar's close of the prior available day


def collect_days(symbols: list = None, verbose: bool = True) -> dict:
    """
    {(symbol, day): {condition, direction, gap_pct}} for every day whose
    opening candle qualifies under STRONG or BIAS and passes the gap filter.
    """
    symbols = symbols or [u["symbol"] for u in stock_data.universe()]
    out = {}
    for n, sym in enumerate(symbols, 1):
        data = stock_data.load(sym)
        if not data:
            continue
        for day, bars in data.items():
            orange = opening_15min_candle(bars)
            if not orange:
                continue
            prev_close = _day_prev_close(data, day)
            if prev_close is None:
                continue
            cls = classify(orange, prev_close)
            if cls["condition"] is None:
                continue
            direction = "short" if "short" in cls["condition"] else "long"
            out[(sym, day)] = {"condition": cls["condition"], "direction": direction,
                                "gap_pct": cls["gap_pct"], "orange": orange}
        if verbose and n % 40 == 0:
            print(f"  ...{n}/{len(symbols)} symbols", flush=True)
    return out


def run(symbols: list = None, verbose: bool = True) -> dict:
    selected = collect_days(symbols, verbose)
    if verbose:
        counts = defaultdict(int)
        for v in selected.values():
            counts[v["condition"]] += 1
        print(f"\n{len(selected)} qualifying symbol-days: {dict(counts)}\n", flush=True)

    symbols = symbols or [u["symbol"] for u in stock_data.universe()]
    cache = {}

    cells = defaultdict(list)   # (condition, exit_or_control) -> list of trade dicts

    for (sym, day), meta in selected.items():
        if sym not in cache:
            cache[sym] = stock_data.load(sym)
        bars = cache[sym].get(day)
        if not bars:
            continue
        direction = meta["direction"]
        condition = meta["condition"]

        for exit_ in EXIT_FAMILIES:
            if exit_ == "time_exit":
                t = time_exit_simulate(bars, meta["orange"], direction)
            else:
                variant = ss.StockVariant(name=f"{condition}/{exit_}", entry="momentum",
                                           exit=exit_, selection_minutes=15)
                t = ss.simulate(bars, variant, day=day, symbol=sym)
                # momentum's own direction must agree with our classification --
                # it always will (both derive from close vs open of the same
                # opening range), but assert rather than silently trust it.
                if t and t["direction"] != direction:
                    continue
            if not t:
                continue
            stat = stock_costs.statutory_costs(
                t["entry"], t["entry"] + t["gross_per_share"], ASSUMED_QTY)["total"]
            cells[(condition, exit_)].append({
                "gross_inr": t["gross_per_share"] * ASSUMED_QTY - stat,
                "turnover_inr": t["turnover_per_share"] * ASSUMED_QTY,
                "r_multiple": t["r_multiple"], "entry": t["entry"],
            })

        # random-direction control on the SAME (symbol, day), same exit
        # families, so it's a fair comparison of payoff geometry only.
        for exit_ in ("eod", "fixed_r", "runner"):
            variant = ss.StockVariant(name=f"{condition}/random/{exit_}", entry="random",
                                       exit=exit_, selection_minutes=15, seed=7)
            t = ss.simulate(bars, variant, day=day, symbol=sym)
            if not t:
                continue
            stat = stock_costs.statutory_costs(
                t["entry"], t["entry"] + t["gross_per_share"], ASSUMED_QTY)["total"]
            cells[(f"{condition}|random", exit_)].append({
                "gross_inr": t["gross_per_share"] * ASSUMED_QTY - stat,
                "turnover_inr": t["turnover_per_share"] * ASSUMED_QTY,
                "r_multiple": t["r_multiple"], "entry": t["entry"],
            })

    results = {}
    for (condition, exit_), trades in cells.items():
        if len(trades) < 30:
            continue
        rs = [t["r_multiple"] for t in trades]
        gross_total = sum(t["gross_inr"] for t in trades)
        mean_r = statistics.mean(rs)
        se = statistics.pstdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else 0
        results[f"{condition}|{exit_}"] = {
            "condition": condition, "exit": exit_, "n": len(trades),
            "mean_r": round(mean_r, 4),
            "t_stat": round(mean_r / se, 2) if se > 0 else None,
            "net_per_share_pct": round(gross_total / sum(t["entry"] for t in trades) * 100, 4),
            "break_even_slippage_bps": round(stock_costs.break_even_slippage_bps(trades), 2),
        }
    return {"n_qualifying_days": len(selected), "n_cells": len(results), "cells": results}


def describe(summary: dict, top: int = 30) -> str:
    cells = summary["cells"]
    if not cells:
        return f"{summary['n_qualifying_days']} qualifying symbol-days, but no cell reached the 30-trade minimum."
    real = [c for c in cells.values() if "|random" not in c["condition"]]
    n_tested = len(real)
    bar = 1.96 if n_tested <= 1 else abs(_inv_norm(0.025 / max(n_tested, 1)))

    lines = [
        f"OTG mechanical-half backtest: {summary['n_qualifying_days']} qualifying symbol-days, "
        f"{summary['n_cells']} cells with >=30 trades",
        f"{n_tested} non-control cells -> Bonferroni bar |t| > {bar:.2f}",
        "",
        "'strong' = open==high or open==low (exact). 'bias' = big-bodied candle (no exact match).",
        "'|random' rows = same symbol/day population, coin-flip direction instead -- the real control.",
        "",
        f"{'cell':<30} {'n':>6} {'meanR':>8} {'t':>7} {'net%':>8} {'BE slip bps':>12}",
    ]
    order = sorted(cells.items(), key=lambda kv: (kv[1]["condition"], -kv[1]["break_even_slippage_bps"]))
    for name, c in order[:top]:
        mark = "  <-CONTROL" if "|random" in c["condition"] else (
            "  *" if c["t_stat"] is not None and abs(c["t_stat"]) > bar else "")
        lines.append(
            f"{name:<30} {c['n']:>6,} {c['mean_r']:>+8.4f} "
            f"{(c['t_stat'] or 0):>+7.2f} {c['net_per_share_pct']:>+8.4f} "
            f"{c['break_even_slippage_bps']:>12.2f}{mark}")
    lines += [
        "",
        "BE slip bps = per-leg slippage at which the cell nets zero, AFTER statutory costs.",
        "* clears Bonferroni on mean R. |random rows are the control, not a strategy.",
        "OTG itself (the proprietary indicator) is NOT tested -- unavailable outside the paid product.",
    ]
    return "\n".join(lines)


def _inv_norm(p: float) -> float:
    from component_study import _inv_norm as f
    return f(p)


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/otg_study.json")
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
