"""
Tests for interrupted-session recovery being idempotent.

The defect these pin, found by auditing the FULL journal history rather
than a single session: `settle_stale_trades()` journalled every stale
trade but never cleared `state["trades"]`, leaving that to the caller.
Anything interrupting the process between journalling and the caller's
save left those trades still marked OPEN on disk, so the next start
recovered and journalled them all over again.

That really happened. Eight trades from the 2026-07-23 session were
journalled twice on 2026-07-24 -- once at 00:39:42 (market shut, no quote
available, `exit_price_estimated: true`) and again at 07:28:46 (fresh
quote, slightly different exit prices) -- double-counting Rs -1,976.

It matters beyond tidiness: compute_risk_state() sums today's journalled
P&L to drive the daily-loss circuit breaker, so duplicates would trip the
breaker early on phantom losses.

Run: python -m pytest tests/ -q
"""

import json

import pytest

import config
import trade_tracker as tt


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Point the tracker at a throwaway journal."""
    path = tmp_path / "trade_journal.jsonl"
    monkeypatch.setattr(tt, "JOURNAL_PATH", path)
    return path


def _open_trade(trade_id="24050.0_PE_20260723125449", entry=58.75):
    return {
        "id": trade_id,
        "strike": 24050.0,
        "option_type": "PE",
        "expiry": "2026-07-28",
        "opened_at": "2026-07-23T12:54:49",
        "entry": entry,
        "target": entry * 1.6,
        "stop": entry * 0.7,
        "lots": 1,
        "status": "OPEN",
        "current_ltp": entry * 0.8,
        "reason_tags": ["long_buildup"],
    }


def _read(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().split("\n") if line]


# --------------------------------------------------------------------------
# The core fix
# --------------------------------------------------------------------------

def test_settle_stale_trades_clears_state(journal):
    """
    Consistency with force_close_end_of_day(), which has always cleared
    state itself. Leaving it to the caller is what opened the window.
    """
    state = {"date": "2026-07-23", "trades": [_open_trade()]}
    tt.settle_stale_trades(state)

    assert state["trades"] == []
    assert len(_read(journal)) == 1


def test_recovery_run_twice_journals_once(journal):
    """
    The exact 2026-07-24 sequence: recover with no snapshot (market shut),
    then recover again later with a fresh one. Second pass must be a no-op.
    """
    trade = _open_trade()

    first = tt.settle_stale_trades({"date": "2026-07-23", "trades": [trade]})
    assert len(first) == 1

    # Simulate the state write never landing -- the trade is still OPEN.
    second = tt.settle_stale_trades({"date": "2026-07-23", "trades": [dict(trade)]})
    assert second == []

    entries = _read(journal)
    assert len(entries) == 1, f"expected one journal entry, got {len(entries)}"


def test_distinct_trades_are_all_recovered(journal):
    """The dedup guard must key on trade id, not suppress everything."""
    state = {"date": "2026-07-23", "trades": [
        _open_trade("23950.0_PE_20260723121524"),
        _open_trade("23900.0_PE_20260723122400"),
        _open_trade("23850.0_PE_20260723125016"),
    ]}
    recovered = tt.settle_stale_trades(state)

    assert len(recovered) == 3
    assert len({e["id"] for e in _read(journal)}) == 3


def test_reopened_same_strike_later_is_not_suppressed(journal):
    """
    Trade ids embed an entry timestamp, so a genuinely new trade on the
    same strike must still journal. Guarding against over-blocking.
    """
    tt.settle_stale_trades({"date": "2026-07-23", "trades": [
        _open_trade("24050.0_PE_20260723125449"),
    ]})
    tt.settle_stale_trades({"date": "2026-07-24", "trades": [
        _open_trade("24050.0_PE_20260724110417"),   # different session
    ]})

    assert len(_read(journal)) == 2


# --------------------------------------------------------------------------
# Downstream: the circuit breaker must not double-count
# --------------------------------------------------------------------------

def test_daily_loss_ignores_duplicate_journal_entries(monkeypatch):
    """
    Real duplicates already exist in the user's journal from before the
    fix, so the reader has to stay robust to them rather than assume a
    clean file.
    """
    from datetime import date
    today = date.today().isoformat()
    dupe = {
        "id": "23850.0_PE_20260723125016",
        "closed_at": f"{today}T00:39:42",
        "pnl_inr": -1800.50,
    }
    near_dupe = dict(dupe, closed_at=f"{today}T07:28:46", pnl_inr=-1868.75)

    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [dupe, near_dupe])
    _, loss_pct = tt.compute_risk_state({"trades": []})

    # Counted once (the later entry wins), not summed to -3,669.25.
    expected = 1868.75 / config.TOTAL_CAPITAL * 100
    assert loss_pct == pytest.approx(expected, abs=0.01)


def test_daily_loss_still_sums_genuinely_distinct_trades(monkeypatch):
    from datetime import date
    today = date.today().isoformat()
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: [
        {"id": "a", "closed_at": f"{today}T10:43:53", "pnl_inr": -1595.75},
        {"id": "b", "closed_at": f"{today}T14:05:31", "pnl_inr": -1586.00},
    ])
    _, loss_pct = tt.compute_risk_state({"trades": []})

    expected = (1595.75 + 1586.00) / config.TOTAL_CAPITAL * 100
    assert loss_pct == pytest.approx(expected, abs=0.01)


def test_journaled_trade_ids_survives_a_corrupt_line(journal):
    """A half-written line must not blow up recovery at startup."""
    journal.write_text(
        '{"id": "good_1", "pnl_inr": -100}\n'
        '{"id": "truncated", "pnl_i\n'
        '{"id": "good_2", "pnl_inr": -200}\n'
    )
    assert tt._journaled_trade_ids() == {"good_1", "good_2"}
