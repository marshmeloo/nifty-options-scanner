"""
Tests for price_action_tracker.py's position lifecycle: open, mark-to-
market against target/stop, real-expiry settlement (including the
missing-quote intrinsic-value fallback), and R-multiple excursion
tracking.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import pytest

import price_action_tracker as pat
from models import MarketSnapshot, OptionQuote, Setup, TradePlan


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    state_path = tmp_path / "price_action_position.json"
    journal_path = tmp_path / "price_action_journal.jsonl"
    monkeypatch.setattr(pat, "STATE_PATH", state_path)
    monkeypatch.setattr(pat, "JOURNAL_PATH", journal_path)
    return state_path, journal_path


def _quote(strike, opt, ltp, bid=None, ask=None):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-07", strike=strike, option_type=opt,
        ltp=ltp, oi=5000, oi_change_pct=0.0, volume=500, iv=14.0, iv_percentile=50.0,
        bid=bid, ask=ask,
    )


def _snapshot(ts, chain, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=chain, timestamp=ts)


def _setup(strike=24000, option_type="CE", expiry="2026-08-07"):
    return Setup(symbol="NIFTY", strike=strike, option_type=option_type, expiry=expiry,
                reasons=["structural bullish break"], score=1.0)


def _plan(entry=100.0, stop=80.0, target=140.0, lots=1):
    return TradePlan(setup=_setup(), entry=entry, target=target, stop=stop,
                     invalidation="test", lots=lots, capital_at_risk=1300.0,
                     risk_pct_of_capital=2.6, risk_level="Low",
                     stop_basis="test", entry_basis="LTP 100.0")


def test_open_position_writes_state_and_returns_trade(state_paths):
    state = pat.load_state()
    snap = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    trade = pat.open_position(state, _setup(), _plan(), pair="intraday", snapshot=snap)

    assert trade["status"] == "OPEN"
    assert trade["pair"] == "intraday"
    assert pat.has_open_trade(state, "intraday")
    assert not pat.has_open_trade(state, "daily_hourly")
    reloaded = pat.load_state()
    assert len(reloaded["trades"]) == 1


def test_update_positions_closes_on_target(state_paths):
    state = pat.load_state()
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, _setup(), _plan(entry=100.0, stop=80.0, target=140.0), "intraday", snap0)

    snap1 = _snapshot(datetime(2026, 6, 1, 10, 0), [_quote(24000, "CE", 145.0)])
    closed = pat.update_positions(state, snap1)

    assert len(closed) == 1
    assert closed[0]["outcome"] == "WIN"
    assert closed[0]["exit_ltp"] == 145.0
    assert closed[0]["pnl_inr"] > 0
    assert state["trades"] == []  # closed trades don't linger in state


def test_update_positions_closes_on_stop(state_paths):
    state = pat.load_state()
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, _setup(), _plan(entry=100.0, stop=80.0, target=140.0), "intraday", snap0)

    snap1 = _snapshot(datetime(2026, 6, 1, 10, 0), [_quote(24000, "CE", 75.0)])
    closed = pat.update_positions(state, snap1)

    assert closed[0]["outcome"] == "LOSS"
    assert closed[0]["exit_ltp"] == 75.0


def test_update_positions_uses_bid_for_exit_when_book_available(state_paths, monkeypatch):
    import config_price_action as pcfg
    monkeypatch.setattr(pcfg, "USE_BID_ASK_FILLS", True)
    state = pat.load_state()
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, _setup(), _plan(entry=100.0, stop=80.0, target=140.0), "intraday", snap0)

    # bid 141 (>= target -> WIN via bid), ask 150 (LTP would also clear target,
    # but the close must use the bid, not the ask or LTP)
    snap1 = _snapshot(datetime(2026, 6, 1, 10, 0), [_quote(24000, "CE", 148.0, bid=141.0, ask=150.0)])
    closed = pat.update_positions(state, snap1)
    assert closed[0]["exit_ltp"] == 141.0


def test_missing_quote_mid_hold_keeps_trade_open_not_a_zero(state_paths):
    """Missing information is not a zero -- same principle as condor/
    directional_spread's own trackers."""
    state = pat.load_state()
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, _setup(), _plan(), "intraday", snap0)

    snap1 = _snapshot(datetime(2026, 6, 1, 10, 0), [])  # strike absent this cycle, not yet expiry
    closed = pat.update_positions(state, snap1)
    assert closed == []
    assert len(state["trades"]) == 1
    assert state["trades"][0]["status"] == "OPEN"


def test_past_expiry_with_quote_settles_at_ltp(state_paths):
    state = pat.load_state()
    setup = _setup(expiry="2026-06-01")
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, setup, _plan(entry=100.0, stop=80.0, target=140.0), "daily_hourly", snap0)

    # Next day, past that contract's own expiry, still mid-band (neither target nor stop)
    snap1 = _snapshot(datetime(2026, 6, 2, 9, 20), [_quote(24000, "CE", 110.0)])
    closed = pat.update_positions(state, snap1)
    assert closed[0]["outcome"] == "EXPIRY_CLOSE"
    assert closed[0]["exit_ltp"] == 110.0


def test_past_expiry_without_quote_falls_back_to_intrinsic(state_paths):
    state = pat.load_state()
    setup = _setup(strike=24000, option_type="CE", expiry="2026-06-01")
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, setup, _plan(entry=100.0, stop=80.0, target=140.0), "daily_hourly", snap0)

    # Strike never reappears in the chain, but spot is now well above it
    snap1 = _snapshot(datetime(2026, 6, 2, 9, 20), [], spot=24150.0)
    closed = pat.update_positions(state, snap1)
    assert closed[0]["outcome"] == "EXPIRY_CLOSE"
    assert closed[0]["exit_ltp"] == 150.0  # intrinsic: max(24150 - 24000, 0)


def test_r_multiple_milestones_tracked_while_open(state_paths):
    state = pat.load_state()
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, _setup(), _plan(entry=100.0, stop=80.0, target=1000.0), "intraday", snap0)

    # 1R = 20 premium pts. Price at 120 -> exactly 1.0R.
    snap1 = _snapshot(datetime(2026, 6, 1, 10, 0), [_quote(24000, "CE", 120.0)])
    pat.update_positions(state, snap1)

    trade = state["trades"][0]
    assert trade["max_r_seen"] == pytest.approx(1.0)
    assert "1.0" in trade["rr_milestones_hit"]
    assert "0.5" in trade["rr_milestones_hit"]
    assert "1.5" not in trade["rr_milestones_hit"]


def test_two_pairs_can_hold_open_positions_independently(state_paths):
    state = pat.load_state()
    snap0 = _snapshot(datetime(2026, 6, 1, 9, 20), [])
    pat.open_position(state, _setup(strike=24000), _plan(), "intraday", snap0)
    pat.open_position(state, _setup(strike=24100), _plan(), "daily_hourly", snap0)

    assert pat.has_open_trade(state, "intraday")
    assert pat.has_open_trade(state, "daily_hourly")
    assert pat.tracked_strikes(state) == {(24000, "CE"), (24100, "CE")}
