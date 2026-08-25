"""
One-off: rank today's full universe by RVOL (both directions), not just the
strict pass/fail selection. RESEARCH ONLY, ad hoc -- not part of the tracked
pipeline.

Exists because selected_name_spreads' strict rule (RVOL>=3, move<=-1%) found
exactly one name on 2026-08-20, and the user asked to widen that to a top-5
ranked view to get more data points per day instead of one pass/fail. Reuses
selection_from_candles' per-symbol computation but keeps every result instead
of filtering, then ranks.

Run: python -m research.top5_today [--day YYYY-MM-DD]
"""
import argparse
import json
from datetime import date

from research import stock_data, stock_spread_recorder as rec
from research.selected_name_spreads import opening_volume_baseline
from research import stocks_in_play as sip


def rank_today(day: str) -> dict:
    from research import stock_spread_recorder as _rec
    _rec.refresh_token_from_registry()

    universe = {u["symbol"]: u["security_id"] for u in stock_data.universe()}
    out = []
    failures = 0
    for i, (sym, sid) in enumerate(universe.items(), 1):
        try:
            fetched = stock_data.fetch_month(sid, date.fromisoformat(day), date.fromisoformat(day))
        except Exception as e:
            if "401" in str(e):
                raise SystemExit(f"FATAL 401 on {sym} -- stale token")
            failures += 1
            continue
        bars = fetched.get(day)
        if not bars:
            continue
        head = sip.opening_slice(bars, sip.SELECTION_MINUTES)
        if len(head) < sip.SELECTION_MINUTES // 5:
            continue
        baseline = opening_volume_baseline(sym, day)
        if baseline <= 0:
            continue
        day_open, ref = head[0][1], head[-1][4]
        if not day_open:
            continue
        rvol = sum(b[5] or 0 for b in head) / baseline
        move = (ref - day_open) / day_open * 100
        out.append({"symbol": sym, "rvol": round(rvol, 2), "move_pct": round(move, 2)})
        if i % 40 == 0:
            print(f"  ...{i}/{len(universe)}", flush=True)
    if failures:
        print(f"  ({failures} symbols failed to fetch)")
    return {"day": day, "rows": out}


def spread_for(day: str, symbol: str):
    rows = rec.load(day)
    if not rows:
        return None
    by_sym = {u["symbol"]: u["security_id"] for u in stock_data.universe()}
    sid = by_sym.get(symbol)
    if not sid:
        return None
    # use the earliest sample that actually has usable data for this symbol
    for r in rows:
        q = r["q"].get(str(sid))
        if q:
            return rec.spread_bps(q["b"], q["a"]), r["t"]
    return None


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    day = args.day or date.today().isoformat()

    print(f"rebuilding full-universe RVOL ranking for {day} (~12 min)...", flush=True)
    res = rank_today(day)
    rows = res["rows"]

    decliners = sorted([r for r in rows if r["move_pct"] < 0], key=lambda r: -r["rvol"])[:5]
    gainers = sorted([r for r in rows if r["move_pct"] > 0], key=lambda r: -r["rvol"])[:5]

    print()
    print(f"TOP 5 DECLINERS by RVOL, true 09:30 window, {day}:")
    for r in decliners:
        sp = spread_for(day, r["symbol"])
        sp_str = f"spread {sp[0]:.2f} bps @ {sp[1]}" if sp else "no spread sample"
        print(f"   {r['symbol']:<14} rvol {r['rvol']:>5.2f}  move {r['move_pct']:>+6.2f}%   {sp_str}")

    print()
    print(f"TOP 5 GAINERS by RVOL, true 09:30 window, {day}:")
    for r in gainers:
        sp = spread_for(day, r["symbol"])
        sp_str = f"spread {sp[0]:.2f} bps @ {sp[1]}" if sp else "no spread sample"
        print(f"   {r['symbol']:<14} rvol {r['rvol']:>5.2f}  move {r['move_pct']:>+6.2f}%   {sp_str}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"day": day, "decliners": decliners, "gainers": gainers}, f, indent=2)
        print(f"\nwritten to {args.out}")
