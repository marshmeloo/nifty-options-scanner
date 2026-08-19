"""
Pins the 2026-08-19 fix for Dhan's EXCLUSIVE `fromDate` on
/charts/intraday.

Asking for a session "from 09:15:00" returns the session MINUS its own
first bar. Every candle fetch in this project used that natural-looking
value, so every series it had ever produced -- live and historical --
began one bar late. Worst hit were the consumers that aggregate
intraday bars into a daily candle (opening_gap.py, premarket.py), which
take day_candles[0].open as "today's open": they were reading the 09:20
open (or the 10:15 open on 60-minute candles) and, in opening_gap's
case, comparing exactly that against the prior close.

These tests are deliberately about the REQUEST this project sends, not
about Dhan's response -- the exclusivity itself is external behaviour
confirmed by direct probe (from 09:15 -> 74 bars starting 09:20; from
09:14 -> 75 bars starting 09:15) and cannot be asserted offline. What
CAN be pinned, and what actually regressed, is that no caller asks from
09:15 any more.

Run: python -m pytest tests/test_session_fetch_boundary.py -q
"""

import re
from pathlib import Path

import pytest

import dhan_source
import opening_gap
import premarket

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Read as SOURCE FILES rather than importing. main_price_action.py and
# main_price_action_banknifty.py each patch the shared
# price_action_tracker module's paths at import time and assert those
# paths are still pristine when they load, so importing both into one
# interpreter trips that guard by design (they are "own process only"
# modules -- same reason tests/test_fast_check_rewiring.py runs its
# equivalents in subprocesses). Nothing here needs them imported: the
# check is on the request string they contain.
FILES_THAT_FETCH_SESSIONS = [
    "banknifty_context.py",
    "dhan_source.py",
    "historical_source.py",
    "main_price_action.py",
    "main_price_action_banknifty.py",
    "opening_gap.py",
    "premarket.py",
]


def test_session_fetch_from_time_is_before_the_open():
    """The constant must sit strictly before 09:15, or the exclusive
    boundary still eats the opening bar."""
    assert dhan_source.SESSION_FETCH_FROM_TIME < "09:15:00"


def test_session_fetch_from_time_is_not_so_early_it_reaches_pre_open():
    """NSE runs a pre-open session 09:00-09:15. Verified by probe that
    Dhan returns no bars for it, but keeping the window tight means a
    change in that behaviour can't quietly prepend pre-open bars to
    every series."""
    assert dhan_source.SESSION_FETCH_FROM_TIME >= "09:10:00"


@pytest.mark.parametrize("filename", FILES_THAT_FETCH_SESSIONS)
def test_no_module_still_requests_candles_from_0915(filename):
    """
    The actual regression guard. A literal "09:15:00" in a source file
    is, in every case this project has, a candle-fetch `fromDate` -- and
    that is exactly the bug. Docstrings and comments are stripped first
    so the modules can still DESCRIBE the bug (several do, at length)
    without tripping the check.
    """
    src = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)      # docstrings
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    src = re.sub(r"#[^\n]*", "", src)                # comments
    assert "09:15:00" not in src, (
        f"{filename} still requests candles from 09:15:00 -- Dhan's fromDate is "
        f"exclusive, so that drops the session's first bar. Use "
        f"dhan_source.SESSION_FETCH_FROM_TIME."
    )


def test_intraday_default_from_date_uses_the_constant(monkeypatch):
    """End-to-end on the default path: calling with from_date=None must
    put SESSION_FETCH_FROM_TIME on the wire, not 09:15."""
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"timestamp": [], "open": [], "high": [], "low": [], "close": []}

    def fake_post(url, headers, json, timeout):
        seen["from"] = json["fromDate"]
        return _Resp()

    monkeypatch.setattr(dhan_source.requests, "post", fake_post)
    monkeypatch.setattr(dhan_source, "_headers", lambda: {})
    monkeypatch.setattr(dhan_source.dhan_rate_limiter, "wait_for_slot", lambda *a, **kw: None)

    dhan_source.get_nifty_intraday_candles(interval="5")

    assert seen["from"].endswith(dhan_source.SESSION_FETCH_FROM_TIME)
    assert not seen["from"].endswith("09:15:00")


def test_explicit_from_date_is_still_respected(monkeypatch):
    """The fix must not override a caller that deliberately asks for a
    different window -- orb_candle_cache.py relies on that."""
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"timestamp": [], "open": [], "high": [], "low": [], "close": []}

    monkeypatch.setattr(dhan_source.requests, "post",
                        lambda url, headers, json, timeout: (seen.update(from_=json["fromDate"]), _Resp())[1])
    monkeypatch.setattr(dhan_source, "_headers", lambda: {})
    monkeypatch.setattr(dhan_source.dhan_rate_limiter, "wait_for_slot", lambda *a, **kw: None)

    dhan_source.get_nifty_intraday_candles(interval="5", from_date="2026-08-18 11:00:00")

    assert seen["from_"] == "2026-08-18 11:00:00"


def test_opening_gap_window_starts_before_the_open():
    """opening_gap is the module the bug hurt most -- it measures the
    day's OPEN from the first intraday bar."""
    from_date, _to_date = opening_gap._window(lookback_days=10)
    assert from_date.endswith(dhan_source.SESSION_FETCH_FROM_TIME)


def test_premarket_window_starts_before_the_open():
    from_date, _to_date = premarket._previous_trading_day_window(lookback_days=10)
    assert from_date.endswith(dhan_source.SESSION_FETCH_FROM_TIME)
