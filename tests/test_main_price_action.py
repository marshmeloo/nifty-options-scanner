"""
Tests for main_price_action.py's pure logic and one-cycle wiring:
market-hours gating, HTF/LTF candle assembly per pair (with network
calls mocked out), and that a forced signal actually opens a tracked
position end-to-end through trade_staging's auto-approve path.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config_price_action as pcfg
import main_price_action as mpa
import price_action_tracker as pat
from models import Candle, MarketSnapshot, OptionQuote
from price_structure import StructureSignal


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pat, "STATE_PATH", tmp_path / "price_action_position.json")
    monkeypatch.setattr(pat, "JOURNAL_PATH", tmp_path / "price_action_journal.jsonl")
    staged_path = tmp_path / "staged_orders.json"
    import trade_staging as staging
    monkeypatch.setattr(staging, "STAGED_ORDERS_PATH", staged_path)
    return tmp_path


def _candles(n, start, minutes=5, base=100.0):
    ts = datetime.fromisoformat(start)
    return [
        Candle(timestamp=ts + timedelta(minutes=minutes * i),
              open=base, high=base + 1, low=base - 1, close=base, volume=1000)
        for i in range(n)
    ]


def _quote(strike, opt, ltp):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-07", strike=strike, option_type=opt,
        ltp=ltp, oi=5000, oi_change_pct=0.0, volume=500, iv=14.0, iv_percentile=50.0,
    )


def _snapshot(ts, chain, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=chain, timestamp=ts)


# --- market_is_open ---------------------------------------------------

def test_market_is_open_within_hours_on_a_weekday():
    assert mpa.market_is_open(datetime(2026, 8, 4, 10, 0))  # Tuesday


def test_market_is_open_false_before_open_and_after_close():
    assert not mpa.market_is_open(datetime(2026, 8, 4, 9, 0))
    assert not mpa.market_is_open(datetime(2026, 8, 4, 15, 45))


def test_market_is_open_false_on_weekend():
    assert not mpa.market_is_open(datetime(2026, 8, 8, 10, 0))  # Saturday


# --- _htf_ltf_for -------------------------------------------------------

def test_intraday_pair_uses_today_five_min_and_resamples_htf(monkeypatch):
    five_min = _candles(24, "2026-08-04T09:15:00", minutes=5)  # 2 hours -> 8 15-min bars
    monkeypatch.setattr(mpa, "get_nifty_intraday_candles", lambda **kw: five_min)

    htf, ltf = mpa._htf_ltf_for("intraday", now=datetime(2026, 8, 4, 11, 15))
    assert ltf == five_min
    assert len(htf) == 8  # 24 candles / (15/5 ratio=3) = 8


def test_daily_hourly_pair_excludes_todays_still_forming_daily_bar(monkeypatch):
    daily = [
        Candle(datetime(2026, 8, 1), 100, 101, 99, 100, 1000),
        Candle(datetime(2026, 8, 3), 100, 102, 98, 101, 1000),
        Candle(datetime(2026, 8, 4), 101, 103, 100, 102, 1000),  # today -- must be excluded
    ]
    monkeypatch.setattr(mpa.dhan_source, "get_nifty_daily_candles", lambda **kw: daily)
    monkeypatch.setattr(mpa, "get_nifty_intraday_candles", lambda **kw: _candles(12, "2026-08-04T09:15:00"))

    htf, ltf = mpa._htf_ltf_for("daily_hourly", now=datetime(2026, 8, 4, 11, 15))
    assert len(htf) == 2
    assert all(c.timestamp.date() < datetime(2026, 8, 4).date() for c in htf)
    assert len(ltf) == 1  # 12 5-min candles -> 1 completed 60-min bar (ratio 12)


# --- run_once: a forced signal actually opens a tracked position -------

def test_forced_signal_opens_a_tracked_position_via_auto_approve(monkeypatch, isolated_state):
    monkeypatch.setattr(pcfg, "AUTO_APPROVE_NEW_POSITIONS", True)
    monkeypatch.setattr(pcfg, "ACTIVE_PAIRS", ["intraday"])

    chain = [_quote(24000, "CE", 90.0)]
    snap = _snapshot(datetime(2026, 8, 4, 10, 30), chain)
    monkeypatch.setattr(mpa, "get_nifty_snapshot", lambda **kw: snap)
    monkeypatch.setattr(mpa, "get_nifty_intraday_candles", lambda **kw: _candles(30, "2026-08-04T09:15:00"))

    zone = type("Zone", (), {"kind": "support", "low": 23990.0, "high": 24010.0, "note": "test"})()
    signal = StructureSignal(direction="bullish", entry_reference=24050.0, stop_reference=24000.0,
                             zone=zone, reasons=["forced test signal"])
    monkeypatch.setattr(mpa.ps, "generate_signal", lambda htf, ltf: signal)

    state = pat.load_state()
    mpa.run_once(state)

    assert pat.has_open_trade(state, "intraday")
    trade = pat.open_trades(state)[0]
    assert trade["strike"] == 24000
    assert trade["option_type"] == "CE"
    assert trade["entry"] == 90.0


def test_no_signal_leaves_state_untouched(monkeypatch, isolated_state):
    monkeypatch.setattr(pcfg, "ACTIVE_PAIRS", ["intraday"])
    chain = [_quote(24000, "CE", 90.0)]
    snap = _snapshot(datetime(2026, 8, 4, 10, 30), chain)
    monkeypatch.setattr(mpa, "get_nifty_snapshot", lambda **kw: snap)
    monkeypatch.setattr(mpa, "get_nifty_intraday_candles", lambda **kw: _candles(30, "2026-08-04T09:15:00"))
    monkeypatch.setattr(mpa.ps, "generate_signal", lambda htf, ltf: None)

    state = pat.load_state()
    mpa.run_once(state)
    assert state.get("trades", []) == []


def test_open_position_for_a_pair_is_not_reopened_while_still_open(monkeypatch, isolated_state):
    """The gate is per-pair: an open intraday trade must not stop
    _handle_pair from being CALLED for daily_hourly, but must skip
    scanning for intraday itself."""
    monkeypatch.setattr(pcfg, "ACTIVE_PAIRS", ["intraday"])
    chain = [_quote(24000, "CE", 90.0)]
    snap = _snapshot(datetime(2026, 8, 4, 10, 30), chain)
    monkeypatch.setattr(mpa, "get_nifty_snapshot", lambda **kw: snap)
    monkeypatch.setattr(mpa, "get_nifty_intraday_candles", lambda **kw: _candles(30, "2026-08-04T09:15:00"))

    calls = {"n": 0}
    def spy(htf, ltf):
        calls["n"] += 1
        return None
    monkeypatch.setattr(mpa.ps, "generate_signal", spy)

    state = pat.load_state()
    from models import Setup, TradePlan
    setup = Setup(symbol="NIFTY", strike=24000, option_type="CE", expiry="2026-08-07",
                 reasons=["preexisting"], score=1.0)
    plan = TradePlan(setup=setup, entry=90.0, target=130.0, stop=70.0, invalidation="x",
                     lots=1, capital_at_risk=1300.0, risk_pct_of_capital=2.6, risk_level="Low")
    pat.open_position(state, setup, plan, "intraday", snap)

    mpa.run_once(state)
    assert calls["n"] == 0  # never even reached signal generation for an already-open pair
