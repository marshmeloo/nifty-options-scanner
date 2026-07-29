"""
Copy recorded market data from PRODUCTION into DEVELOPMENT, one way.

WHY A COPY, NOT A SHARED PATH
-----------------------------
Development could have been pointed straight at production's data
directory, which would avoid duplication and staleness. It was rejected
deliberately: analysis code evolves fast and occasionally has bugs, and
a single stray write into production's trade journal or state files is
the worst outcome this system has. That already happened once during
development, when replay.py appended 35 simulated trades to the real
journal before the guard existed.

A one-way copy cannot corrupt production no matter what development
does. The cost is a few MB per day and having to re-run this to pick up
new sessions -- a trade worth making.

WHAT IS COPIED
--------------
  logs/snapshots/*.jsonl.gz   recorded chains, the input to shadow/replay
  logs/trade_journal.jsonl    real trade outcomes
  logs/decision_log.jsonl     per-cycle audit trail
  state/*.json                open trades, baselines, OI history

WHAT IS NOT
-----------
Code. Code flows the other way (development -> production) via git,
after market close. This script refuses to write anything into
production.

USAGE
-----
    python3 sync_from_prod.py              # copy, reporting what changed
    python3 sync_from_prod.py --dry-run    # show what would be copied
    python3 sync_from_prod.py --prod PATH  # non-default production path
"""

import argparse
import shutil
from pathlib import Path

import workspace

DEFAULT_PROD = Path(r"D:\AI Projects\nifty-options-scanner")
DEV_DIR = Path(__file__).parent

COPY_SPEC = (
    ("logs/snapshots", "*.jsonl.gz"),
    ("logs", "trade_journal.jsonl"),
    ("logs", "decision_log.jsonl"),
    ("state", "*.json"),
)


def _describe(path: Path) -> str:
    if not path.exists():
        return "absent"
    size = path.stat().st_size
    return f"{size / 1_048_576:.2f} MB" if size > 1_048_576 else f"{size:,} B"


def sync(prod_dir: Path, dry_run: bool = False) -> list:
    if not prod_dir.exists():
        raise SystemExit(f"Production path not found: {prod_dir}")
    if prod_dir.resolve() == DEV_DIR.resolve():
        raise SystemExit("Refusing to sync a directory onto itself.")
    if workspace.is_production():
        raise SystemExit(
            "This checkout is marked PRODUCTION. sync_from_prod.py copies data INTO "
            "development; running it here would overwrite live data with a copy of itself."
        )

    actions = []
    for subdir, pattern in COPY_SPEC:
        src_dir = prod_dir / subdir
        if not src_dir.exists():
            continue
        dst_dir = DEV_DIR / subdir
        for src in sorted(src_dir.glob(pattern)):
            dst = dst_dir / src.name
            # Skip files already identical in size and mtime -- snapshots
            # for past days never change, so a re-sync should be cheap.
            if dst.exists() and dst.stat().st_size == src.stat().st_size \
                    and int(dst.stat().st_mtime) == int(src.stat().st_mtime):
                actions.append(("skip", src, dst))
                continue
            if not dry_run:
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            actions.append(("copy", src, dst))
    return actions


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prod", default=str(DEFAULT_PROD), help="production checkout path")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    prod = Path(args.prod)
    print(f"production : {prod}")
    print(f"development: {DEV_DIR}")
    print()

    actions = sync(prod, dry_run=args.dry_run)
    copied = [a for a in actions if a[0] == "copy"]
    skipped = [a for a in actions if a[0] == "skip"]

    for _kind, src, _dst in copied:
        print(f"  {'would copy' if args.dry_run else 'copied'}  {src.parent.name}/{src.name}  ({_describe(src)})")
    if skipped:
        print(f"  {len(skipped)} file(s) already up to date")
    if not actions:
        print("  nothing found to copy -- has production recorded a session yet?")

    print()
    print(workspace.status())


if __name__ == "__main__":
    main()
