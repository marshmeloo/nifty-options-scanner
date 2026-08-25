"""
Event-driven entry: fire the MOMENT both conditions are true, instead
of waiting for a fixed clock time. RESEARCH ONLY -- nothing here trades.

THE QUESTION, AND WHY IT IS NOT THE SAME AS selection_window_sweep
-------------------------------------------------------------------
selection_window_sweep tested fixed CLOCK windows: every stock is
judged at 09:20, or every stock at 09:30, and so on. This tests
something different -- a per-stock TRIGGER. Watch each name bar by
bar; the first bar where (RVOL >= threshold AND move <= -1%) is true,
enter on the next bar. Different stocks therefore enter at different
times, and a fast mover is entered long before a slow one.

The motivating observation, from DIXON on 2026-08-24: the rule asks for
a 1% decline, but by the time the fixed 09:30 window closed the stock
was down 3.18%. It crossed -1% inside the FIRST five-minute bar. The
fixed clock made the rule wait ~13 minutes after its own criterion was
already satisfied, and entered at 14,535 instead of the ~14,692
available at 09:20 -- on a day whose entire move was over by then.

WHY THE -1% LEVEL ITSELF CANNOT BE THE ENTRY PRICE
----------------------------------------------------
The tempting version is "enter exactly at -1%". That is not honestly
backtestable here and is not implemented:

  - RVOL needs at least one completed bar to compute, so nothing can be
    confirmed before 09:20. By then a fast mover is already well past
    -1% (DIXON was at -2.06%). Entering at the -1% price would be
    filling at a level that had already gone, i.e. look-ahead.
  - Data here is 5-minute OHLC. Within a bar the path is unknown -- the
    same intrabar ambiguity orb.py and stock_strategies.py already
    resolve conservatively. Assuming a -1% touch filled would assume
    the path.

So entry is the OPEN OF THE BAR AFTER confirmation: a price that
actually existed, after information that was actually available. This
is deliberately the pessimistic reading of the idea.

WHAT IS COMPARED
----------------
Two populations are reported, because they answer different things:

  - ALL triggered days: the strategy as it would actually run.
  - MATCHED days: only days that ALSO qualify under the current fixed
    09:30 rule, entered both ways. This isolates ENTRY TIMING from
    selection -- same stock, same day, earlier entry vs 09:30 entry --
    and is the comparison that actually answers the question.

Controls (random direction at the same trigger bar) run on the same
population, for the same reason they do everywhere else here: a stop
plus a target produces positive mean R with no directional information
at all, and that has to be measurable rather than assumed away.

Run: python -m research.early_trigger_sweep
"""

import argparse
import json
import math
import statistics
from collections import defaultdict

from research import stock_costs, stock_data

SESSION_OPEN_MIN = 9 * 60 + 15
RVOL_LOOKBACK_DAYS = 20
MIN_LOOKBACK_DAYS = 10
RVOL_THRESHOLD = 3.0
MIN_DOWN_MOVE_PCT = -1.0
MAX_TRIGGER_BAR = 9          # stop looking after this many bars (~10:00)
FIXED_RULE_BAR = 2           # the current rule: bars 0..2 = 09:15-09:30
TARGET_R = 2.0
ASSUMED_QTY = 1
MIN_TRADES = 100


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def cumulative_volumes(bars: list, upto: int) -> list:
    """[cum vol through bar 0, through bar 1, ...] for the first `upto`+1 bars."""
    out, run = [], 0.0
    for b in bars[:upto + 1]:
        run += (b[5] or 0)
        out.append(run)
    return out


