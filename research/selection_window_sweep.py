"""
Does the SELECTION WINDOW length change the stocks-in-play result?
RESEARCH ONLY -- nothing here trades.

THE QUESTION
------------
The surviving cell from stocks_in_play_sweep (pullback on high-RVOL
decliners) selects on the 09:15-09:30 window and enters immediately
after. That collapses selection and entry into the same moment, so
every entry is a bet that a move which just FINISHED will continue.
Watching DIXON on 2026-08-24 made the cost of that concrete: it fell
3.1% inside the opening 15 minutes, qualified, was entered at 09:30 --
and then went sideways for the rest of the session. The move was over
before the rule could see it.

Three incompatible answers exist for when selection should happen, all
defensible from reasoning alone, which is exactly why this is measured
rather than argued:

  - SHORTER (5/10 min): catch the stock while the move is still
    running, with more of the volatile opening period left to continue.
    Costs: a noisier RVOL estimate (5 minutes of volume against a
    20-day baseline is far less stable than 15), and WIDER spreads,
    since spreads are at their worst in the first ten minutes -- the
    very cost this track has been carefully measuring at 09:30.
  - CURRENT (15 min): the status quo, and the only window with a
    measured spread number behind it.
  - LONGER / DELAYED (30/45 min, i.e. entry at 09:45 or 10:00): the
    opposite hypothesis, from the reference dashboard transcript
    (2026-08-24) -- that the open is noise and the tradeable signal
    only appears once it settles.

WHY THIS IS NOT JUST ANOTHER PARAMETER SWEEP
----------------------------------------------
It is one, and that is the risk. Sweeping windows on the SAME dataset
that already produced the surviving cell raises multiple-comparison
risk directly: some window will look best by chance alone. So the bar
here is deliberately higher than "highest break-even slippage wins":

  A different window is only interesting if it beats 15-min in EVERY
  independent year, not merely in the pooled total. A window that wins
  overall but loses in one of three years is curve-fit, and is reported
  as such.

Per-year figures are therefore computed and printed alongside the
pooled ones, not as an afterthought.

The RVOL baseline is rebuilt PER WINDOW -- a 5-minute window is
compared against the 20-day mean of prior 5-minute volumes, never
against the 15-minute baseline. Mixing those is the same class of
error as reading a 13:14 quote sample against a 15-minute baseline
(see selected_name_spreads.py), and would inflate or deflate every
RVOL by a constant factor, silently selecting a different population.

Run: python -m research.selection_window_sweep
"""

import argparse
import json
import math
import statistics
from collections import defaultdict

from research import stock_costs, stock_data
from research import stock_strategies as ss

SESSION_OPEN_MIN = 9 * 60 + 15

# Selection window in minutes -> entry happens at the first bar after it.
# 5 -> enter 09:20, 15 -> enter 09:30 (current), 45 -> enter 10:00.
WINDOWS = [5, 10, 15, 30, 45]

RVOL_LOOKBACK_DAYS = 20
MIN_LOOKBACK_DAYS = 10
RVOL_THRESHOLD = 3.0
MIN_DOWN_MOVE_PCT = -1.0     # decliners only -- the surviving cell's direction
ENTRIES = ["pullback", "orb", "always_short", "random"]
EXIT = "fixed_r"             # the exit the surviving cell used
MIN_TRADES = 100
ASSUMED_QTY = 1


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def opening_slice(bars: list, minutes: int) -> list:
    end = SESSION_OPEN_MIN + minutes
    return [b for b in bars if SESSION_OPEN_MIN <= _minutes(b[0]) < end]


