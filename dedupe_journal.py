"""
One-off repair utility: remove duplicate entries from trade_journal.jsonl.

WHY THIS EXISTS
---------------
`trade_tracker.settle_stale_trades()` used to journal recovered trades
without clearing them from `state/open_trades.json`, leaving that to the
caller. Anything that interrupted the process between journalling and the
caller's save left those trades still marked OPEN on disk, so the next
start recovered and journalled them a SECOND time.

That happened on 2026-07-24: 8 trades from the 2026-07-23 session were
journalled at 00:39:42 (market shut, no quote available -- exit price
fell back to the last known live price, `exit_price_estimated: true`) and
again at 07:28:46 (a fresh quote was available, so slightly different
exit prices). 37 journal lines held only 29 distinct trades.

The tracker itself is fixed and now refuses to re-journal a trade id it
has already written, and `compute_risk_state()` deduplicates defensively.
This script cleans up the entries that were already written before that
fix landed.

WHICH COPY IS KEPT
------------------
The LAST occurrence of each trade id, i.e. the most recently written one.
For the 2026-07-24 duplicates that is the 07:28 pass, which had a real
quote (`exit_price_estimated: false`) rather than the 00:39 pass, which
had to fall back to the last known price. Keeping the fresher, confirmed
exit is both more accurate and the more conservative choice.

USAGE
-----
    python3 dedupe_journal.py            # dry run, shows what would change
    python3 dedupe_journal.py --apply    # writes, after taking a backup

The backup is written next to the journal as
`trade_journal.jsonl.bak-YYYYMMDDHHMMSS` and is never overwritten.
"""

import argparse
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

JOURNAL_PATH = Path(__file__).parent / "logs" / "trade_journal.jsonl"


def load_entries(path: Path):
    """Returns (entries, malformed_lines). Malformed lines are preserved verbatim."""
    entries, malformed = [], []
    if not path.exists():
        return entries, malformed

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entries.append((lineno, raw, json.loads(raw)))
        except json.JSONDecodeError:
            malformed.append((lineno, raw))
    return entries, malformed


def dedupe(entries):
    """
    Keep the LAST entry per trade id, preserving original file order for
    the survivors. Entries with no id are always kept -- we can't prove
    they're duplicates, so we don't touch them.
    """
    last_index_for_id = {}
    for i, (_lineno, _raw, obj) in enumerate(entries):
        tid = obj.get("id")
        if tid:
            last_index_for_id[tid] = i

    kept, dropped = [], []
    for i, item in enumerate(entries):
        tid = item[2].get("id")
        if tid and last_index_for_id[tid] != i:
            dropped.append(item)
        else:
            kept.append(item)
    return kept, dropped


def describe(entries, kept, dropped, malformed):
    ids = [obj.get("id") for _l, _r, obj in entries if obj.get("id")]
    dupe_ids = {tid: n for tid, n in Counter(ids).items() if n > 1}

    print(f"journal:            {JOURNAL_PATH}")
    print(f"total lines:        {len(entries)}")
    print(f"distinct trade ids: {len(set(ids))}")
    print(f"duplicate ids:      {len(dupe_ids)}")
    if malformed:
        print(f"malformed lines:    {len(malformed)} (preserved as-is, not parsed)")
    print()

    if not dropped:
        print("Nothing to do -- no duplicate entries found.")
        return

    dropped_pnl = sum(obj.get("pnl_inr", 0) or 0 for _l, _r, obj in dropped)
    print(f"Would remove {len(dropped)} duplicate line(s), leaving {len(kept)}.")
    print(f"Phantom P&L removed: Rs {dropped_pnl:,.2f}\n")

    for tid in sorted(dupe_ids):
        rows = [(l, obj) for l, _r, obj in entries if obj.get("id") == tid]
        print(f"  {tid}")
        for lineno, obj in rows:
            keep = all(lineno != dl for dl, _r, _o in dropped)
            mark = "KEEP  " if keep else "remove"
            est = obj.get("exit_price_estimated")
            est_note = "" if est is None else ("  [estimated exit]" if est else "  [confirmed quote]")
            print(
                f"    {mark} line {lineno:>3}  closed {obj.get('closed_at')}  "
                f"exit {obj.get('exit_ltp')}  pnl Rs {obj.get('pnl_inr', 0) or 0:,.2f}{est_note}"
            )
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually rewrite the journal (default is a dry run)")
    args = parser.parse_args()

    entries, malformed = load_entries(JOURNAL_PATH)
    if not entries and not malformed:
        print(f"No journal found at {JOURNAL_PATH} -- nothing to do.")
        return

    kept, dropped = dedupe(entries)
    describe(entries, kept, dropped, malformed)

    if not dropped:
        return
    if not args.apply:
        print("Dry run -- nothing written. Re-run with --apply to make these changes.")
        return

    backup = JOURNAL_PATH.with_suffix(
        JOURNAL_PATH.suffix + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    shutil.copy2(JOURNAL_PATH, backup)
    print(f"Backup written: {backup}")

    # Reassemble in original line order, keeping any malformed lines where
    # they were -- this is a trading record, so nothing gets silently lost.
    surviving = {lineno: raw for lineno, raw, _obj in kept}
    surviving.update({lineno: raw for lineno, raw in malformed})
    rebuilt = "\n".join(surviving[k] for k in sorted(surviving)) + "\n"

    tmp = JOURNAL_PATH.with_suffix(JOURNAL_PATH.suffix + ".tmp")
    tmp.write_text(rebuilt, encoding="utf-8")
    tmp.replace(JOURNAL_PATH)   # atomic swap, same pattern as atomic_state.py
    print(f"Rewrote {JOURNAL_PATH}: {len(entries)} lines -> {len(surviving)}")


if __name__ == "__main__":
    main()
