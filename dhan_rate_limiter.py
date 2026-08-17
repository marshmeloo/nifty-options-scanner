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
import random
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

# Ceiling on the unlocked fallback's own queueing (see
# _respect_interval_unlocked). Bounded so a trading loop can never be
# held here indefinitely, but generous enough to actually let several
# waiters take turns rather than firing together -- with 11 processes
# competing, the whole point is that they queue.
UNLOCKED_SPACING_MAX_WAIT_SECONDS = 15.0


def _try_acquire_lock() -> bool:
    """
    True if this process now holds the lock, False if someone else does.

    Catches PermissionError alongside FileExistsError, added 2026-08-17
    after it took down a live process. On Windows, O_CREAT|O_EXCL against
    a path another process is concurrently creating/deleting raises
    PermissionError (WinError 5), not FileExistsError -- same "someone
    else has it" condition, different exception. That escaped this
    handler, propagated all the way out of wait_for_slot(), and killed
    orderflow_feed_banknifty.py outright at startup that morning: the
    Bank Nifty order-flow feed never subscribed to a single contract
    all session, so every Bank Nifty book_imbalance reading stayed None
    for the whole day. "Lock is contended" must never be able to crash
    a caller -- the whole design already treats failure to acquire as
    survivable (see wait_for_slot's MAX_ACQUIRE_WAIT_SECONDS fallback).

    OSError is the catch-all parent of both, used deliberately rather
    than naming the two: any filesystem-level reason the lock can't be
    taken right now is the same answer to the caller ("not yours, retry
    or proceed"), and a rate-limit coordination helper must not be the
    thing that takes down a trading process.
    """
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except OSError:
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


def _respect_interval_unlocked(min_interval: float):
    """
    Best-effort spacing WITHOUT holding the lock, for when acquisition
    times out under heavy contention.

    Rewritten 2026-08-17. The old behaviour here was a bare `return` --
    "proceed without it this cycle" -- which abandoned rate limiting
    entirely at exactly the moment it was most needed. That turned a
    queueing problem into a thundering herd: with demand above the
    limiter's budget every process eventually times out, and they then
    all fire at once. Measured in one real session: 2,209 timeouts
    across the process group, 53 genuine 429s on NIFTY alone, and 171
    cycles with NO chain data from either source (79 of them while real
    positions were open).

    Reading last_request_at without the lock can race -- two processes
    may compute the same slot and both fire. That is strictly better
    than the old behaviour, where ALL of them fired immediately: an
    occasional double is a small overshoot, a stampede is an outage.
    The jitter spreads out processes that timed out together, so they
    don't re-collide on the very next attempt.
    """
    # Re-check in a loop rather than sleeping once and firing. Every
    # waiter that reaches here read the SAME last_request_at, so a single
    # "sleep out the remainder" would have them all wake and fire
    # together -- measurably barely better than the old bare `return`
    # (11 concurrent waiters finished in 0.40s vs 0.30s in a test that
    # caught exactly this). Looping means each one re-reads the timestamp
    # the previous one wrote and queues behind it properly.
    deadline = time.monotonic() + UNLOCKED_SPACING_MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        elapsed = time.time() - _read_last_request_at()
        if elapsed >= min_interval:
            break
        # Jittered re-check so waiters don't march in lockstep and
        # re-collide on the same tick.
        time.sleep(min(min_interval - elapsed, min_interval / 4) + random.uniform(0.0, min_interval / 4))

    try:
        STATE_PATH.write_text(json.dumps({"last_request_at": time.time()}))
    except OSError:
        pass  # observability only -- never block the request on this


def wait_for_slot(min_interval: float = None):
    """
    Block until it's this process's turn to make a Dhan request,
    coordinating with every other process sharing this account. Call
    immediately before every Dhan HTTP request.

    Never blocks a trading loop indefinitely: if the lock can't be
    acquired within MAX_ACQUIRE_WAIT_SECONDS, falls back to best-effort
    unlocked spacing (see _respect_interval_unlocked) rather than either
    waiting forever OR firing unthrottled.

    IMPORTANT, and not something this function can fix on its own: it
    can only SHAPE demand, never create capacity. Measured 2026-08-17
    with 11 live processes sharing one account -- roughly 58 chain
    requests/min while trades are open against a ~17/min budget at
    MIN_INTERVAL_SECONDS=3.5 (itself already above Dhan's documented
    20/min). Sustained 3.4x oversubscription means requests queue and
    some cycles will still be slow or fail no matter how fair the
    queueing is. The durable fix is fewer/cheaper requests -- see
    BACKLOG.md.
    """
    min_interval = min_interval if min_interval is not None else MIN_INTERVAL_SECONDS
    acquire_deadline = time.monotonic() + MAX_ACQUIRE_WAIT_SECONDS

    acquired = False
    while time.monotonic() < acquire_deadline:
        if _try_acquire_lock():
            acquired = True
            break
        _clear_stale_lock()
        # Jittered poll: a fixed sleep marches every waiter in lockstep,
        # so they keep colliding on the same retry tick.
        time.sleep(random.uniform(0.02, 0.08))

    if not acquired:
        log.info("  [rate limiter] lock contended, spacing this request without it")
        _respect_interval_unlocked(min_interval)
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
