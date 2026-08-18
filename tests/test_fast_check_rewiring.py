"""
Tests for the 2026-08-18 fast-check rewiring: all four momentum
processes' check_open_trades_fast() now build a lightweight snapshot via
dhan_source.get_fast_check_snapshot() (orderflow-first, /marketfeed/ltp
fallback) instead of re-fetching a full option chain -- see
dhan_source.get_fast_check_snapshot's docstring and dhan_rate_limiter's
module docstring for why.

Also pins Sentinel's re-enablement: main_live_sentinel.py and
main_live_banknifty_sentinel.py had their fast check deliberately
REMOVED from the polling loop on 2026-08-17 (too expensive), then
RE-ENABLED on 2026-08-18 once it stopped being expensive. These tests
confirm both the function's own behaviour AND that run_forever() calls
it again.

WHY SUBPROCESSES FOR THREE OF THE FOUR: main_live_banknifty.py,
main_live_sentinel.py, and main_live_banknifty_sentinel.py each patch
the SHARED trade_tracker module's path constants at IMPORT TIME (to
route to their own journal/state files), and each one asserts those
paths are STILL pristine defaults at the moment IT imports -- see e.g.
main_live_banknifty.py's `assert tt.JOURNAL_PATH.name ==
"trade_journal.jsonl"` right after its own imports. That is a real,
deliberate safety check (these files document themselves as "own
process only", never meant to coexist with a sibling variant in one
interpreter) and it correctly fires if a SECOND one of these files is
imported into the same process after a first one already patched
trade_tracker's shared state -- exactly what a naive test importing
several of them together would do. Running each in its own subprocess
is the same isolation these files get for real in production (one OS
process per variant), not a workaround for a test-only problem.
main_live.py itself does no such patching (it IS trade_tracker's
default), so it alone is safe to import directly here, matching how
tests/test_main_live.py and tests/test_strike_range_protection.py
already do.

Run: python -m pytest tests/test_fast_check_rewiring.py -q
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import main_live


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _state_with_trades(*positions):
    """positions: (strike, option_type) pairs."""
    return {"trades": [{"strike": s, "option_type": ot} for s, ot in positions]}


# --------------------------------------------------------------------------
# main_live.py (Anchor NIFTY) -- in-process, matches existing test convention
# --------------------------------------------------------------------------

def test_nifty_nothing_open_returns_without_any_snapshot_call(monkeypatch):
    calls = []
    monkeypatch.setattr(main_live.dhan_source, "get_fast_check_snapshot", lambda *a, **kw: calls.append(1))
    main_live.check_open_trades_fast({"trades": []}, expiry="2026-08-20")
    assert calls == []


def test_nifty_open_trades_passed_as_strike_option_type_pairs_with_default_symbol(monkeypatch):
    seen = {}

    def fake_snapshot(positions, expiry, symbol="NIFTY"):
        seen["positions"] = set(positions)
        seen["expiry"] = expiry
        seen["symbol"] = symbol
        import models
        return models.MarketSnapshot(symbol=symbol, spot=0.0, vwap=0.0, pcr=0.0, chain=[])

    monkeypatch.setattr(main_live.dhan_source, "get_fast_check_snapshot", fake_snapshot)
    monkeypatch.setattr(main_live.tt, "save_open_trades", lambda state: None)

    state = _state_with_trades((24500.0, "CE"), (24300.0, "PE"))
    main_live.check_open_trades_fast(state, expiry="2026-08-20")

    assert seen["positions"] == {(24500.0, "CE"), (24300.0, "PE")}
    assert seen["expiry"] == "2026-08-20"
    assert seen["symbol"] == "NIFTY"


def test_nifty_snapshot_fetch_failure_is_caught_not_raised(monkeypatch):
    def fake_snapshot(*a, **kw):
        raise ConnectionError("boom")
    monkeypatch.setattr(main_live.dhan_source, "get_fast_check_snapshot", fake_snapshot)

    state = _state_with_trades((24500.0, "CE"))
    main_live.check_open_trades_fast(state, expiry="2026-08-20")  # must not raise


def test_nifty_a_real_close_still_saves_and_journals(monkeypatch):
    """
    End-to-end through the real trade_tracker.update_open_trades(), not
    just checking the call arguments -- confirms the lightweight snapshot
    actually plugs into the existing close/journal/save pipeline.

    Runs inside trade_tracker.journal_writes_disabled() -- an EARLIER
    version of this test forgot that update_open_trades() calls the real
    _append_journal() on any close, and only mocked save_open_trades(),
    which left the actual JOURNAL_PATH write live. Running the full
    suite once against the PRODUCTION checkout wrote this exact fake WIN
    trade for real into prod's live trade_journal.jsonl (caught and
    removed 2026-08-18 -- see git history / incident notes). Mocking
    save_open_trades() is still correct (state persistence isn't what
    this test is verifying), but journal_writes_disabled() is now what
    stops the journal side effect, not an oversight relying on nothing
    else in the call path touching disk.
    """
    import models

    saved = []
    monkeypatch.setattr(main_live.tt, "save_open_trades", lambda state: saved.append(state))

    def fake_snapshot(positions, expiry, symbol="NIFTY"):
        quote = models.OptionQuote(
            symbol=symbol, expiry=expiry, strike=24500.0, option_type="CE",
            ltp=200.0, oi=0, oi_change_pct=0.0, volume=0, iv=0.0, iv_percentile=0.0,
        )
        return models.MarketSnapshot(symbol=symbol, spot=0.0, vwap=0.0, pcr=0.0, chain=[quote])
    monkeypatch.setattr(main_live.dhan_source, "get_fast_check_snapshot", fake_snapshot)

    state = {
        "trades": [{"strike": 24500.0, "option_type": "CE", "entry": 100.0,
                   "target": 150.0, "stop": 80.0, "lots": 1, "entry_time": "09:20:00"}]
    }
    with main_live.tt.journal_writes_disabled():
        main_live.check_open_trades_fast(state, expiry="2026-08-20")

    assert len(saved) == 1
    assert state["trades"] == [], "the winning trade should have closed"


def test_nifty_run_forever_still_references_check_open_trades_fast():
    """Sanity check for the structural technique used against the other
    three modules below, against a file that was NEVER disabled."""
    assert "check_open_trades_fast" in main_live.run_forever.__code__.co_names


# --------------------------------------------------------------------------
# main_live_banknifty.py, main_live_sentinel.py, main_live_banknifty_sentinel.py
# -- each in its own subprocess; see module docstring for why.
# --------------------------------------------------------------------------

_SUBPROCESS_SCRIPT = textwrap.dedent("""
    import json
    import {module} as mod
    import models

    seen = {{}}
    def fake_snapshot(positions, expiry, symbol="NIFTY"):
        seen["positions"] = sorted(positions)
        seen["expiry"] = expiry
        seen["symbol"] = symbol
        return models.MarketSnapshot(symbol=symbol, spot=0.0, vwap=0.0, pcr=0.0, chain=[])

    mod.dhan_source.get_fast_check_snapshot = fake_snapshot
    mod.tt.save_open_trades = lambda state: None

    state = {{"trades": [{{"strike": 24500.0, "option_type": "CE"}},
                         {{"strike": 24300.0, "option_type": "PE"}}]}}
    mod.check_open_trades_fast(state, expiry="2026-08-20")

    # A second, independent call with nothing open must short-circuit
    # before touching fake_snapshot again -- if it didn't, this would
    # overwrite `seen` with positions=[] and the assertions below on the
    # FIRST call's recorded values would fail, catching the regression.
    mod.check_open_trades_fast({{"trades": []}}, expiry="2026-08-20")

    print(json.dumps({{
        "positions": seen.get("positions"),
        "expiry": seen.get("expiry"),
        "symbol": seen.get("symbol"),
        "references_fast_check_in_loop":
            "check_open_trades_fast" in mod.run_forever.__code__.co_names,
    }}))