def simulate_from(bars: list, entry_i: int, direction: str, risk: float,
                  entry_px: float) -> dict:
    """Fixed-R exit from `entry_i`, same conventions as stock_strategies:
    stop assumed on any bar that could have hit both stop and target."""
    if risk <= 0 or entry_i >= len(bars):
        return None
    sign = 1 if direction == "long" else -1
    stop = entry_px - sign * risk
    target = entry_px + sign * risk * TARGET_R

    for b in bars[entry_i:]:
        hi, lo = b[2], b[3]
        hit_stop = lo <= stop if direction == "long" else hi >= stop
        hit_t = hi >= target if direction == "long" else lo <= target
        if hit_stop:
            exit_px, reason = stop, "stop"
            break
        if hit_t:
            exit_px, reason = target, "target"
            break
    else:
        exit_px, reason = bars[-1][4], "eod"

    gross = (exit_px - entry_px) * sign
    return {"entry": entry_px, "gross_per_share": gross,
            "turnover_per_share": entry_px + exit_px,
            "r_multiple": gross / risk, "outcome": reason}


def _pack(t: dict) -> dict:
    stat = stock_costs.statutory_costs(
        t["entry"], t["entry"] + t["gross_per_share"], ASSUMED_QTY)["total"]
    return {"gross_inr": t["gross_per_share"] * ASSUMED_QTY - stat,
            "turnover_inr": t["turnover_per_share"] * ASSUMED_QTY,
            "r_multiple": t["r_multiple"], "entry": t["entry"]}


def run(symbols: list = None, verbose: bool = True) -> dict:
    symbols = symbols or [u["symbol"] for u in stock_data.universe()]

    cells = defaultdict(list)
    cells_year = defaultdict(list)
    trigger_bars = defaultdict(int)

    for n, sym in enumerate(symbols, 1):
        data = stock_data.load(sym)
        if not data:
            continue
        days = sorted(data)
        # per-bar-index history of cumulative volume, for per-index baselines
        hist = []          # list of (day, [cumvol by bar index])

        for day in days:
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
            y = int(day[:4]) - (1 if day[5:7] < "08" else 0)

            # --- event-driven trigger: first bar where BOTH hold ---
            trig_i = None
            for i in range(0, min(MAX_TRIGGER_BAR, len(bars) - 2) + 1):
                base_vals = [p[i] for p in prior if len(p) > i and p[i]]
                if len(base_vals) < MIN_LOOKBACK_DAYS:
                    continue
                baseline = statistics.mean(base_vals)
                if baseline <= 0:
                    continue
                rvol = cums[i] / baseline
                move = (bars[i][4] - day_open) / day_open * 100
                if rvol >= RVOL_THRESHOLD and move <= MIN_DOWN_MOVE_PCT:
                    trig_i = i
                    break

            # --- the current fixed rule, for the matched comparison ---
            fixed_ok = False
            if len(bars) > FIXED_RULE_BAR + 1:
                base_vals = [p[FIXED_RULE_BAR] for p in prior
                             if len(p) > FIXED_RULE_BAR and p[FIXED_RULE_BAR]]
                if len(base_vals) >= MIN_LOOKBACK_DAYS:
                    baseline = statistics.mean(base_vals)
                    if baseline > 0:
                        rvol_f = cums[FIXED_RULE_BAR] / baseline
                        move_f = (bars[FIXED_RULE_BAR][4] - day_open) / day_open * 100
                        fixed_ok = rvol_f >= RVOL_THRESHOLD and move_f <= MIN_DOWN_MOVE_PCT

            def range_risk(upto):
                hi = max(b[2] for b in bars[:upto + 1])
                lo = min(b[3] for b in bars[:upto + 1])
                return hi - lo

            if trig_i is not None:
                trigger_bars[trig_i] += 1
                e_i = trig_i + 1
                t = simulate_from(bars, e_i, "short", range_risk(trig_i), bars[e_i][1])
                if t:
                    cells[("early", "all")].append(_pack(t))
                    cells_year[("early", "all", y)].append(_pack(t))
                    if fixed_ok:
                        cells[("early", "matched")].append(_pack(t))
                        cells_year[("early", "matched", y)].append(_pack(t))
                # control: same bar, coin-flip side
                import random as _r
                d = _r.Random(f"13:{sym}:{day}").choice(["long", "short"])
                tc = simulate_from(bars, e_i, d, range_risk(trig_i), bars[e_i][1])
                if tc:
                    cells[("early_random", "all")].append(_pack(tc))

            if fixed_ok and len(bars) > FIXED_RULE_BAR + 1:
                e_i = FIXED_RULE_BAR + 1
                t = simulate_from(bars, e_i, "short", range_risk(FIXED_RULE_BAR),
                                  bars[e_i][1])
                if t:
                    cells[("fixed0930", "all")].append(_pack(t))
                    cells_year[("fixed0930", "all", y)].append(_pack(t))
                    if trig_i is not None:
                        cells[("fixed0930", "matched")].append(_pack(t))
                        cells_year[("fixed0930", "matched", y)].append(_pack(t))

        if verbose and n % 40 == 0:
            print(f"  ...{n}/{len(symbols)} symbols", flush=True)

    def summarise(trades):
        if len(trades) < MIN_TRADES:
            return None
        rs = [t["r_multiple"] for t in trades]
        mean_r = statistics.mean(rs)
        se = statistics.pstdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else 0
        return {"n": len(trades), "mean_r": round(mean_r, 4),
                "t_stat": round(mean_r / se, 2) if se > 0 else None,
                "break_even_slippage_bps": round(
                    stock_costs.break_even_slippage_bps(trades), 2)}

    pooled = {}
    for (rule, pop), trades in cells.items():
        s = summarise(trades)
        if s:
            pooled[f"{rule}|{pop}"] = {"rule": rule, "population": pop, **s}
    per_year = {}
    for (rule, pop, y), trades in cells_year.items():
        s = summarise(trades)
        if s:
            per_year[f"{rule}|{pop}|{y}"] = {"rule": rule, "population": pop,
                                              "year": y, **s}

    return {"pooled": pooled, "per_year": per_year,
            "trigger_bar_distribution": dict(sorted(trigger_bars.items()))}


