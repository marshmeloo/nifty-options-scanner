"""
Measures the bid/ask spread of the names the strategy WOULD HAVE
SELECTED, at the moment it would have entered them. RESEARCH ONLY.

THE QUESTION THIS SETTLES
--------------------------
The stocks-in-play sweep found one live cell: pullback on high-RVOL
decliners, worst-year break-even slippage 10.6 bps. Whether that is
tradeable depends entirely on the spread of the names it picks -- and
crucially NOT on the spread of the universe at large. The screen selects
on unusual volume, which preferentially surfaces the less-liquid,
wider-spread end of the universe. Quoting a universe-median spread would
therefore flatter the strategy by measuring the wrong population, which
is the subtler cousin of the mistake that sank the published US version.

So this measures the SELECTED names specifically, and reports the
universe alongside only as a contrast.

WHY 09:30 AND NOT ANY OTHER TIME
---------------------------------
The strategy's selection window ends at 09:30 and entry follows
immediately. Spreads are at their widest in the first minutes of the
session and tighten through the day, so a midday measurement would
understate the cost exactly where the strategy pays it. The analysis
therefore pins to the sample nearest 09:30 and also reports the
intraday profile, so the size of that effect is visible rather than
assumed.

HOW SELECTION IS RECONSTRUCTED FROM A QUOTE SAMPLE
---------------------------------------------------
A single /marketfeed/quote response carries cumulative session volume
and the day's open alongside the depth. At the 09:30 sample:

    RVOL      = volume-so-far / (20-day mean of first-15-min volume)
    open move = (last_price - day_open) / day_open

The baseline comes from the historical backfill. Both inputs are
therefore known at 09:30 -- no look-ahead, and no separate 208-call
candle fetch that would take twelve minutes and miss the very window it
was trying to measure.

Run: python -m research.selected_name_spreads [--day YYYY-MM-DD] [--at 09:30]
"""

import argparse
import json
import statistics
from datetime import date

from research import stock_data, stock_spread_recorder as rec, stocks_in_play as sip

RVOL_THRESHOLD = 3.0
MIN_DOWN_MOVE_PCT = -1.0
BREAK_EVEN_BPS = 10.6      # worst-year break-even from the sweep -- the bar to clear


def opening_volume_baseline(symbol: str, before_day: str) -> float:
    """Mean first-15-minute volume over the trailing 20 sessions strictly
    BEFORE `before_day`, from the historical backfill."""
    data = stock_data.load(symbol)
    if not data:
        return 0.0
    vols = []
    for day in sorted(d for d in data if d < before_day)[-sip.RVOL_LOOKBACK_DAYS:]:
        head = sip.opening_slice(data[day], sip.SELECTION_MINUTES)
        if head:
            vols.append(sum(b[5] or 0 for b in head))
    return statistics.mean(vols) if len(vols) >= sip.MIN_LOOKBACK_DAYS else 0.0


def sample_nearest(rows: list, hhmm: str):
    """The recorded sample closest to `hhmm`, or None."""
    if not rows:
        return None
    target = int(hhmm[:2]) * 60 + int(hhmm[3:5])

    def dist(r):
        return abs(int(r["t"][:2]) * 60 + int(r["t"][3:5]) - target)

    return min(rows, key=dist)


