"""
Interactive review CLI for staged orders. This is the human half of the
approval gate in trade_staging.py -- run it, look at each PENDING order,
and explicitly approve or reject it. Nothing here ever places an order;
approving just flips a status in state/staged_orders.json for a future
execution layer (which doesn't exist yet) to eventually read.

Run:
  python3 approve_orders.py
"""

import trade_staging as staging


def main():
    pending = staging.list_staged(status="PENDING")

    if not pending:
        print("No pending staged orders.")
        _print_summary()
        return

    print(f"{len(pending)} pending staged order(s) to review.\n")

    for record in pending:
        print(staging.render_diff(record))
        choice = input("\n  Approve / Reject / Skip? [a/r/s]: ").strip().lower()

        if choice == "a":
            note = input("  Optional approval note: ").strip()
            staging.approve(record["id"], note=note)
            print(f"  -> {record['id']} APPROVED\n")
        elif choice == "r":
            note = input("  Reason for rejecting: ").strip()
            staging.reject(record["id"], note=note)
            print(f"  -> {record['id']} REJECTED\n")
        else:
            print(f"  -> {record['id']} left PENDING (skipped)\n")

    _print_summary()


def _print_summary():
    all_records = staging.list_staged()
    counts = {}
    for r in all_records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if all_records:
        print("Staged order summary:", ", ".join(f"{k} {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
