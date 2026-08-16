"""
Standalone watchdog: run this in a SEPARATE terminal/process from
main_live.py. It cannot fix a frozen main process (nothing can watch
itself from the inside if it's the thing that's stuck), but it gives
independent visibility into exactly the failure mode that caused the
2026-07-24 incident: main_live.py went completely silent for 4+ hours
during market hours -- no error, no new log lines, process possibly
still "running" -- and nobody noticed until reviewing the log after the
fact.

What it does: checks logs/nifty_scan_YYYYMMDD.log's last-modified time
every STALE_CHECK_INTERVAL_SECONDS during market hours. If it hasn't
been updated in longer than STALE_THRESHOLD_SECONDS (comfortably more
than one poll cycle), prints a loud warning. It does NOT restart
main_live.py itself -- that's a deliberate choice; auto-restarting a
process that might have a live option position open, without knowing
why it died, is its own risk. This is meant to get a human's attention
quickly, not to paper over the underlying problem.

Likely root causes worth checking if this ever fires (found while
investigating the 2026-07-24 incident):
  - Windows "Quick Edit Mode" in cmd.exe / PowerShell consoles: if you
    click or select text in the console window, Windows PAUSES the
    process until you press Escape or right-click to deselect. The
    process is still alive and will resume instantly once unblocked,
    but produces zero output the entire time it's paused -- this matches
    "no error, looked like it was running but wasn't" exactly. Fix:
    right-click the console title bar -> Properties -> uncheck "Quick
    Edit Mode", or run from Windows Terminal instead of legacy cmd.exe
    (Windows Terminal doesn't have this behavior by default).
  - The machine went to sleep/hibernated (laptop lid closed, power plan).
  - A genuine network-level stall that somehow outlasted every request's
    explicit timeout (rare, but not impossible depending on OS/network
    stack behavior) -- if this recurs even with Quick Edit Mode ruled
    out, that's worth a deeper look at the specific call that was
    in-flight when it happened.

Run:
  python3 watchdog.py
"""

import json
import time
from datetime import datetime, time as dtime
from pathlib import Path

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

STALE_CHECK_INTERVAL_SECONDS = 60
STALE_THRESHOLD_SECONDS = 180  # 3x the main loop's 30s poll interval -- comfortable margin, not a hair trigger

LOG_DIR = Path(__file__).parent / "logs"

# decision_log.jsonl going stale is a DIFFERENT failure mode from the scan
# log going stale (the 2026-07-24 incident above): the process can keep
# scanning and logging plain-text lines to nifty_scan_*.log completely
# normally -- so the check above reports OK -- while decision_log.jsonl
# itself silently stops growing, because that write happens later in the
# same cycle. This is exactly what happened from 2026-07-29 onward: found
# 2026-08-16 while investigating whether there was enough order-flow data
# to analyse, and discovered decision_log.jsonl (the file meant to
# accumulate it) hadn't recorded a single new cycle in over two weeks,
# despite nifty_scan_*.log showing completely normal activity across every
# one of those days. The exact original trigger traces to a since-fixed
# git mistake (the file was accidentally committed early on, then
# untracked in commit 08b2f4d on 2026-07-29 -- see that commit and
# BACKLOG.md), but the write path itself was directly re-tested afterward
# and works correctly, so there's no known reason for this to recur. This
# check exists so that IF it ever does, it's a loud warning within minutes
# during market hours, not a silent multi-week gap discovered by accident.
DECISION_LOG_PATHS = (
    ("NIFTY", LOG_DIR / "decision_log.jsonl"),
    ("Bank Nifty", LOG_DIR / "decision_log_banknifty.jsonl"),
)


def _last_line_timestamp(path: Path):
    """
    The timestamp of the last recorded cycle in a decision_log*.jsonl file,
    or None if the file is absent/empty/unparseable. Reads only the tail of
    the file rather than the whole thing -- these files run tens of MB, and
    only the last line matters here.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    chunk_size = 65536
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        read_size = min(chunk_size, size)
        while True:
            f.seek(size - read_size)
            data = f.read(read_size)
            # Strip the file's own trailing newline(s) first -- otherwise a
            # window that ends in "...\n" looks like it found a real line
            # boundary (a split of length > 1) when it's only seen the
            # empty piece after that trailing newline, not an actual
            # earlier line. Only a "\n" found AFTER that strip marks a
            # genuine boundary between two records (JSON escapes any "\n"
            # that appears inside a string value, so a raw newline byte
            # can only ever be a JSONL record separator).
            stripped = data.rstrip(b"\n")
            newline_idx = stripped.rfind(b"\n")
            if newline_idx != -1 or read_size >= size:
                last_line = stripped[newline_idx + 1:]
                break
            read_size = min(read_size * 4, size)
    try:
        record = json.loads(last_line)
        return datetime.fromisoformat(record["timestamp"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def decision_log_check_once() -> str:
    """Returns a human-readable status string for both indices' decision logs."""
    if not market_is_open():
        return f"[{datetime.now().strftime('%H:%M:%S')}] decision_log: market closed -- not monitoring."

    parts = []
    now = datetime.now()
    for label, path in DECISION_LOG_PATHS:
        last_ts = _last_line_timestamp(path)
        if last_ts is None:
            parts.append(f"*** WARNING *** {label} decision_log: no readable cycle found at {path.name}")
            continue
        age_seconds = (now - last_ts).total_seconds()
        if age_seconds > STALE_THRESHOLD_SECONDS:
            parts.append(
                f"*** WARNING *** {label} decision_log: last cycle recorded {int(age_seconds)}s ago "
                f"(threshold {STALE_THRESHOLD_SECONDS}s) -- the scan log can look perfectly healthy while "
                f"this silently stops growing (see this file's module docstring)."
            )
        else:
            parts.append(f"{label} decision_log OK ({int(age_seconds)}s ago)")
    return f"[{now.strftime('%H:%M:%S')}] " + "  |  ".join(parts)


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def todays_log_path() -> Path:
    return LOG_DIR / f"nifty_scan_{datetime.now().strftime('%Y%m%d')}.log"


def check_once() -> str:
    """Returns a human-readable status string for this check."""
    if not market_is_open():
        return f"[{datetime.now().strftime('%H:%M:%S')}] Market closed -- not monitoring."

    log_path = todays_log_path()
    if not log_path.exists():
        return (
            f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: market is open but "
            f"{log_path.name} doesn't exist yet -- is main_live.py running at all?"
        )

    age_seconds = time.time() - log_path.stat().st_mtime
    if age_seconds > STALE_THRESHOLD_SECONDS:
        return (
            f"[{datetime.now().strftime('%H:%M:%S')}] *** WARNING ***  {log_path.name} hasn't been "
            f"updated in {int(age_seconds)}s (threshold {STALE_THRESHOLD_SECONDS}s) during market hours. "
            f"main_live.py may have crashed, frozen, or been killed. See this file's docstring for likely "
            f"causes (Windows Quick Edit Mode is the most common one) -- check the process and the terminal "
            f"it's running in."
        )
    return f"[{datetime.now().strftime('%H:%M:%S')}] OK -- log updated {int(age_seconds)}s ago."


def run_forever():
    print("Watchdog started -- monitoring main_live.py's log freshness during market hours. Ctrl+C to stop.")
    while True:
        print(check_once())
        print(decision_log_check_once())
        time.sleep(STALE_CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
