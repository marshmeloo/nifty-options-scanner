"""
Cross-PROCESS rate limiting for Dhan's option-chain API.

WHY THIS EXISTS
---------------
Dhan's documented limit (1 request/3s) is per ACCOUNT/TOKEN, not per
process. Three separate processes share one Dhan account with no
coordination between them: main_live.py (30s poll + a 5s fast-check),
main_condor.py (5 min), main_directional_spread.py (30s). Each one's own
in-memory cooldown bookkeeping (resilient_source._last_failure) only
sees its OWN requests -- it has no idea the other two processes exist.

Confirmed live on 2026-07-30 and 2026-07-31: 429s on main_condor.py,
then on 2026-07-31 the SAME issue reached main_live.py directly --
48 of 72 failure-related log lines that day landed while a real trade
was open, including the 5s fast-check itself failing outright ("Both
dhan and nse sources are in cooldown"). Nothing broke only because that
trade never approached its stop/target during the gaps; that's luck,
not the system working as designed. A momentum strategy's stop/target
check silently not running during a real approach is a genuine risk to
the very P&L data this project's whole measurement effort depends on
being trustworthy.

HOW IT WORKS
------------
A lock file (atomic create, not a true OS mutex -- doesn't need to be,
see below) guards a small shared state file recording the wall-clock
time of the last Dhan request from ANY process. Before making a
request, a process acquires the lock, checks how long it's been since
the last request (by any process), sleeps out the remainder of
MIN_INTERVAL_SECONDS if needed, records its own request time, and
releases the lock.

Using `os.O_CREAT | os.O_EXCL` for the lock is a standard, portable
advisory-lock pattern: the OS guarantees that exact combination either
creates the file or fails atomically, so two processes can never both
believe they hold it. A STALE lock (owner crashed mid-hold) is detected
by age and force-cleared rather than deadlocking every process sharing
this account forever.

Wall-clock time (`time.time()`), not `time.monotonic()`, is used
deliberately -- monotonic's epoch is implementation-defined and isn't
guaranteed comparable across separate processes, which would silently
make the whole cross-process comparison meaningless.

WHAT THIS DOES NOT DO
---------------------
It does not fix the NSE fallback tier being blocked by tightened bot
detection (see BACKLOG.md) -- that's a different, external problem this
can't touch. It only ensures Dhan itself, the primary tier, isn't
rate-limited by our own combined traffic.
"""

import os
import time
import json
import logging
from pathlib import Path

log = logging.getLogger("nifty_scanner")

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
LOCK_PATH = STATE_DIR / "dhan_rate_limiter.lock"
STATE_PATH = STATE_DIR / "dhan_rate_limiter.json"

# Slightly above Dhan's documented 3s/request limit, shared across ALL
# processes hitting this account -- not per-process. Deliberately a
# little conservative rather than exactly 3.0: Dhan's own enforcement
# window and our wall-clock timing won't line up perfectly, and the
# cost of being 0.5s too cautious is negligible next to the cost of
# another 429 storm.
MIN_INTERVAL_SECONDS = 3.5

# If a lock file is older than this, its owner almost certainly crashed
# or was killed mid-request (matches this project's general assumption,
# see supervisor.py, that any process here can be force-terminated at
# any time) -- clear it rather than let every process sharing this
# account deadlock forever waiting for a lock nobody will ever release.
STALE_LOCK_SECONDS = 10.0

# Never block a request indefinitely even if lock acquisition itself is
# struggling (e.g. very heavy contention) -- proceed anyway past this
# point rather than let rate-limit coordination become a bigger outage
# than the 429s it exists to prevent.
MAX_ACQUIRE_WAIT_SECONDS = 8.0


def _try_acquire_lock() -> bool:
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _clear_stale_lock():
    try:
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age > STALE_LOCK_SECONDS:
            LOCK_PATH.unlink()
            log.info(f"  [rate limiter] cleared a stale lock ({age:.0f}s old, owner likely crashed)")
    except OSError:
        pass  # already gone, or a race with another process clearing it -- fine either way


def _read_last_request_at() -> float:
    if not STATE_PATH.exists():
        return 0.0
    try:
        return json.loads(STATE_PATH.read_text()).get("last_request_at", 0.0)
    except (json.JSONDecodeError, OSError):
        return 0.0


def wait_for_slot(min_interval: float = None):
    """
    Block until it's this process's turn to make a Dhan request,
    coordinating with every other process sharing this account. Call
    immediately before every Dhan HTTP request.

    Fails safe in every direction: if the lock can't be acquired within
    MAX_ACQUIRE_WAIT_SECONDS, proceeds without it rather than blocking a
    trading loop forever over a coordination mechanism.
    """
    min_interval = min_interval if min_interval is not None else MIN_INTERVAL_SECONDS
    acquire_deadline = time.monotonic() + MAX_ACQUIRE_WAIT_SECONDS

    acquired = False
    while time.monotonic() < acquire_deadline:
        if _try_acquire_lock():
            acquired = True
            break
        _clear_stale_lock()
        time.sleep(0.05)

    if not acquired:
        log.info("  [rate limiter] could not acquire lock in time, proceeding without it this cycle")
        return

    try:
        elapsed = time.time() - _read_last_request_at()
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        STATE_PATH.write_text(json.dumps({"last_request_at": time.time()}))
    finally:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass  # already gone (e.g. force-cleared as stale by another process) -- fine
