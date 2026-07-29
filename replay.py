"""
Replay recorded market history through the CURRENT decision logic.

WHAT THIS IS FOR
----------------
Answering "did that change help?" without waiting months. Change the
scorer, replay both versions over identical recorded cycles, and diff the
decisions: you see exactly which trades appear, vanish, or change entry.

WHAT IT HONESTLY CANNOT DO
--------------------------
This is NOT a backtest that validates an edge, and results from it must
never be presented as one. Replaying the same few recorded days while
tweaking logic is textbook overfitting -- with enough variants something
will always look good on two sessions.

What replay legitimately provides:
  - REGRESSION SAFETY: did this change alter decisions I didn't expect?
  - CHEAP REJECTION: a variant that is worse on recorded history is
    unlikely to be better live, so kill it now rather than in three weeks.
  - COUNTERFACTUALS: what would the candidates we REJECTED have done
    (see forward_returns below)?

Confirming an edge still requires out-of-sample forward sessions. Replay
makes rejection fast; only forward data confirms.

A FURTHER CAVEAT
----------------
Replay reproduces the decision path, not execution. Fills are still
modelled at recorded LTP, so replayed P&L inherits the same optimistic
bias as the live journal until bid/ask and costs land (audit items 1-2).
Treat replayed P&L as an upper bound.

USAGE
-----
    python3 replay.py --list
    python3 replay.py --day 2026-07-28
    python3 replay.py --day 2026-07-28 --save baseline.json
    python3 replay.py --day 2026-07-28 --compare baseline.json
    python3 replay.py --day 2026-07-28 --forward-returns
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import config
import logic_version
import snapshot_recorder
import oi_analytics
import price_action
import trade_tracker as tt
from scanner import scan
from plan_generator import build_plan
from risk_checker import check


def replay_day(day: str, verbose: bool = False) -> dict:
    """
    Run every recorded cycle for `day` through the current logic.

    Deliberately reconstructs derived values (OI analytics, price-action
    structure, ATR, IV-driven scoring) from the recorded raw inputs
    rather than reusing whatever was computed live. That's what makes a
    change to those computations visible here at all.
    """
    state = {"date": day, "trades": [], "opened_today": 0, "stop_cooldowns": {}}
    opened, decisions = [], Counter()
    cycles = 0

    for snapshot, candles, _meta in snapshot_recorder.load_day(day):
        cycles += 1

        # Recompute everything derived, exactly as the live loop would.
        try:
            snapshot.oi_analysis = oi_analytics.analyze(snapshot.chain, snapshot.spot)
        except Exception:
            snapshot.oi_analysis = None

        price_levels, context, atr = [], None, None
        if candles:
            try:
                price_levels, context = price_action.analyze_with_context(candles)
                atr = price_action.compute_atr(candles)
            except Exception:
                pass

        # Mark open trades against this cycle, same as live.
        tt.update_open_trades(state, snapshot)

        setups = scan(snapshot, price_levels=price_levels, context=context)
        if not setups:
            decisions["NO_SETUPS"] += 1
            continue

        results = []
        for setup in setups:
            try:
                plan = build_plan(snapshot, setup, atr=atr)
            except ValueError:
                continue
            exposure_pct, daily_loss_pct = tt.compute_risk_state(state)
            verdict = check(plan, exposure_pct, daily_loss_pct, "normal")
            if verdict.decision == "REJECTED" and plan.lots == 0:
                continue
            results.append((setup, plan, verdict))

        if not results:
            decisions["NO_VIABLE_PLANS"] += 1
            continue

        results.sort(key=lambda r: r[0].score, reverse=True)
        trade = tt.try_open_new_trade(results, state, snapshot)
        if trade:
            decisions["OPENED"] += 1
            opened.append({
                "opened_at": trade["opened_at"],
                "strike": trade["strike"],
                "option_type": trade["option_type"],
                "entry": trade["entry"],
                "target": trade["target"],
                "stop": trade["stop"],
                "score": trade["score_at_entry"],
                "adjusted_score": trade["adjusted_score_at_entry"],
                "stop_basis": trade.get("stop_basis", ""),
            })
            if verbose:
                print(f"  [OPEN] {trade['opened_at'][11:19]} {trade['strike']} "
                      f"{trade['option_type']} @ {trade['entry']} "
                      f"(score {trade['score_at_entry']} -> {trade['adjusted_score_at_entry']})")
        else:
            decisions["NO_TRADE"] += 1

    # Settle anything still open on the last recorded chain, so replayed
    # trades have outcomes rather than dangling.
    closed = []
    if state["trades"]:
        last = None
        for snapshot, _c, _m in snapshot_recorder.load_day(day):
            last = snapshot
        if last is not None:
            closed = tt.force_close_end_of_day(state, last)

    return {
        "day": day,
        "logic_version": logic_version.compute(),
        "cycles": cycles,
        "decision_counts": dict(decisions),
        "opened": opened,
        "closed": [
            {"strike": c["strike"], "option_type": c["option_type"], "entry": c["entry"],
             "exit": c.get("exit_ltp"), "outcome": c.get("outcome"),
             "pnl_pct": c.get("pnl_pct"), "max_r_reached": c.get("max_r_reached")}
            for c in closed
        ],
    }


def forward_returns(day: str, horizon_minutes: int = 30) -> list:
    """
    Counterfactual evaluation: for EVERY candidate the scanner flagged,
    what did that contract actually do over the next `horizon_minutes`?

    This is the point of recording chains. The live journal only contains
    trades that were taken -- roughly 4 a day out of 12-15 distinct
    candidates considered -- which is both a tiny sample and a
    survivorship-biased one. It cannot tell you whether your REJECTIONS
    were good rejections. This can.

    Returns one row per (cycle, candidate) with the forward return of the
    contract, so accepted and rejected candidates can be compared on the
    same footing.
    """
    cycles = list(snapshot_recorder.load_day(day))
    if not cycles:
        return []

    # Index every cycle's chain by contract for O(1) forward lookup.
    indexed = []
    for snapshot, candles, _m in cycles:
        indexed.append((
            snapshot.timestamp,
            {(q.strike, q.option_type): q.ltp for q in snapshot.chain},
            snapshot, candles,
        ))

    rows = []
    for i, (ts, _prices, snapshot, candles) in enumerate(indexed):
        # Find the cycle closest to `horizon_minutes` ahead.
        future = None
        for j in range(i + 1, len(indexed)):
            elapsed = (indexed[j][0] - ts).total_seconds() / 60
            if elapsed >= horizon_minutes:
                future = indexed[j]
                break
        if future is None:
            break

        price_levels, context = ([], None)
        if candles:
            try:
                price_levels, context = price_action.analyze_with_context(candles)
            except Exception:
                pass

        try:
            snapshot.oi_analysis = oi_analytics.analyze(snapshot.chain, snapshot.spot)
        except Exception:
            snapshot.oi_analysis = None

        for setup in scan(snapshot, price_levels=price_levels, context=context):
            key = (setup.strike, setup.option_type)
            now_px, later_px = None, future[1].get(key)
            for q in snapshot.chain:
                if (q.strike, q.option_type) == key:
                    now_px = q.ltp
                    break
            if not now_px or not later_px:
                continue
            adjusted, _notes = tt.apply_learned_adjustment(setup.score, setup.reasons)
            rows.append({
                "timestamp": ts.isoformat(),
                "strike": setup.strike,
                "option_type": setup.option_type,
                "raw_score": setup.score,
                "adjusted_score": adjusted,
                "cleared_bar": adjusted >= config.MIN_CONVICTION_SCORE_TO_TRACK,
                "price_now": now_px,
                "price_later": later_px,
                "forward_return_pct": round((later_px - now_px) / now_px * 100, 2),
            })
    return rows


def summarise_forward_returns(rows: list, horizon_minutes: int) -> str:
    """
    Does a higher score actually predict a better forward return? This is
    the single most important question about the scoring model, and it is
    answerable from recorded data alone -- no trades required.
    """
    if not rows:
        return "No forward-return rows (need at least one cycle pair spanning the horizon)."

    buckets = {}
    for r in rows:
        b = round(r["adjusted_score"] * 2) / 2   # half-point buckets
        buckets.setdefault(b, []).append(r["forward_return_pct"])

    lines = [f"Forward return by adjusted score, {horizon_minutes}min horizon ({len(rows)} observations):",
             f"{'score':>7} {'n':>6} {'mean %':>9} {'median %':>10} {'win rate':>9}"]
    for b in sorted(buckets):
        vals = sorted(buckets[b])
        mean = sum(vals) / len(vals)
        median = vals[len(vals) // 2]
        winrate = sum(1 for v in vals if v > 0) / len(vals) * 100
        lines.append(f"{b:>7} {len(vals):>6} {mean:>9.2f} {median:>10.2f} {winrate:>8.1f}%")

    cleared = [r["forward_return_pct"] for r in rows if r["cleared_bar"]]
    rejected = [r["forward_return_pct"] for r in rows if not r["cleared_bar"]]
    lines.append("")
    if cleared:
        lines.append(f"  cleared the {config.MIN_CONVICTION_SCORE_TO_TRACK} bar: n={len(cleared):<5} "
                     f"mean {sum(cleared)/len(cleared):+.2f}%")
    else:
        lines.append(f"  cleared the {config.MIN_CONVICTION_SCORE_TO_TRACK} bar: n=0")
    if rejected:
        lines.append(f"  rejected below bar:      n={len(rejected):<5} "
                     f"mean {sum(rejected)/len(rejected):+.2f}%")
    lines.append("\n  If the rejected set doesn't do WORSE than the accepted set, the "
                 "conviction bar\n  is not selecting anything -- that is the finding to act on.")
    return "\n".join(lines)


def compare(baseline: dict, current: dict) -> str:
    """Diff two replay runs so a logic change's effect is explicit."""
    def key(t):
        return (t["opened_at"][:19], t["strike"], t["option_type"])

    base_map = {key(t): t for t in baseline.get("opened", [])}
    cur_map = {key(t): t for t in current.get("opened", [])}

    only_base = [t for k, t in base_map.items() if k not in cur_map]
    only_cur = [t for k, t in cur_map.items() if k not in base_map]
    both = [(base_map[k], cur_map[k]) for k in base_map if k in cur_map]

    lines = [
        f"Baseline logic: {baseline.get('logic_version')}  ({len(base_map)} trades)",
        f"Current  logic: {current.get('logic_version')}  ({len(cur_map)} trades)",
        "",
    ]
    if not only_base and not only_cur:
        lines.append("Same trades opened by both versions.")
    if only_base:
        lines.append(f"NO LONGER opened ({len(only_base)}):")
        for t in only_base:
            lines.append(f"  - {t['opened_at'][11:19]} {t['strike']} {t['option_type']} @ {t['entry']}")
    if only_cur:
        lines.append(f"NEWLY opened ({len(only_cur)}):")
        for t in only_cur:
            lines.append(f"  + {t['opened_at'][11:19]} {t['strike']} {t['option_type']} @ {t['entry']}")

    changed = [(b, c) for b, c in both if b["target"] != c["target"] or b["stop"] != c["stop"]]
    if changed:
        lines.append(f"Same trade, CHANGED geometry ({len(changed)}):")
        for b, c in changed:
            lines.append(f"  ~ {c['strike']} {c['option_type']}: "
                         f"target {b['target']}->{c['target']}, stop {b['stop']}->{c['stop']}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="show recorded history available")
    p.add_argument("--day", help="ISO date to replay, e.g. 2026-07-28")
    p.add_argument("--all", action="store_true", help="replay every recorded day")
    p.add_argument("--verbose", action="store_true", help="print each trade as it opens")
    p.add_argument("--save", metavar="FILE", help="write this run's result to FILE (a baseline)")
    p.add_argument("--compare", metavar="FILE", help="diff this run against a saved baseline")
    p.add_argument("--forward-returns", action="store_true",
                   help="counterfactual: forward return of EVERY candidate, not just traded ones")
    p.add_argument("--horizon", type=int, default=30, help="forward-return horizon in minutes (default 30)")
    args = p.parse_args()

    if args.list or not (args.day or args.all):
        print(snapshot_recorder.describe())
        print()
        print(logic_version.describe())
        if not (args.day or args.all):
            print("\nNothing to replay yet. Pass --day YYYY-MM-DD once history has been recorded.")
        return

    days = snapshot_recorder.available_days() if args.all else [args.day]
    results = []
    for day in days:
        if args.forward_returns:
            rows = forward_returns(day, args.horizon)
            print(f"\n=== {day} ===")
            print(summarise_forward_returns(rows, args.horizon))
            continue

        print(f"\n=== replaying {day} ===")
        result = replay_day(day, verbose=args.verbose)
        results.append(result)
        print(f"cycles: {result['cycles']}   logic: {result['logic_version']}")
        print(f"decisions: {result['decision_counts']}")
        print(f"trades opened: {len(result['opened'])}")
        for c in result["closed"]:
            print(f"  {c['strike']} {c['option_type']}  {c['entry']} -> {c['exit']}  "
                  f"{c['outcome']}  {c['pnl_pct']:+.1f}%  peak {c.get('max_r_reached')}R")

    if args.save and results:
        Path(args.save).write_text(json.dumps(results[0] if len(results) == 1 else results,
                                              indent=2, default=str))
        print(f"\nSaved baseline: {args.save}")

    if args.compare and results:
        baseline = json.loads(Path(args.compare).read_text())
        if isinstance(baseline, list):
            baseline = baseline[0]
        print("\n" + compare(baseline, results[0]))


if __name__ == "__main__":
    main()
