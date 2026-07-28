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
"""

import json
import os
from pathlib import Path


def atomic_write_json(path, data, **json_kwargs):
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, **json_kwargs))
    os.replace(tmp_path, path)
