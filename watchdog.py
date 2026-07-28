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

import time
from datetime import datetime, time as dtime
from pathlib import Path

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

STALE_CHECK_INTERVAL_SECONDS = 60
STALE_THRESHOLD_SECONDS = 180  # 3x the main loop's 30s poll interval -- comfortable margin, not a hair trigger

LOG_DIR = Path(__file__).parent / "logs"


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
        time.sleep(STALE_CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
