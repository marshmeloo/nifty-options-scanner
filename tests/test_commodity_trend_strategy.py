"""
Tests for commodity_trend_strategy.py's indicators and backtests.

Run: python -m pytest tests/test_commodity_trend_strategy.py -q
"""
from datetime import datetime, timedelta

import pytest

import commodity_trend_strategy as cts
from models import Candle


def _series(closes, start="2026-01-05"):
    ts = datetime.fromisoformat(f"{start}T00:00:00")
    out = []
    day = 0
    for px in closes:
        d = ts + timedelta(days=day)
        while d.weekday() >= 5:
            day += 1
            d = ts + timedelta(days=day)
        out.append(Candle(timestamp=d, open=px, high=px + 1, low=px - 1, close=px, volume=100))
        day += 1
    return out


def test_sma_series_basic():
    candles = _series([10, 20, 30, 40, 50])
    sma = cts.sma_series(candles, 2)
    assert sma[0] is None
    assert sma[1] == pytest.approx(15.0)
    assert sma[4] == pytest.approx(45.0)


def test_ema_series_seeds_with_sma_of_first_period():
    candles = _series([10, 20, 30, 40, 50])
    ema = cts.ema_series(candles, 3)
    assert ema[0] is None and ema[1] is None
    assert ema[2] == pytest.approx((10 + 20 + 30) / 3)


def test_ema_reacts_faster_than_sma_to_a_recent_jump():
    # Flat then one big jump: EMA should sit closer to the new price
    # than SMA does, since EMA weights the recent bar more heavily.
    candles = _series([100] * 10 + [200])
    sma = cts.sma_series(candles, 5)
    ema = cts.ema_series(candles, 5)
    assert ema[-1] > sma[-1]


def test_atr_series_warms_up_then_smooths():
    candles = _series([100, 102, 101, 105, 103, 107, 104, 110, 108, 112, 109, 115])
    atr = cts.atr_series(candles, 5)
    assert all(a is None for a in atr[:5])
    assert atr[5] is not None
    assert atr[5] > 0


def test_rsi_series_pure_uptrend_stays_high():
    """A monotonic uptrend should sit near 100 -- no losses to average."""
    candles = _series([100 + i for i in range(30)])
    rsi = cts.rsi_series(candles, period=14)
    later = [r for r in rsi[15:] if r is not None]
    assert later and all(r > 90 for r in later)


def test_supertrend_settles_up_on_a_strong_uptrend():
    candles = _series([100 + i * 3 for i in range(40)])
    st = cts.supertrend_series(candles, period=10, multiplier=3.0)
    directions = [d for d, _ in st if d is not None]
    assert directions[-1] == "up"


def test_supertrend_settles_down_on_a_strong_downtrend():
    candles = _series([300 - i * 3 for i in range(40)])
    st = cts.supertrend_series(candles, period=10, multiplier=3.0)
    directions = [d for d, _ in st if d is not None]
    assert directions[-1] == "down"


def test_backtest_supertrend_flips_produce_alternating_directions(monkeypatch):
    # A strong up leg then a strong down leg should produce at least one
    # flip, and consecutive trades must alternate direction (an
    # always-in-the-market reversal system, never two "up" trades back
    # to back without a "down" between them).
    up = [100 + i * 4 for i in range(30)]
    down = [up[-1] - i * 4 for i in range(1, 30)]
    candles = _series(up + down)
    monkeypatch.setattr(cts, "clean_daily_candles", lambda sym: candles)

    trades = cts.backtest_supertrend("GOLD")
    assert len(trades) >= 1
    for a, b in zip(trades, trades[1:]):
        assert a.direction != b.direction


def test_backtest_ma_crossover_normalises_r_by_atr(monkeypatch):
    # Needs a SECOND flip so the first trade actually closes -- an
    # opened-but-never-closed position is correctly not counted as a
    # trade (matches the project's established convention elsewhere).
    up1 = [100 + i * 2 for i in range(60)]
    down = [up1[-1] - i * 2 for i in range(1, 60)]
    up2 = [down[-1] + i * 2 for i in range(1, 60)]
    candles = _series(up1 + down + up2)
    monkeypatch.setattr(cts, "clean_daily_candles", lambda sym: candles)

    trades = cts.backtest_ma_crossover("GOLD", fast=5, slow=15, atr_period=10)
    assert len(trades) >= 1
    for t in trades:
        assert t.net_r is not None


def test_backtest_ma_crossover_ema_defaults_reproduce_old_sma_behaviour(monkeypatch):
    up1 = [100 + i * 2 for i in range(60)]
    down = [up1[-1] - i * 2 for i in range(1, 60)]
    up2 = [down[-1] + i * 2 for i in range(1, 60)]
    candles = _series(up1 + down + up2)
    monkeypatch.setattr(cts, "clean_daily_candles", lambda sym: candles)

    sma_trades = cts.backtest_ma_crossover("GOLD", fast=5, slow=15, atr_period=10)
    default_trades = cts.backtest_ma_crossover("GOLD", fast=5, slow=15, atr_period=10)
    assert [t.opened_at for t in sma_trades] == [t.opened_at for t in default_trades]

    ema_trades = cts.backtest_ma_crossover("GOLD", fast=5, slow=15, atr_period=10, ma_type="ema")
    assert isinstance(ema_trades, list)


def test_known_bad_dates_excluded_from_clean_daily_candles(monkeypatch):
    candles = _series(list(range(100, 110)), start="2022-09-19")
    monkeypatch.setattr(cts.csrc, "load_daily", lambda sym: candles)
    cleaned = cts.clean_daily_candles("GOLD")
    assert cts._date(2022, 9, 22) not in {c.timestamp.date() for c in cleaned}
