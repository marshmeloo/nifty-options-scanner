"""
Which checkout am I, and is it safe to do this here?

WHY THIS EXISTS
---------------
There are two checkouts of this repo:

  PRODUCTION  D:\\AI Projects\\nifty-options-scanner
              Runs the live session. Owns the real data: recorded
              snapshots, the trade journal, decision log, state files.
              Runs released code only (master), and is NEVER modified
              while a session is in progress.

  DEVELOPMENT D:\\AI Projects\\option-scanner\\nifty-options-scanner
              Where changes are written and tested. Analyses a COPY of
              production data. Never places or tracks a real trade.

Without a marker distinguishing them, the failure modes are quiet and
expensive: editing a file in production mid-session (which supervisor.py
would pick up on its next restart, swapping logic underneath an open
position), or running analysis in development against stale data and
drawing conclusions from it.

This module makes the role explicit and lets tools refuse to do the
wrong thing in the wrong place.

The role comes from a `.workspace` file at the repo root containing
"production" or "development", or the NIFTY_WORKSPACE environment
variable. It is deliberately NOT inferred from the directory path --
paths get moved and copied, and a wrong guess here is worse than no
guess at all.
"""

import os
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).parent
MARKER_PATH = REPO_DIR / ".workspace"

PRODUCTION = "production"
DEVELOPMENT = "development"
UNKNOWN = "unknown"


def role() -> str:
    env = os.environ.get("NIFTY_WORKSPACE", "").strip().lower()
    if env in (PRODUCTION, DEVELOPMENT):
        return env
    if MARKER_PATH.exists():
        value = MARKER_PATH.read_text(encoding="utf-8").strip().lower()
        if value in (PRODUCTION, DEVELOPMENT):
            return value
    return UNKNOWN


def is_production() -> bool:
    return role() == PRODUCTION


def _git(*args) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=REPO_DIR,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_state() -> dict:
    dirty = _git("status", "--porcelain")
    # Ignore pure line-ending noise, which this repo has a lot of and
    # which never represents a real change.
    real_changes = [ln for ln in dirty.splitlines() if ln and not ln.startswith(" M ")]
    return {
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "?",
        "commit": _git("rev-parse", "--short", "HEAD") or "?",
        "dirty": bool(real_changes),
        "changed_files": len(real_changes),
    }


def data_inventory() -> dict:
    """What data this checkout actually holds -- the thing that differs."""
    logs, state = REPO_DIR / "logs", REPO_DIR / "state"
    snaps = sorted((logs / "snapshots").glob("*.jsonl.gz")) if (logs / "snapshots").exists() else []
    journal = logs / "trade_journal.jsonl"
    journal_lines = 0
    if journal.exists():
        journal_lines = sum(1 for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip())
    return {
        "snapshot_days": len(snaps),
        "snapshot_dates": [p.name.split(".")[0] for p in snaps],
        "snapshot_mb": round(sum(p.stat().st_size for p in snaps) / 1_048_576, 2),
        "journal_lines": journal_lines,
        "state_files": len(list(state.glob("*.json"))) if state.exists() else 0,
    }


def require_development(action: str = "this action"):
    """
    Refuse to mutate anything in the production checkout.

    Production runs the live session; a file changed there can be picked
    up by supervisor.py's next restart, swapping decision logic
    underneath an open position. That is the one failure this whole
    split exists to prevent.
    """
    if is_production():
        raise RuntimeError(
            f"Refusing to run {action} in the PRODUCTION checkout ({REPO_DIR}). "
            f"Make the change in development and promote it after market close."
        )


def status() -> str:
    g, d = git_state(), data_inventory()
    r = role()
    lines = [
        f"workspace : {r.upper()}" + ("  (set .workspace or NIFTY_WORKSPACE)" if r == UNKNOWN else ""),
        f"path      : {REPO_DIR}",
        f"git       : {g['branch']} @ {g['commit']}" +
        (f"  [{g['changed_files']} uncommitted file(s)]" if g["dirty"] else "  [clean]"),
        f"snapshots : {d['snapshot_days']} day(s), {d['snapshot_mb']} MB"
        + (f"  {', '.join(d['snapshot_dates'])}" if d["snapshot_dates"] else ""),
        f"journal   : {d['journal_lines']} trade(s)",
        f"state     : {d['state_files']} file(s)",
    ]
    if r == PRODUCTION and g["branch"] != "master":
        lines.append("")
        lines.append(f"  WARNING: production is on '{g['branch']}', not master. "
                     f"Live sessions should run released code only.")
    if r == PRODUCTION and g["dirty"]:
        lines.append("")
        lines.append("  WARNING: production has uncommitted changes. If supervisor.py "
                     "restarts main_live.py,\n           it will pick them up mid-session.")
    if r == DEVELOPMENT and d["snapshot_days"] == 0:
        lines.append("")
        lines.append("  NOTE: no recorded data here yet. Run `python3 sync_from_prod.py` "
                     "to copy it from production.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(status())