def analyse(day: str = None, at: str = "09:30") -> dict:
    day = day or date.today().isoformat()
    rows = rec.load(day)
    if not rows:
        return {"error": f"no spread recording for {day}"}

    snap = sample_nearest(rows, at)
    by_sid = {u["security_id"]: u["symbol"] for u in stock_data.universe()}

    # RVOL here is only MEANINGFUL at ~09:30. `vol` from the quote feed is
    # CUMULATIVE session volume, and the baseline is the 20-day mean of
    # the first FIFTEEN minutes. Those two only describe the same window
    # at 09:30. Read the 13:14 sample and you are dividing four hours of
    # volume by a fifteen-minute baseline, which inflates every RVOL by
    # roughly 16x and silently selects a completely different set of
    # names than the strategy ever would. Flagged rather than corrected:
    # time-scaling the baseline would produce a number that LOOKS like
    # the strategy's selection while being a different quantity, and the
    # honest answer is that this measurement belongs at 09:30.
    sample_min = int(snap["t"][:2]) * 60 + int(snap["t"][3:5])
    window_min = sample_min - (9 * 60 + 15)
    rvol_valid = abs(window_min - sip.SELECTION_MINUTES) <= 3

    universe, selected = [], []
    for sid_s, q in snap["q"].items():
        sid = int(sid_s)
        sym = by_sid.get(sid)
        spread = rec.spread_bps(q["b"], q["a"])
        if spread is None:
            continue
        universe.append({"symbol": sym, "spread_bps": spread})

        if not sym or not q.get("o") or not q.get("vol") or not q.get("ltp"):
            continue
        baseline = opening_volume_baseline(sym, day)
        if baseline <= 0:
            continue
        rvol = q["vol"] / baseline
        move = (q["ltp"] - q["o"]) / q["o"] * 100
        if rvol >= RVOL_THRESHOLD and move <= MIN_DOWN_MOVE_PCT:
            selected.append({"symbol": sym, "spread_bps": spread,
                             "rvol": round(rvol, 2), "open_move_pct": round(move, 2)})

    def profile(rows_, label):
        if not rows_:
            return {"label": label, "n": 0}
        s = sorted(r["spread_bps"] for r in rows_)
        return {
            "label": label, "n": len(s),
            "median_bps": round(statistics.median(s), 2),
            "mean_bps": round(statistics.mean(s), 2),
            "p75_bps": round(s[int(len(s) * 0.75)], 2),
            "p90_bps": round(s[int(len(s) * 0.90)], 2),
            "max_bps": round(s[-1], 2),
            "pct_over_break_even": round(
                100 * sum(1 for x in s if x > BREAK_EVEN_BPS) / len(s), 1),
        }

    return {
        "day": day, "sample_time": snap["t"], "requested_at": at,
        "rvol_valid": rvol_valid,
        "volume_window_minutes": window_min,
        "universe": profile(universe, "universe (208)"),
        "selected": profile(selected, f"SELECTED (rvol>={RVOL_THRESHOLD}, down<={MIN_DOWN_MOVE_PCT}%)"),
        "selected_names": sorted(selected, key=lambda r: -r["spread_bps"]),
        "break_even_bps": BREAK_EVEN_BPS,
    }


def describe(res: dict) -> str:
    if "error" in res:
        return res["error"]
    lines = [
        f"Selected-name spreads -- {res['day']} sample {res['sample_time']} "
        f"(wanted {res['requested_at']})",
        f"break-even to clear: {res['break_even_bps']} bps per leg",
    ]
    if not res.get("rvol_valid", True):
        lines += [
            "",
            f"*** RVOL INVALID AT THIS SAMPLE ***  cumulative volume covers "
            f"{res['volume_window_minutes']} min against a "
            f"{sip.SELECTION_MINUTES}-min baseline, inflating every RVOL by roughly "
            f"{max(res['volume_window_minutes'], 1) / sip.SELECTION_MINUTES:.1f}x.",
            "    The SELECTED set below is NOT the set the strategy would pick. Spreads",
            "    shown are real; the selection is not. Use the 09:30 sample.",
        ]
    lines += [
        "",
        f"{'population':<46} {'n':>4} {'med':>7} {'p75':>7} {'p90':>7} {'max':>8} {'>BE':>7}",
    ]
    for p in (res["universe"], res["selected"]):
        if not p["n"]:
            lines.append(f"{p['label']:<46} {'0':>4}  (none)")
            continue
        lines.append(f"{p['label']:<46} {p['n']:>4} {p['median_bps']:>7.2f} "
                     f"{p['p75_bps']:>7.2f} {p['p90_bps']:>7.2f} {p['max_bps']:>8.2f} "
                     f"{p['pct_over_break_even']:>6.1f}%")
    if res["selected_names"]:
        lines += ["", "selected names, widest first:"]
        for r in res["selected_names"][:15]:
            flag = "  <-- OVER break-even" if r["spread_bps"] > res["break_even_bps"] else ""
            lines.append(f"   {r['symbol']:<14} spread {r['spread_bps']:>6.2f} bps   "
                         f"rvol {r['rvol']:>6.2f}  move {r['open_move_pct']:>+6.2f}%{flag}")
    lines += ["", "The SELECTED row is the one that matters. Universe median is shown only "
                  "to expose how much the screen shifts the population."]
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", default=None)
    p.add_argument("--at", default="09:30")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    res = analyse(args.day, args.at)
    print(describe(res))
    if args.out and "error" not in res:
        with open(args.out, "w") as f:
            json.dump(res, f, indent=2)
        print(f"\nwritten to {args.out}")
