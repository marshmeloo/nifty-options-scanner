"""
Run this after you've approved a condor candidate via approve_orders.py.
Finds the most recent APPROVED "condor_open_candidate" staged advisory,
rebuilds the exact CondorPlan from its stored data (not a fresh re-scan
-- the market may have moved since staging, and this should open exactly
what you reviewed and approved, not a new plan), opens the position in
condor_tracker's state, and marks the staged advisory EXECUTED so it
won't be opened twice.

Run:
  python3 open_approved_condor.py
"""

import trade_staging as staging
import condor_tracker
from models import CondorPlan


def main():
    approved = [r for r in staging.list_staged(status="APPROVED") if r.get("kind") == "condor_open_candidate"]

    if not approved:
        print("No approved condor candidates waiting to be opened.")
        return

    # Most recent first -- if there are several (shouldn't normally
    # happen given MAX_CONCURRENT_POSITIONS=1), only open the latest and
    # leave the rest for manual review.
    approved.sort(key=lambda r: r["staged_at"], reverse=True)
    record = approved[0]

    if len(approved) > 1:
        print(f"WARNING: {len(approved)} approved condor candidates found -- opening only the most recent "
              f"({record['id']}, staged {record['staged_at']}). Review the others manually.")

    plan = CondorPlan(**record["data"])

    state = condor_tracker.load_position()
    if state.get("position") and state["position"].get("status") == "OPEN":
        print("A condor position is already OPEN in state -- refusing to open a second one. "
              "Close or settle the existing position first.")
        return

    position = condor_tracker.open_position(state, plan)
    staging.mark_executed(record["id"], broker_order_id="manual", note="Opened via open_approved_condor.py")

    print(f"Condor position opened: sell {plan.short_ce_strike}CE / {plan.short_pe_strike}PE, "
          f"hedge {plan.hedge_ce_strike}CE / {plan.hedge_pe_strike}PE")
    print(f"Net credit: Rs {plan.net_credit_inr:,.0f}   Max loss: Rs {plan.max_loss_inr:,.0f}")
    print(f"Breakevens: {plan.breakeven_lower} / {plan.breakeven_upper}")
    print(f"Saved to {condor_tracker.STATE_PATH}")


if __name__ == "__main__":
    main()