def selections_for_window(symbol: str, data: dict, window: int) -> list:
    """
    Days where this symbol qualified under (RVOL >= threshold AND
    down >= MIN_DOWN_MOVE_PCT) measured over `window` minutes from the
    open. Baseline is rebuilt from the SAME window length on prior days.
    Look-ahead-safe: only prior days feed the baseline, only bars inside
    the window feed the numerator.
    """
    days = sorted(data)
    out = []
    hist_open_vol = []

    for day in days:
        bars = data[day]
        if len(bars) < 40:
            hist_open_vol.append(None)
            continue
        head = opening_slice(bars, window)
        if len(head) < max(1, window // 5):
            hist_open_vol.append(None)
            continue

        open_vol = sum(b[5] or 0 for b in head)
        prior = [v for v in hist_open_vol[-RVOL_LOOKBACK_DAYS:] if v]
        hist_open_vol.append(open_vol)
        if len(prior) < MIN_LOOKBACK_DAYS:
            continue
        baseline = statistics.mean(prior)
        if baseline <= 0:
            continue

        day_open, ref = head[0][1], head[-1][4]
        if not day_open or not ref:
            continue

        rvol = open_vol / baseline
        move = (ref - day_open) / day_open * 100
        if rvol >= RVOL_THRESHOLD and move <= MIN_DOWN_MOVE_PCT:
            out.append({"day": day, "rvol": rvol, "move": move})
    return out


def run(symbols: list = None, verbose: bool = True) -> dict:
    symbols = symbols or [u["symbol"] for u in stock_data.universe()]
    # (window, entry) -> list of trades ; and per-year variant
    cells = defaultdict(list)
    cells_year = defaultdict(list)
    n_selected = defaultdict(int)

    for n, sym in enumerate(symbols, 1):
        data = stock_data.load(sym)
        if not data:
            continue
        for window in WINDOWS:
            picks = selections_for_window(sym, data, window)
            n_selected[window] += len(picks)
            for p in picks:
                day = p["day"]
                bars = data[day]
                year = day[:4] if day[5:7] < "08" else f"{day[:4]}H"  # coarse; refined below
                # independent ~1-year periods aligned to the backfill start (Aug)
                y = int(day[:4]) - (1 if day[5:7] < "08" else 0)
                for entry in ENTRIES:
                    variant = ss.StockVariant(
                        name=f"w{window}/{entry}", entry=entry, exit=EXIT,
                        selection_minutes=window, seed=11)
                    t = ss.simulate(bars, variant, day=day, symbol=sym)
                    if not t:
                        continue
                    stat = stock_costs.statutory_costs(
                        t["entry"], t["entry"] + t["gross_per_share"], ASSUMED_QTY)["total"]
                    rec = {"gross_inr": t["gross_per_share"] * ASSUMED_QTY - stat,
                           "turnover_inr": t["turnover_per_share"] * ASSUMED_QTY,
                           "r_multiple": t["r_multiple"], "entry": t["entry"]}
                    cells[(window, entry)].append(rec)
                    cells_year[(window, entry, y)].append(rec)
        if verbose and n % 40 == 0:
            print(f"  ...{n}/{len(symbols)} symbols", flush=True)

    def summarise(trades):
        if len(trades) < MIN_TRADES:
            return None
        rs = [t["r_multiple"] for t in trades]
        mean_r = statistics.mean(rs)
        se = statistics.pstdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else 0
        return {
            "n": len(trades), "mean_r": round(mean_r, 4),
            "t_stat": round(mean_r / se, 2) if se > 0 else None,
            "break_even_slippage_bps": round(stock_costs.break_even_slippage_bps(trades), 2),
        }

    pooled, per_year = {}, {}
    for (window, entry), trades in cells.items():
        s = summarise(trades)
        if s:
            pooled[f"w{window}|{entry}"] = {"window": window, "entry": entry, **s}
    for (window, entry, y), trades in cells_year.items():
        s = summarise(trades)
        if s:
            per_year[f"w{window}|{entry}|{y}"] = {"window": window, "entry": entry, "year": y, **s}

    return {"pooled": pooled, "per_year": per_year,
            "n_selected_by_window": dict(n_selected)}


def describe(res: dict) -> str:
    pooled, per_year = res["pooled"], res["per_year"]
    real = [c for c in pooled.values() if c["entry"] not in ("always_short", "random")]
    bar = 1.96 if len(real) <= 1 else abs(_inv_norm(0.025 / max(len(real), 1)))

    lines = [
        "Selection-window sweep -- does entering earlier or later beat 09:30?",
        f"selection: RVOL>={RVOL_THRESHOLD} AND move<={MIN_DOWN_MOVE_PCT}% over the window; exit={EXIT}",
        f"{len(real)} non-control cells -> Bonferroni bar |t| > {bar:.2f}",
        "",
        "qualifying symbol-days by window: " +
        ", ".join(f"{w}min={n:,}" for w, n in sorted(res["n_selected_by_window"].items())),
        "",
        f"{'cell':<24} {'entry@':>7} {'n':>7} {'meanR':>9} {'t':>7} {'BE slip bps':>12}",
    ]
    for name in sorted(pooled, key=lambda k: (pooled[k]["window"], pooled[k]["entry"])):
        c = pooled[name]
        entry_at = f"{(SESSION_OPEN_MIN + c['window']) // 60:02d}:{(SESSION_OPEN_MIN + c['window']) % 60:02d}"
        mark = "  <-CONTROL" if c["entry"] in ("always_short", "random") else (
            "  *" if c["t_stat"] is not None and abs(c["t_stat"]) > bar else "")
        lines.append(f"{name:<24} {entry_at:>7} {c['n']:>7,} {c['mean_r']:>+9.4f} "
                     f"{(c['t_stat'] or 0):>+7.2f} {c['break_even_slippage_bps']:>12.2f}{mark}")

    # --- the bar that actually matters: per-year consistency, pullback only ---
    lines += ["", "PER-YEAR (pullback only) -- a window must win in EVERY year, not just pooled:",
              f"{'window':<10} " + " ".join(f"{y:>18}" for y in sorted({c['year'] for c in per_year.values()}))]
    years = sorted({c["year"] for c in per_year.values()})
    for window in WINDOWS:
        cells_ = []
        for y in years:
            c = per_year.get(f"w{window}|pullback|{y}")
            cells_.append(f"R{c['mean_r']:+.3f} BE{c['break_even_slippage_bps']:>6.1f}" if c else f"{'--':>18}")
        lines.append(f"{f'{window}min':<10} " + " ".join(f"{x:>18}" for x in cells_))

    lines += [
        "",
        "BE slip bps = per-leg slippage at which the cell nets zero, after statutory costs.",
        "Spreads WIDEN toward the open, so an early window must beat 15min by more than",
        "the extra spread it pays -- a tie on this table is a LOSS for the earlier window.",
        "* clears Bonferroni. CONTROL rows are not strategies.",
    ]
    return "\n".join(lines)


def _inv_norm(p: float) -> float:
    from component_study import _inv_norm as f
    return f(p)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/selection_window_sweep.json")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    syms = [u["symbol"] for u in stock_data.universe()]
    if args.limit:
        syms = syms[:args.limit]
    res = run(syms)
    print()
    print(describe(res))
    with open(args.out, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\nwritten to {args.out}")
