"""
Can the early-entry gain be kept WITHOUT the look-ahead that produced
it? RESEARCH ONLY -- nothing here trades.

THE PROBLEM THIS EXISTS TO SOLVE
----------------------------------
early_trigger_sweep found that entering the moment the criteria fire
beats waiting for 09:30 by a wide margin -- BE 21.70 vs 7.58 bps,
consistent across all three years -- but ONLY on the "matched"
population: days that also still qualified under the fixed 09:30 rule.

That population is not tradeable. Whether a stock will still qualify at
09:30 is not knowable at 09:20; selecting on it is look-ahead, and the
21.70 figure is therefore an UPPER BOUND on what a real rule could
achieve, not a result. Taking every early trigger instead -- which IS
tradeable -- gives BE 5.33, WORSE than simply waiting for 09:30, and
its random-direction control scores 0.00, meaning the extra days it
picks up carry no directional information at all.

So the real question is whether some filter available AT TRIGGER TIME
separates the good early triggers from the ~1,570 harmful extra ones.

WHAT IS KNOWABLE AT THE TRIGGER, AND THEREFORE ALLOWED
--------------------------------------------------------
Every filter here uses only bars up to and including the trigger bar:

  - `max_bar`     -- which bar the trigger is allowed to fire on. Note
                     that a trigger at bar 3+ CANNOT be "matched" by
                     construction (if it had qualified at bar 2 it
                     would have fired at bar 2 or earlier), so
                     restricting to bars 0-2 removes 849 structurally
                     unmatched days without any look-ahead at all.
                     This is the cheapest honest filter available.
  - `rvol_min`    -- a higher volume bar than the base 3.0.
  - `move_max`    -- a deeper decline than the base -1%.
  - `low_close`   -- the trigger bar closes in the bottom third of its
                     own range, i.e. it is still being sold into the
                     bar's close rather than bouncing off the low.
  - `confirm`     -- wait ONE more bar and require it to close below the
                     trigger bar's close before entering. Costs five
                     minutes of the edge the whole exercise is trying to
                     capture, which is exactly the trade-off worth
                     measuring rather than assuming.

Entry is always the OPEN of the bar after the last bar used for the
decision -- a price that existed, after information that was available.

THE BAR FOR CALLING ANYTHING AN IMPROVEMENT
---------------------------------------------
Same as selection_window_sweep, and for the same multiple-comparison
reason -- this sweeps ~15 variants on the dataset that already produced
the finding:

  beat fixed-09:30 in EVERY independent year, not merely pooled,
  and beat its own random control on its own population.

A variant that wins pooled but loses a year is curve-fit and is
reported as such.

Run: python -m research.hybrid_trigger_sweep
"""

import argparse
import json
import math
import random as _random
import statistics
from collections import defaultdict

from research import stock_costs, stock_data

SESSION_OPEN_MIN = 9 * 60 + 15
RVOL_LOOKBACK_DAYS = 20
MIN_LOOKBACK_DAYS = 10
FIXED_RULE_BAR = 2
TARGET_R = 2.0
ASSUMED_QTY = 1
MIN_TRADES = 100

# name -> filter spec. All thresholds are evaluated on bars <= trigger bar.
VARIANTS = {
    "base_any_bar":      dict(max_bar=9, rvol_min=3.0, move_max=-1.0),
    "bars02":            dict(max_bar=2, rvol_min=3.0, move_max=-1.0),
    "bars02_rvol4":      dict(max_bar=2, rvol_min=4.0, move_max=-1.0),
    "bars02_rvol5":      dict(max_bar=2, rvol_min=5.0, move_max=-1.0),
    "bars02_rvol6":      dict(max_bar=2, rvol_min=6.0, move_max=-1.0),
    "bars02_move15":     dict(max_bar=2, rvol_min=3.0, move_max=-1.5),
    "bars02_move20":     dict(max_bar=2, rvol_min=3.0, move_max=-2.0),
    "bars02_lowclose":   dict(max_bar=2, rvol_min=3.0, move_max=-1.0, low_close=True),
    "bars02_confirm":    dict(max_bar=2, rvol_min=3.0, move_max=-1.0, confirm=True),
    "bars01":            dict(max_bar=1, rvol_min=3.0, move_max=-1.0),
    "bar0_only":         dict(max_bar=0, rvol_min=3.0, move_max=-1.0),
    "bars02_rvol5_low":  dict(max_bar=2, rvol_min=5.0, move_max=-1.0, low_close=True),
    "bars02_rvol4_m15":  dict(max_bar=2, rvol_min=4.0, move_max=-1.5),
    "bars02_rvol5_conf": dict(max_bar=2, rvol_min=5.0, move_max=-1.0, confirm=True),
}


