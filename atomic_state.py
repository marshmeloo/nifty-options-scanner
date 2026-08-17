"""
One shared atomic-write helper, used by every module that persists state
to a JSON file (trade_tracker, condor_tracker, trade_staging, opening_gap,
dhan_source's baseline/IV-history/live-state caches). Writes to a temp
file in the same directory, then os.replace()s it over the real path --
os.replace is atomic on every platform this runs on (POSIX rename,
Windows MoveFileEx with replace), so a process killed at any point
during the write -- including a forced kill from supervisor.py during a
restart -- leaves either the OLD file fully intact or the NEW one fully
intact, never a half-written/corrupted one.

Added 2026-07-24 alongside supervisor.py: once something can forcibly
terminate main_live.py mid-cycle, every state write it might be in the
middle of needs this guarantee, not just the trade state file.

WINDOWS CONTENTION (added 2026-08-17)
--------------------------------------
Two problems showed up in a real session, both Windows-specific and both
absent on POSIX:

  1. os.replace() FAILS if any other process currently has the
     destination file open, even just for reading (Windows shares files
     far less permissively than POSIX, where rename over an open file is
     fine). state/orderflow.json is written every 2s by orderflow_feed.py
     while several strategy processes read it every cycle, so collisions
     are routine, not rare: 15 "WinError 5: Access is denied" write
     failures in one session. Each one silently dropped that write, so
     readers kept seeing an older book than the feed actually had.
  2. A fixed ".tmp" suffix means two processes writing the SAME path
     would fight over one temp file. Not currently possible (each state
     file has a single writer), but a per-process temp name costs
     nothing and removes the footgun entirely.

Fixed by retrying the replace briefly (the reader's handle is open for
microseconds -- a few short retries clear essentially all of it) and by
making the temp filename process-unique. The atomicity guarantee above
is unchanged: os.replace is still what publishes the file, still all-or-
nothing, and a crash mid-retry leaves the old file intact exactly as
before.
"""

import json
import os
import time
from pathlib import Path

# Total time to keep retrying a blocked replace before giving up. Sized
# against what it's actually waiting for -- another process's read of a
# small JSON file, i.e. sub-millisecond -- not against anything slow.
# Deliberately short: a state write that can't land promptly is better
# abandoned (the next cycle writes again seconds later) than allowed to
# stall a trading loop.
REPLACE_RETRY_SECONDS = 0.5
REPLACE_RETRY_SLEEP = 0.02


def atomic_write_json(path, data, **json_kwargs):
    path = Path(path)
    # Process-unique temp name: two writers of the same path can never
    # clobber each other's partial temp file.
    tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, **json_kwargs))

    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            # Windows: destination is open in another process right now.
            # Retry -- that handle is short-lived.
            if time.monotonic() >= deadline:
                # Don't leave the temp file behind on give-up; the caller
                # sees the exception and decides (callers that treat state
                # writes as observability already catch and continue).
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
            time.sleep(REPLACE_RETRY_SLEEP)
