"""
Runs the full pipeline: SCAN -> SIGNALS -> PLAN -> RISK -> DECISION.
This is decision-support only. It prints recommendations for you to
review and act on manually. Nothing here places an order.
"""

from data_source import load_snapshot_from_csv
from scanner import scan
from plan_generator import build_plan
from risk_checker import check


def run(csv_path: str, current_open_exposure_pct: float = 0.0, current_daily_loss_pct: float = 0.0):
    snapshot = load_snapshot_from_csv(csv_path)
    print(f"\n=== NIFTY snapshot: spot {snapshot.spot}, VWAP {snapshot.vwap}, PCR {snapshot.pcr} ===\n")

    setups = scan(snapshot)
    if not setups:
        print("No setups flagged in this snapshot.")
        return

    for setup in setups:
        plan = build_plan(snapshot, setup)
        verdict = check(
            plan,
            current_open_exposure_pct=current_open_exposure_pct,
            current_daily_loss_pct=current_daily_loss_pct,
        )

        print(f"--- {setup.symbol} {setup.strike} {setup.option_type} ({setup.expiry}) ---")
        print(f"Score: {setup.score}  |  Reasons: {', '.join(setup.reasons)}")
        print(
            f"Plan: entry {plan.entry}, target {plan.target}, stop {plan.stop}, "
            f"lots {plan.lots}, risk {plan.risk_pct_of_capital}% ({plan.risk_level})"
        )
        print(f"Invalidation: {plan.invalidation}")
        print(f"DECISION: {verdict.decision}")
        if verdict.reasons:
            print(f"Notes: {'; '.join(verdict.reasons)}")
        print()


if __name__ == "__main__":
    run("sample_data.csv")
