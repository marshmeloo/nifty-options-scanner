"""
The gate + reversal exit on BANK NIFTY's own 1,244-day reconstructed
history -- the measurement that was wrongly believed impossible.

WHY THIS EXISTS. On 2026-08-28 it was asserted (in shadow.py comments,
BACKLOG.md and a commit message) that Bank Nifty had no reconstructed
history and that every Bank Nifty conclusion was extrapolated from
NIFTY. That was FALSE, and the error was checking the PRODUCTION
checkout, which only keeps what the live processes record (12 days).
The research data lives in DEV: logs/snapshots_banknifty holds 1,244
days, 2021-08-04 to 2026-08-11, backfilled from the same Dhan Expired
Options endpoint NIFTY's history comes from. sweep_banknifty_cluster_cap.py
had already used it to choose the live 500pt band across 5 independent
~1-year periods.

So Bank Nifty CAN be measured directly, and this does it, using the same
three-stage comparison research/directional_exposure_backtest.py runs
for NIFTY.

Bank Nifty's live config is applied via shadow.BANKNIFTY_SENTINEL_OVERRIDES
(lot 30, premium 300-800, strike range 2000, cluster cap 500pt/30min),
which mirrors main_live_banknifty_sentinel.py -- see that constant's own
comment for why replaying another underlying needs the WHOLE patch set.

    python -m research.banknifty_directional_exposure_backtest
"""

import argparse
import json
from pathlib import Path

import shadow
import snapshot_recorder
from research.directional_exposure_backtest import STAGES, annual_table
from research.one_trade_per_day_study import institutional_metrics, to_rows
from research.concurrent_direction_exposure_study import CAPITAL

BN_SNAPSHOT_DIR = Path(__file__).parent.parent / "logs" / "snapshots_banknifty"


def banknifty_days() -> list:
    days = []
    for day in snapshot_recorder.available_days(snapshot_dir=BN_SNAPSHOT_DIR):
        first = next(snapshot_recorder.load_day(
            day, snapshot_dir=BN_SNAPSHOT_DIR, symbol="BANKNIFTY"), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    return sorted(days)


def run_stage(stage: str, gate: bool, exit_: bool, days: list) -> dict:
    ov = shadow.BANKNIFTY_SENTINEL_OVERRIDES
    policy = shadow.Policy(
        name=f"BankNifty_{stage}", use_learned_adjustment=False,
        symbol="BANKNIFTY", snapshot_dir=str(BN_SNAPSHOT_DIR), config_overrides=ov,
        strike_adjacency_band_points=ov["CLUSTER_CAP_ADJACENCY_POINTS"],
        cluster_window_minutes=ov["CLUSTER_CAP_WINDOW_MINUTES"],
        use_opposite_direction_gate=gate, use_reversal_exit=exit_)
    trades = []
    for day in days:
        try:
            trades.extend(shadow.run_policy(day, policy))
        except Exception as e:
            print(f"    {day} failed: {type(e).__name__}", flush=True)
    closed = [t for t in trades if t.outcome]
    rows = to_rows(closed)
    return {
        "metrics": institutional_metrics(rows, capital=CAPITAL),
        "annual": annual_table(rows, CAPITAL),
        "n_reversal_exits": sum(1 for t in closed if t.outcome == "REVERSAL_EXIT"),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/banknifty_directional_exposure_backtest.json")
    args = p.parse_args()

    days = banknifty_days()
    print(f"{len(days)} reconstructed BANK NIFTY days "
          f"({days[0]} -> {days[-1]})\n", flush=True)

    out = {"n_days": len(days), "capital": CAPITAL, "stages": {}}
    for stage, gate, exit_ in STAGES:
        print(f"  BankNifty / {stage} (gate={gate}, exit={exit_})...", flush=True)
        out["stages"][stage] = run_stage(stage, gate, exit_, days)

    keys = (("n", "{:.0f}"), ("win_rate_pct", "{:.1f}%"),
            ("total_return_pct", "{:+.1f}%"), ("max_dd_pct", "{:.1f}%"),
            ("calmar", "{:.2f}"), ("profit_factor", "{:.2f}"),
            ("expectancy_r", "{:+.4f}"), ("avg_trades_per_day", "{:.2f}"))
    print()
    print(f"Bank Nifty Sentinel, Rs{CAPITAL:,.0f} fixed base, {len(days)} days")
    print(f"  {'metric':<22}{'baseline':>14}{'gate only':>14}{'gate + exit':>14}")
    for key, fmt in keys:
        cells = []
        for stage, _g, _e in STAGES:
            v = out["stages"][stage]["metrics"].get(key)
            cells.append(fmt.format(v) if v is not None else "n/a")
        print(f"  {key:<22}" + "".join(f"{c:>14}" for c in cells))
    print(f"  {'reversal exits':<22}" + "".join(
        f"{out['stages'][s]['n_reversal_exits']:>14}" for s, _g, _e in STAGES))
    print()
    years = sorted({r["year"] for s, _g, _e in STAGES for r in out["stages"][s]["annual"]})
    print(f"  {'year':<8}" + "".join(f"{s:>16}" for s, _g, _e in STAGES))
    for y in years:
        cells = []
        for stage, _g, _e in STAGES:
            row = next((x for x in out["stages"][stage]["annual"] if x["year"] == y), None)
            cells.append(f"{row['return_pct']:+.1f}%" if row else "n/a")
        print(f"  {y:<8}" + "".join(f"{c:>16}" for c in cells))

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
