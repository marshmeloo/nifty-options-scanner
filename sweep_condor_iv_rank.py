"""
Would a LOWER India-VIX IV-rank entry gate let the condor trade more
without giving back its edge?

WHY THIS EXISTS
---------------
`config_condor.MIN_IV_RANK_TO_OPEN = 15.0` stops the condor opening when
India VIX's IV rank is below 15. It has done exactly that every session
since 2026-08-13 (VIX ~11.3-11.5, IV rank 11-12), so the strategy has sat
flat for a week. The gate is working as designed -- selling premium into
a dead-calm market is what it exists to prevent -- but "never trades" is
also not a strategy, so the threshold itself is worth re-examining.

THE GAP THIS FILLS
------------------
The sweep that chose 15 (BACKLOG 2026-08-12) tested
min_iv_rank in {15, 20, 25, 30}. **15 was the LOWEST value tried**, so
"is something below 15 better?" was never actually asked -- 15 won a
grid it sat on the edge of. That is a materially different situation
from a value that beat neighbours on both sides.

What IS known from that sweep: no gate at all (`baseline`) was the worst
cell (-Rs 31,527, positive in only 2 of 4 periods), and raising the gate
above 15 got progressively more mixed. So the effect is not monotonic
"higher is better" -- which is precisely why the untested side is worth
measuring rather than assumed.

METHOD, MATCHING THE ORIGINAL SO RESULTS ARE COMPARABLE
--------------------------------------------------------
  - Same 4 independent periods, so a cell has to survive regime changes
    rather than ride one good stretch.
  - PT=50% held fixed (the original found the profit-target level
    "secondary to the IV-rank gate itself", so varying it again would
    just re-litigate a settled axis and multiply the comparison count).
  - Real p90 costs via spread_cost_study.multi_leg_costs -- the
    pessimistic spread scenario, since a strategy whose verdict flips
    between median and p90 spreads is not a real edge.
  - Thresholds are FIXED, pre-specified round numbers, never derived
    from this sample (see CondorPolicy.min_iv_rank's own comment on why
    a sample-derived threshold would be look-ahead).

Run: python sweep_condor_iv_rank.py
"""

import argparse
import json
import statistics

import india_vix_source as ivs
import shadow_condor
import snapshot_recorder
import spread_cost_study

# The 4 independent stretches the real data supports. VIX history starts
# 2021-08, so ~2022-08 is the earliest date an IV rank is computable --
# same boundaries as BACKLOG's 2026-08-12 entry, deliberately, so these
# numbers can be read against that table.
PERIODS = [
    ("2022-08..2023-12", "2022-08-01", "2023-12-31"),
    ("2024-01..2024-07", "2024-01-01", "2024-07-31"),
    ("2024-08..2025-12", "2024-08-01", "2025-12-31"),
    ("2026-01..2026-08", "2026-01-01", "2026-12-31"),
]

# None = no gate at all (the known-bad baseline, kept as the floor of the
# range). 15 = today's live value. Everything below it is the untested
# territory this sweep exists to cover.
IV_RANK_GRID = [None, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0]

PROFIT_TARGET_PCT = 50.0


def _net_inr(condor) -> float:
    """
    Gross P&L minus real p90 costs. shadow_condor keeps pnl_inr GROSS by
    design (see its ShadowCondor comment), so costs are charged here.

    Legs: the two SHORT legs pay STT at open; the two hedges are bought.
    expiry_settled matters because a position held to expiry is closed
    by settlement rather than by four reversing orders.
    """
    plan = condor.__dict__
    premiums = [
        plan.get("short_ce_premium") or 0.0,
        plan.get("short_pe_premium") or 0.0,
        plan.get("hedge_ce_premium") or 0.0,
        plan.get("hedge_pe_premium") or 0.0,
    ]
    if not any(premiums):
        return condor.pnl_inr or 0.0
    costs = spread_cost_study.multi_leg_costs(
        premiums, scenario="p90", lots=1,
        expiry_settled=(condor.exit_reason == "expiry_settlement"),
        sold_legs=[True, True, False, False],
    )
    return (condor.pnl_inr or 0.0) - costs["total"]


def run_cell(days: list, min_iv_rank, vix_history: list) -> dict:
    policy = shadow_condor.CondorPolicy(
        name=f"iv>={min_iv_rank}",
        profit_target_pct=PROFIT_TARGET_PCT,
        min_iv_rank=min_iv_rank,
        vix_history=vix_history,
    )
    condors, _skips = shadow_condor.run_all(days=days, policy=policy)
    closed = [c for c in condors if getattr(c, "pnl_inr", None) is not None]
    if not closed:
        return {"n": 0, "net_inr": 0.0, "win_pct": None}
    nets = [_net_inr(c) for c in closed]
    return {
        "n": len(closed),
        "net_inr": round(sum(nets)),
        "win_pct": round(100 * sum(1 for v in nets if v > 0) / len(nets), 1),
        "mean_inr": round(statistics.mean(nets)),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/sweep_condor_iv_rank.json")
    args = p.parse_args()

    vix_history = ivs.load_or_refresh_history()
    all_days = snapshot_recorder.available_days()
    print(f"{len(all_days)} recorded days, VIX history {vix_history[0][0]}..{vix_history[-1][0]}\n", flush=True)

    results = {}
    for gate in IV_RANK_GRID:
        label = "none" if gate is None else f"{gate:g}"
        per_period = {}
        for pname, start, end in PERIODS:
            days = [d for d in all_days if start <= d <= end]
            per_period[pname] = run_cell(days, gate, vix_history) if days else {"n": 0, "net_inr": 0}
        total_n = sum(r["n"] for r in per_period.values())
        total_net = sum(r["net_inr"] for r in per_period.values())
        pos = sum(1 for r in per_period.values() if r["net_inr"] > 0)
        results[label] = {
            "min_iv_rank": gate,
            "total_n": total_n,
            "total_net_inr": total_net,
            "periods_positive": pos,
            "per_period": per_period,
        }
        print(f"  iv_rank>={label:<5} n={total_n:>4}  net=Rs {total_net:>+9,}  "
              f"periods_positive={pos}/4", flush=True)

    print()
    print(f"{'gate':<10} {'trades':>7} {'net Rs':>12} {'pos':>5}  " +
          "  ".join(f"{n[:9]:>10}" for n, _, _ in PERIODS))
    for label, r in results.items():
        cells = "  ".join(f"{r['per_period'][n]['net_inr']:>+10,}" for n, _, _ in PERIODS)
        print(f"iv>={label:<6} {r['total_n']:>7} {r['total_net_inr']:>+12,} "
              f"{r['periods_positive']:>4}/4  {cells}")

    with open(args.out, "w") as f:
        json.dump({"profit_target_pct": PROFIT_TARGET_PCT,
                   "periods": [p[0] for p in PERIODS], "results": results}, f, indent=2)
    print(f"\nwritten to {args.out}")
    print("\nCosts: real p90 spread scenario. A cell is only interesting if it keeps "
          "the 4/4 consistency AND meaningfully raises trade count.")


if __name__ == "__main__":
    main()
