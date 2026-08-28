"""
Does replaying a REAL recorded session reproduce the trades that session
actually took? The only END-TO-END measure of backtest fidelity this
project has.

WHY THIS EXISTS
---------------
Every other fidelity check here is component-by-component: is delta
right, is the spread modelled, does the breakeven arm fire. Each can
pass while the whole still diverges. This compares the finished article
-- replay a day whose real trades are already in the journal, and see
whether the same contracts get opened at about the same times.

It only works on days recorded LIVE (`source == "dhan"`), never on
reconstructed history, which has no live journal to compare against.
Live recordings therefore have to accumulate before this says much; with
a handful of days it is a smoke test, not a verdict.

WHAT IT ALREADY CAUGHT
----------------------
Run for the first time on 2026-08-28, this is what surfaced the stale
forming-candle bug in shadow._StructureCache (the replay took NO trades
on 2026-08-17 where live took two; the cached structure was up to a full
candle old, scoring NIFTY 24300 CE at 3.0 against live's 6.0, straddling
MIN_CONVICTION_SCORE_TO_TRACK). Chasing that mismatch instead of
writing it off as timing noise then exposed two more divergences of the
same kind -- the flat-30% stop and the missing breakeven arm. See
BACKLOG.md.

READ THE OUTPUT HONESTLY. Exact equality is NOT the bar and never will
be: live acts on a ~30-40s polling cycle against a 5-minute recorded
one, live sees intra-candle prices the recording cannot preserve, and
live's own state (cooldowns, what was already open) is not reconstructed
here. Persistent DIRECTIONAL disagreement -- replay systematically
taking more, fewer, or different-side trades -- is the signal worth
chasing. A few minutes' difference in entry time is not.

    python -m research.live_replay_parity --journal logs/trade_journal_sentinel.jsonl --sentinel
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import shadow
import snapshot_recorder

SENTINEL_KWARGS = {"strike_adjacency_band_points": 200, "cluster_window_minutes": 30}


def live_trades_by_day(journal_path: Path) -> dict:
    by_day = defaultdict(list)
    if not journal_path.exists():
        return by_day
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        opened = r.get("opened_at")
        if not opened:
            continue
        by_day[opened[:10]].append({
            "ts": opened, "strike": r.get("strike"), "option_type": r.get("option_type"),
            "outcome": r.get("outcome"), "net_inr": r.get("pnl_inr_net"),
            "version": r.get("strategy_version"),
        })
    return by_day


def recorded_live_days(snapshot_dir: Path, symbol: str = "NIFTY") -> list:
    """Only days recorded from the LIVE feed -- reconstructed days have no
    journal to compare against and would silently pad the result."""
    out = []
    for day in snapshot_recorder.available_days(snapshot_dir=snapshot_dir):
        first = next(snapshot_recorder.load_day(day, snapshot_dir=snapshot_dir, symbol=symbol), None)
        if first is not None and first[0].source == "dhan":
            out.append(day)
    return sorted(out)


def replay(day: str, snapshot_dir: Path, sentinel: bool, symbol: str = "NIFTY") -> list:
    """Bank Nifty needs its live process's WHOLE config patch set, not just
    the lot size -- see shadow.BANKNIFTY_SENTINEL_OVERRIDES."""
    kwargs = dict(SENTINEL_KWARGS) if sentinel else {}
    overrides = None
    if symbol.upper() == "BANKNIFTY":
        overrides = shadow.BANKNIFTY_SENTINEL_OVERRIDES
        if sentinel:
            kwargs["strike_adjacency_band_points"] = overrides["CLUSTER_CAP_ADJACENCY_POINTS"]
            kwargs["cluster_window_minutes"] = overrides["CLUSTER_CAP_WINDOW_MINUTES"]
    policy = shadow.Policy(name="parity", use_learned_adjustment=False,
                           symbol=symbol, snapshot_dir=str(snapshot_dir),
                           config_overrides=overrides,
                           use_opposite_direction_gate=True, use_reversal_exit=True,
                           **kwargs)
    return [t for t in shadow.run_policy(day, policy) if t.outcome]


def _minutes(a: str, b: str) -> float:
    return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) / 60


def match(live: list, sim: list, strike_tol: float, minute_tol: float) -> dict:
    """Greedy nearest-in-time pairing on the same option_type and a nearby
    strike. Deliberately loose: the question is whether the replay found
    the same OPPORTUNITY, not whether it ticked at the same second."""
    unmatched_sim = list(sim)
    pairs, missed = [], []
    for lt in live:
        best, best_gap = None, None
        for st in unmatched_sim:
            if st.option_type != lt["option_type"]:
                continue
            if abs(st.strike - lt["strike"]) > strike_tol:
                continue
            gap = _minutes(lt["ts"], st.opened_at)
            if gap > minute_tol:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = st, gap
        if best is None:
            missed.append(lt)
        else:
            unmatched_sim.remove(best)
            pairs.append((lt, best, best_gap))
    return {"pairs": pairs, "live_only": missed, "sim_only": unmatched_sim}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", required=True, help="live trade journal to compare against")
    p.add_argument("--snapshot-dir", default=None,
                   help="recorded snapshots (default: this checkout's logs/snapshots)")
    p.add_argument("--sentinel", action="store_true", help="use Sentinel's cluster-cap config")
    p.add_argument("--symbol", default="NIFTY", help="underlying to replay (e.g. BANKNIFTY)")
    p.add_argument("--strike-tol", type=float, default=100.0,
                   help="Bank Nifty strikes are 100pt apart on a ~57,000 index -- widen this")
    p.add_argument("--minute-tol", type=float, default=15.0)
    p.add_argument("--out", default="logs/live_replay_parity.json")
    args = p.parse_args()

    snap_dir = Path(args.snapshot_dir) if args.snapshot_dir else snapshot_recorder.SNAPSHOT_DIR
    live_by_day = live_trades_by_day(Path(args.journal))
    days = recorded_live_days(snap_dir, args.symbol)
    if not days:
        print(f"No LIVE-recorded days in {snap_dir} -- nothing to compare.")
        return

    tot_live = tot_sim = tot_matched = 0
    report = []
    print(f"{len(days)} live-recorded day(s) in {snap_dir}\n")
    print(f"  {'day':<12}{'live':>6}{'replay':>8}{'matched':>9}{'live only':>11}{'replay only':>13}")
    for day in days:
        live = sorted(live_by_day.get(day, []), key=lambda r: r["ts"])
        sim = replay(day, snap_dir, args.sentinel, args.symbol)
        m = match(live, sim, args.strike_tol, args.minute_tol)
        tot_live += len(live); tot_sim += len(sim); tot_matched += len(m["pairs"])
        print(f"  {day:<12}{len(live):>6}{len(sim):>8}{len(m['pairs']):>9}"
              f"{len(m['live_only']):>11}{len(m['sim_only']):>13}")
        report.append({
            "day": day, "n_live": len(live), "n_replay": len(sim),
            "n_matched": len(m["pairs"]),
            "matched": [{"strike": lt["strike"], "option_type": lt["option_type"],
                         "live_ts": lt["ts"], "replay_ts": st.opened_at,
                         "gap_min": round(gap, 1),
                         "live_outcome": lt["outcome"], "replay_outcome": st.outcome,
                         "live_net": lt["net_inr"], "replay_net": st.net_inr}
                        for lt, st, gap in m["pairs"]],
            "live_only": m["live_only"],
            "replay_only": [{"strike": t.strike, "option_type": t.option_type,
                             "ts": t.opened_at, "outcome": t.outcome} for t in m["sim_only"]],
        })

    print(f"\n  {'TOTAL':<12}{tot_live:>6}{tot_sim:>8}{tot_matched:>9}")
    if tot_live:
        print(f"\n  live trades the replay also found : {tot_matched}/{tot_live} "
              f"({tot_matched / tot_live * 100:.0f}%)")
    if tot_sim:
        print(f"  replay trades live also took      : {tot_matched}/{tot_sim} "
              f"({tot_matched / tot_sim * 100:.0f}%)")
    print("\n  Exact equality is not the bar -- see this module's docstring.")

    with open(args.out, "w") as f:
        json.dump({"snapshot_dir": str(snap_dir), "journal": args.journal,
                   "strike_tol": args.strike_tol, "minute_tol": args.minute_tol,
                   "days": report}, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
