"""
Process supervisor for main_live.py -- relaunches it immediately if it
exits (crash) or goes silent for too long (frozen -- e.g. Windows Quick
Edit Mode pausing the console, or a genuine hang). Built directly in
response to the 2026-07-24 incident: main_live.py went silent for 4+
hours with a live trade open and nobody noticed until reviewing the log
afterward.

This is the strongest protection pure software can offer against that
failure mode. It shrinks the exposure window from "however long until a
human notices" down to roughly one health-check interval (tens of
seconds). It does NOT eliminate the window entirely -- nothing running
only on this machine can protect an open position across the gap
between "the process just died" and "the supervisor notices and
restarts it." Closing that gap completely requires a protective order
sitting on the exchange itself (a broker-side stop-loss), which this
project deliberately does not do -- everything here is decision support,
not order execution. If that's a gap you want closed further, that's a
real, separate decision to make -- this supervisor is the strongest
thing achievable without crossing that line.

How it works, every HEALTH_CHECK_INTERVAL_SECONDS:
  1. Is the main_live.py subprocess still alive (hasn't exited)?
  2. Has today's log file been modified recently (not frozen)?
If either check fails during market hours, the subprocess is killed (if
still alive) and relaunched immediately. Every restart is logged, with
its reason, to logs/supervisor_YYYYMMDD.log -- a restart is itself
something you want a clear record of, not a silent recovery.

State safety: every module that persists trade/position state now
writes atomically (atomic_state.py, added alongside this file) -- a
forced kill mid-write can never corrupt state/open_trades.json or
similar files; it either lands before or after a complete write, never
in between.

Run this INSTEAD of running main_live.py directly:
  set DHAN_CLIENT_ID=...
  set DHAN_ACCESS_TOKEN=...
  python3 supervisor.py
"""

import subprocess
import sys
import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

HEALTH_CHECK_INTERVAL_SECONDS = 20   # how often the supervisor checks in
STALE_THRESHOLD_SECONDS = 90         # 3x the main loop's 30s poll interval -- comfortable margin, not a hair trigger
STARTUP_GRACE_SECONDS = 45           # don't judge staleness until the child has had time to start and log its first cycle

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
SUPERVISOR_LOG_PATH = LOG_DIR / f"supervisor_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler(SUPERVISOR_LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("supervisor")


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def todays_main_log_path() -> Path:
    return LOG_DIR / f"nifty_scan_{datetime.now().strftime('%Y%m%d')}.log"


def start_child() -> subprocess.Popen:
    log.info(f"[{datetime.now().strftime('%H:%M:%S')}] Starting main_live.py...")
    return subprocess.Popen([sys.executable, "main_live.py"], cwd=BASE_DIR)


def restart_reason(proc: subprocess.Popen, started_at: datetime):
    """Returns a reason string if the child needs restarting, else None."""
    exit_code = proc.poll()
    if exit_code is not None:
        return f"process exited (code {exit_code})"

    if not market_is_open():
        return None  # nothing to check outside market hours

    if (datetime.now() - started_at).total_seconds() < STARTUP_GRACE_SECONDS:
        return None  # give it time to log its first cycle before judging staleness

    log_path = todays_main_log_path()
    if not log_path.exists():
        return "market is open but no log file exists yet"

    age = time.time() - log_path.stat().st_mtime
    if age > STALE_THRESHOLD_SECONDS:
        return f"log hasn't updated in {int(age)}s (process may be frozen)"

    return None


def stop_child(proc: subprocess.Popen):
    if proc.poll() is not None:
        return  # already exited
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.info("  Process didn't terminate cleanly within 10s, killing it.")
        proc.kill()
        proc.wait()


def run_forever():
    log.info("Supervisor started. Will relaunch main_live.py on crash or freeze. Ctrl+C to stop both.")
    while True:
        proc = start_child()
        started_at = datetime.now()

        while True:
            time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
            reason = restart_reason(proc, started_at)
            if reason:
                log.info(f"[{datetime.now().strftime('%H:%M:%S')}] RESTARTING main_live.py -- reason: {reason}")
                stop_child(proc)
                break  # exit inner loop -> relaunch in outer loop


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        log.info("Supervisor stopped by user.")
