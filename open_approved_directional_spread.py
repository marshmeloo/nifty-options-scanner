"""
Run this after you've approved a directional-spread candidate via
approve_orders.py. Finds the most recent APPROVED
"directional_spread_open_candidate" staged advisory, rebuilds the exact
DirectionalSpreadPlan from its stored data (not a fresh re-scan -- the
market may have moved since staging, and this should open exactly what
you reviewed and approved), opens the position in
directional_spread_tracker's state, and marks the staged advisory
EXECUTED so it won't be opened twice.

Run:
  python3 open_approved_directional_spread.py
"""

import trade_staging as staging
import directional_spread_tracker as dst
from models import DirectionalSpreadPlan


def main():
    approved = [r for r in staging.list_staged(status="APPROVED")
               if r.get("kind") == "directional_spread_open_candidate"]

    if not approved:
        print("No approved directional spread candidates waiting to be opened.")
        return

    approved.sort(key=lambda r: r["staged_at"], reverse=True)
    record = approved[0]

    if len(approved) > 1:
        print(f"WARNING: {len(approved)} approved candidates found -- opening only the most recent "
              f"({record['id']}, staged {record['staged_at']}). Review the others manually.")

    plan = DirectionalSpreadPlan(**record["data"])

    state = dst.load_state()
    if state.get("position") and state["position"].get("status") == "OPEN":
        print("A directional spread position is already OPEN in state -- refusing to open a second one. "
              "Close or settle the existing position first.")
        return

    dst.open_position(state, plan)
    staging.mark_executed(record["id"], broker_order_id="manual", note="Opened via open_approved_directional_spread.py")

    kind = "bull put" if plan.direction == "PE" else "bear call"
    print(f"Directional spread opened ({kind}): sell {plan.short_strike} @ {plan.short_premium}, "
          f"hedge {plan.hedge_strike} @ {plan.hedge_premium}")
    print(f"Net credit: Rs {plan.net_credit_inr:,.0f}   Max loss: Rs {plan.max_loss_inr:,.0f}")
    print(f"Breakeven: {plan.breakeven}")
    print(f"Saved to {dst.STATE_PATH}")


if __name__ == "__main__":
    main()
