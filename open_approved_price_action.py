"""
Run this after you've approved a price-action candidate via
approve_orders.py (only relevant when config_price_action.
AUTO_APPROVE_NEW_POSITIONS is False -- with it True, main_price_action.py
already opened the position immediately and this has nothing to do).

Finds the most recent APPROVED "price_action_open_candidate" staged
advisory, rebuilds the exact Setup/TradePlan from its stored data (not a
fresh re-scan -- the market may have moved since staging, and this should
open exactly what you reviewed and approved), opens it in
price_action_tracker's state, and marks the staged advisory EXECUTED so
it won't be opened twice.

Run:
  python3 open_approved_price_action.py
"""

from datetime import datetime

import price_action_tracker as pat
import trade_staging as staging
from models import Setup, TradePlan


def main():
    approved = [r for r in staging.list_staged(status="APPROVED")
               if r.get("kind") == "price_action_open_candidate"]

    if not approved:
        print("No approved price-action candidates waiting to be opened.")
        return

    approved.sort(key=lambda r: r["staged_at"], reverse=True)
    record = approved[0]
    if len(approved) > 1:
        print(f"WARNING: {len(approved)} approved price-action candidates found -- opening only "
              f"the most recent ({record['id']}, staged {record['staged_at']}). Review the others manually.")

    data = dict(record["data"])
    pair = data.pop("pair")
    setup_data = data.pop("setup")
    setup = Setup(**setup_data)
    plan = TradePlan(setup=setup, **data)

    state = pat.load_state()
    if pat.has_open_trade(state, pair):
        print(f"A {pair} position is already OPEN in state -- refusing to open a second one. "
              f"Close it first, or review the other staged candidates manually.")
        return

    class _Snap:
        timestamp = datetime.now()

    trade = pat.open_position(state, setup, plan, pair, _Snap())
    staging.mark_executed(record["id"], broker_order_id="manual",
                          note="Opened via open_approved_price_action.py")

    print(f"Price-action position opened [{pair}]: BUY {plan.setup.strike}{plan.setup.option_type} "
          f"@ {plan.entry}, stop {plan.stop}, target {plan.target}")
    print(f"Saved to {pat.STATE_PATH}")


if __name__ == "__main__":
    main()