""")


def _run_fast_check_in_subprocess(module_name: str) -> dict:
    script = _SUBPROCESS_SCRIPT.format(module=module_name)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 0, (
        f"{module_name} subprocess failed (exit {proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_banknifty_check_open_trades_fast_uses_banknifty_symbol():
    result = _run_fast_check_in_subprocess("main_live_banknifty")
    assert result["positions"] == [[24300.0, "PE"], [24500.0, "CE"]]
    assert result["expiry"] == "2026-08-20"
    assert result["symbol"] == "BANKNIFTY"
    assert result["references_fast_check_in_loop"] is True


def test_sentinel_check_open_trades_fast_uses_nifty_symbol_and_is_wired_into_the_loop():
    """The re-enablement pin: as of 2026-08-17 this module's run_forever()
    loop did NOT call check_open_trades_fast at all (deliberately
    removed). references_fast_check_in_loop=True here is the regression
    guard for that having been reversed on 2026-08-18."""
    result = _run_fast_check_in_subprocess("main_live_sentinel")
    assert result["positions"] == [[24300.0, "PE"], [24500.0, "CE"]]
    assert result["symbol"] == "NIFTY"
    assert result["references_fast_check_in_loop"] is True


def test_banknifty_sentinel_check_open_trades_fast_uses_banknifty_symbol_and_is_wired_into_the_loop():
    result = _run_fast_check_in_subprocess("main_live_banknifty_sentinel")
    assert result["positions"] == [[24300.0, "PE"], [24500.0, "CE"]]
    assert result["symbol"] == "BANKNIFTY"
    assert result["references_fast_check_in_loop"] is True