def cumulative_volumes(bars: list, upto: int) -> list:
    out, run = [], 0.0
    for b in bars[:upto + 1]:
        run += (b[5] or 0)
        out.append(run)
    return out


def simulate_from(bars, entry_i, direction, risk, entry_px):
    if risk <= 0 or entry_i >= len(bars):
        return None
    sign = 1 if direction == "long" else -1
    stop = entry_px - sign * risk
    target = entry_px + sign * risk * TARGET_R
    for b in bars[entry_i:]:
        hi, lo = b[2], b[3]
        if (lo <= stop) if direction == "long" else (hi >= stop):
            exit_px = stop
            break
        if (hi >= target) if direction == "long" else (lo <= target):
            exit_px = target
            break
    else:
        exit_px = bars[-1][4]
    gross = (exit_px - entry_px) * sign
    return {"entry": entry_px, "gross_per_share": gross,
            "turnover_per_share": entry_px + exit_px, "r_multiple": gross / risk}


def _pack(t):
    stat = stock_costs.statutory_costs(
        t["entry"], t["entry"] + t["gross_per_share"], ASSUMED_QTY)["total"]
    return {"gross_inr": t["gross_per_share"] * ASSUMED_QTY - stat,
            "turnover_inr": t["turnover_per_share"] * ASSUMED_QTY,
            "r_multiple": t["r_multiple"], "entry": t["entry"]}


def find_trigger(bars, cums, prior, day_open, spec):
    """First bar index satisfying the spec, or None. Uses only bars <= i."""
    max_bar = min(spec["max_bar"], len(bars) - 2)
    for i in range(0, max_bar + 1):
        base_vals = [p[i] for p in prior if len(p) > i and p[i]]
        if len(base_vals) < MIN_LOOKBACK_DAYS:
            continue
        baseline = statistics.mean(base_vals)
        if baseline <= 0:
            continue
        if cums[i] / baseline < spec["rvol_min"]:
            continue
        if (bars[i][4] - day_open) / day_open * 100 > spec["move_max"]:
            continue
        if spec.get("low_close"):
            hi, lo, close = bars[i][2], bars[i][3], bars[i][4]
            if hi <= lo or (close - lo) / (hi - lo) > 1 / 3:
                continue
        return i
    return None


def run(symbols=None, verbose=True):
    symbols = symbols or [u["symbol"] for u in stock_data.universe()]
    cells = defaultdict(list)
    cells_year = defaultdict(list)
    n_days = defaultdict(int)

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
            cums = cumulative_volumes(bars, 9)
            prior = [h for h in hist[-RVOL_LOOKBACK_DAYS:] if h]
            hist.append(cums)
            if len(prior) < MIN_LOOKBACK_DAYS:
                continue
            day_open = bars[0][1]
            if not day_open:
                continue
            y = int(day[:4]) - (1 if day[5:7] < "08" else 0)

            def range_risk(upto):
                return (max(b[2] for b in bars[:upto + 1])
                        - min(b[3] for b in bars[:upto + 1]))

            for vname, spec in VARIANTS.items():
                i = find_trigger(bars, cums, prior, day_open, spec)
                if i is None:
                    continue
                if spec.get("confirm"):
                    if i + 2 >= len(bars) or bars[i + 1][4] >= bars[i][4]:
                        continue
                    e_i = i + 2
                else:
                    e_i = i + 1
                if e_i >= len(bars):
                    continue
                n_days[vname] += 1
                t = simulate_from(bars, e_i, "short", range_risk(i), bars[e_i][1])
                if t:
                    cells[vname].append(_pack(t))
                    cells_year[(vname, y)].append(_pack(t))
                if vname == "bars02":
                    d = _random.Random(f"17:{sym}:{day}").choice(["long", "short"])
                    tc = simulate_from(bars, e_i, d, range_risk(i), bars[e_i][1])
                    if tc:
                        cells["bars02_RANDOM"].append(_pack(tc))

            # fixed 09:30 baseline
            base_vals = [p[FIXED_RULE_BAR] for p in prior
                         if len(p) > FIXED_RULE_BAR and p[FIXED_RULE_BAR]]
            if len(base_vals) >= MIN_LOOKBACK_DAYS and len(bars) > FIXED_RULE_BAR + 1:
                baseline = statistics.mean(base_vals)
                if baseline > 0:
                    rv = cums[FIXED_RULE_BAR] / baseline
                    mv = (bars[FIXED_RULE_BAR][4] - day_open) / day_open * 100
                    if rv >= 3.0 and mv <= -1.0:
                        e_i = FIXED_RULE_BAR + 1
                        t = simulate_from(bars, e_i, "short",
                                          range_risk(FIXED_RULE_BAR), bars[e_i][1])
                        if t:
                            cells["FIXED_0930"].append(_pack(t))
                            cells_year[("FIXED_0930", y)].append(_pack(t))
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

    pooled = {k: v for k, v in ((k, summarise(t)) for k, t in cells.items()) if v}
    per_year = {f"{k[0]}|{k[1]}": {"variant": k[0], "year": k[1], **v}
                for k, v in ((k, summarise(t)) for k, t in cells_year.items()) if v}
    return {"pooled": pooled, "per_year": per_year}


