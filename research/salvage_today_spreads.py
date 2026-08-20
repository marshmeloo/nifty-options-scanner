"""
Recovers a day's selected-name spread measurement when the 09:30 quote
sample is missing. RESEARCH ONLY.

Built for 2026-08-20, when a stale access token killed the spread
recorder until 09:41 -- past the window the whole exercise exists to
measure. The selection is NOT lost, only its timing: today's own
09:15-09:30 candles are fetchable after the fact, so the exact set of
names the strategy would have picked can be rebuilt, then matched
against the earliest spread sample that DOES exist.

WHAT THIS COSTS IN ACCURACY, STATED UP FRONT: the spreads come from the
first available sample (09:41 on 2026-08-20), not from 09:30. Spreads
are widest at the open and tighten through the session, so a 09:41
reading UNDERSTATES what a 09:30 entry pays. Whatever number this
produces is therefore a floor, not the answer -- treat it as "no worse
than this" and let the properly-timed run settle it.

Run: python -m research.salvage_today_spreads [--day YYYY-MM-DD]
"""

import argparse
import json
import statistics
from datetime import date

from research import selected_name_spreads as sns
from research import stock_data, stock_spread_recorder as rec


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    day = args.day or date.today().isoformat()

    rows = rec.load(day)
    if not rows:
        print(f"no spread recording for {day}")
        return
    snap = rows[0]     # EARLIEST available sample, whatever time it is
    print(f"{day}: {len(rows)} samples, earliest {snap['t']} -- using that for spreads")
    print(f"rebuilding the 09:30 selection from candles ({len(stock_data.universe())} symbols, "
          f"~12 min at the shared rate limit)...", flush=True)

    sel = sns.selection_from_candles(day)
    print(f"  computed RVOL/open-move for {len(sel)} symbols", flush=True)

    by_sid = {u["security_id"]: u["symbol"] for u in stock_data.universe()}
    spreads = {}
    for sid_s, q in snap["q"].items():
        sym = by_sid.get(int(sid_s))
        s = rec.spread_bps(q["b"], q["a"])
        if sym and s is not None:
            spreads[sym] = s

    selected = [
        {"symbol": sym, "spread_bps": spreads[sym], "rvol": round(v["rvol"], 2),
         "open_move_pct": round(v["open_move_pct"], 2)}
        for sym, v in sel.items()
        if sym in spreads
        and v["rvol"] >= sns.RVOL_THRESHOLD
        and v["open_move_pct"] <= sns.MIN_DOWN_MOVE_PCT
    ]
    universe = sorted(spreads.values())

    def prof(vals):
        if not vals:
            return None
        v = sorted(vals)
        return (len(v), statistics.median(v), v[int(len(v) * .75)],
                v[int(len(v) * .90)], v[-1],
                100 * sum(1 for x in v if x > sns.BREAK_EVEN_BPS) / len(v))

    print()
    print(f"TRUE 09:30 selection, spreads measured at {snap['t']} (a FLOOR -- see module docstring)")
    print(f"break-even to clear: {sns.BREAK_EVEN_BPS} bps per leg")
    print()
    print(f"{'population':<34} {'n':>4} {'med':>7} {'p75':>7} {'p90':>7} {'max':>8} {'>BE':>7}")
    u = prof(universe)
    print(f"{'universe':<34} {u[0]:>4} {u[1]:>7.2f} {u[2]:>7.2f} {u[3]:>7.2f} {u[4]:>8.2f} {u[5]:>6.1f}%")
    s = prof([r["spread_bps"] for r in selected])
    if s:
        print(f"{'SELECTED (true 09:30 rule)':<34} {s[0]:>4} {s[1]:>7.2f} {s[2]:>7.2f} "
              f"{s[3]:>7.2f} {s[4]:>8.2f} {s[5]:>6.1f}%")
        print()
        for r in sorted(selected, key=lambda x: -x["spread_bps"]):
            flag = "  <-- OVER break-even" if r["spread_bps"] > sns.BREAK_EVEN_BPS else ""
            print(f"   {r['symbol']:<14} spread {r['spread_bps']:>6.2f} bps   "
                  f"rvol {r['rvol']:>6.2f}  move {r['open_move_pct']:>+6.2f}%{flag}")
    else:
        print(f"{'SELECTED (true 09:30 rule)':<34}    0   (no name met the rule today)")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"day": day, "spread_sample_time": snap["t"],
                       "selected": selected, "n_universe": len(universe)}, f, indent=2)
        print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