def describe(res: dict) -> str:
    pooled, per_year = res["pooled"], res["per_year"]
    lines = [
        "Event-driven entry (fire when criteria met) vs fixed 09:30 entry",
        f"selection: RVOL>={RVOL_THRESHOLD} AND move<={MIN_DOWN_MOVE_PCT}%; exit=fixed {TARGET_R}R",
        "entry = OPEN OF THE BAR AFTER confirmation (no intrabar fills assumed)",
        "",
    ]
    dist = res["trigger_bar_distribution"]
    total = sum(dist.values()) or 1
    lines.append("when the trigger actually fires:")
    for i, c in dist.items():
        clock = SESSION_OPEN_MIN + (i + 1) * 5
        lines.append(f"   bar {i} -> entry {clock//60:02d}:{clock%60:02d}   "
                     f"{c:>6,}  ({c/total*100:>5.1f}%)")
    lines += ["", f"{'cell':<28} {'n':>7} {'meanR':>9} {'t':>7} {'BE slip bps':>12}"]
    for name in sorted(pooled):
        c = pooled[name]
        mark = "  <-CONTROL" if "random" in c["rule"] else ""
        lines.append(f"{name:<28} {c['n']:>7,} {c['mean_r']:>+9.4f} "
                     f"{(c['t_stat'] or 0):>+7.2f} {c['break_even_slippage_bps']:>12.2f}{mark}")

    years = sorted({c["year"] for c in per_year.values()})
    lines += ["", "PER-YEAR, MATCHED days only (same stock+day, entry timing is the only difference):",
              f"{'rule':<14} " + " ".join(f"{y:>18}" for y in years)]
    for rule in ("early", "fixed0930"):
        row = []
        for y in years:
            c = per_year.get(f"{rule}|matched|{y}")
            row.append(f"R{c['mean_r']:+.3f} BE{c['break_even_slippage_bps']:>6.1f}"
                       if c else f"{'--':>18}")
        lines.append(f"{rule:<14} " + " ".join(f"{x:>18}" for x in row))

    lines += [
        "",
        "MATCHED is the comparison that answers the question -- same days, both entries.",
        "ALL shows what each rule would trade in practice (different populations).",
        "Spreads are WIDER earlier in the session, so an early win must exceed the",
        "extra spread it pays; a tie on this table favours the later entry.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/early_trigger_sweep.json")
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