def describe(res):
    pooled, per_year = res["pooled"], res["per_year"]
    real = [k for k in pooled if k not in ("FIXED_0930", "bars02_RANDOM")]
    bar = abs(_inv_norm(0.025 / max(len(real), 1))) if len(real) > 1 else 1.96
    base = pooled.get("FIXED_0930")

    lines = [
        "Hybrid: keep the early entry, filter out the harmful extra days",
        "all filters use ONLY bars up to the trigger -- no look-ahead",
        f"{len(real)} variants -> Bonferroni bar |t| > {bar:.2f}",
        "",
        f"baseline FIXED_0930: n={base['n']:,} meanR {base['mean_r']:+.4f} "
        f"BE {base['break_even_slippage_bps']:.2f} bps" if base else "",
        "",
        f"{'variant':<22} {'n':>7} {'meanR':>9} {'t':>7} {'BE slip':>9}  vs fixed",
    ]
    for k in sorted(pooled, key=lambda k: -pooled[k]["break_even_slippage_bps"]):
        c = pooled[k]
        delta = (f"{c['break_even_slippage_bps'] - base['break_even_slippage_bps']:+.2f}"
                 if base and k != "FIXED_0930" else "")
        tag = "  <-CONTROL" if k == "bars02_RANDOM" else (
            "  <-BASELINE" if k == "FIXED_0930" else
            ("  *" if c["t_stat"] and abs(c["t_stat"]) > bar else ""))
        lines.append(f"{k:<22} {c['n']:>7,} {c['mean_r']:>+9.4f} "
                     f"{(c['t_stat'] or 0):>+7.2f} {c['break_even_slippage_bps']:>9.2f}  "
                     f"{delta:>7}{tag}")

    years = sorted({c["year"] for c in per_year.values()})
    lines += ["", "PER-YEAR (the bar that matters -- must beat fixed in EVERY year):",
              f"{'variant':<22} " + " ".join(f"{y:>14}" for y in years)]
    order = ["FIXED_0930"] + sorted(real, key=lambda k: -pooled[k]["break_even_slippage_bps"])
    for k in order:
        row = []
        for y in years:
            c = per_year.get(f"{k}|{y}")
            row.append(f"BE{c['break_even_slippage_bps']:>6.1f}" if c else f"{'--':>14}")
        lines.append(f"{k:<22} " + " ".join(f"{x:>14}" for x in row))

    lines += [
        "",
        "Early entry pays a WIDER spread (measured ~4.1 bps at 09:20 vs ~3.3 at 09:30),",
        "so a variant needs roughly +1 bps over fixed before it is genuinely ahead.",
        "* clears Bonferroni. CONTROL/BASELINE rows are not strategies.",
    ]
    return "\n".join(lines)


def _inv_norm(p):
    from component_study import _inv_norm as f
    return f(p)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/hybrid_trigger_sweep.json")
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
