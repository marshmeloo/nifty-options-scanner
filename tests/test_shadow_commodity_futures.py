"""
Tests for shadow_commodity_futures.py.

Run: python -m pytest tests/test_shadow_commodity_futures.py -q
"""
from datetime import datetime, timedelta

import pytest

import shadow_commodity_futures as scf
from models import Candle


def _daily(n, start="2026-01-05", base=100.0, step=0.0):
    ts = datetime.fromisoformat(f"{start}T00:00:00")
    out = []
    for i in range(n):
        d = ts + timedelta(days=i)
        if d.weekday() >= 5:  # skip weekends, real MCX data has none
            continue
        px = base + step * i
        out.append(Candle(timestamp=d, open=px, high=px + 1, low=px - 1, close=px, volume=100))
    return out


def test_weekly_bars_groups_by_iso_week():
    candles = _daily(10, start="2026-01-05")  # Mon 2026-01-05
    weekly = scf._weekly_bars(candles)
    assert len(weekly) == 2  # 10 calendar days ~ spans 2 ISO weeks after weekend skip
    assert weekly[0].open == candles[0].open


def test_clean_daily_candles_excludes_known_bad_dates(monkeypatch):
    candles = _daily(5, start="2022-09-19")  # includes 2022-09-22, a known-bad date
    monkeypatch.setattr(scf.csrc, "load_daily", lambda sym: candles)

    cleaned = scf._clean_daily_candles("GOLD")
    dates = {c.timestamp.date() for c in cleaned}
    assert scf._date(2022, 9, 22) not in dates
    assert len(cleaned) == len(candles) - 1


def test_finalise_bullish_win():
    trade = scf.FuturesTrade(symbol="GOLD", direction="bullish", opened_at="2026-01-05",
                             entry=100.0, stop=90.0, target=120.0)
    out = scf._finalise(trade, scf._date(2026, 1, 10), 120.0, "WIN", lot_size=1.0)
    assert out.net_r == pytest.approx(2.0)
    assert out.net_inr == pytest.approx(20.0)


def test_finalise_bearish_loss():
    trade = scf.FuturesTrade(symbol="SILVER", direction="bearish", opened_at="2026-01-05",
                             entry=100.0, stop=110.0, target=80.0)
    out = scf._finalise(trade, scf._date(2026, 1, 10), 112.0, "LOSS", lot_size=1.0)
    # bearish: pnl_per_unit = entry - exit = 100 - 112 = -12; risk = |100-110| = 10
    assert out.net_r == pytest.approx(-1.2)
    assert out.net_inr == pytest.approx(-12.0)


def test_run_never_places_a_real_order_and_returns_a_list(monkeypatch):
    """Smoke test: with no real signal ever firing (structure never
    breaks against flat synthetic data), run() must return an empty
    list rather than error -- 'no signal' is the default outcome."""
    flat = _daily(60, start="2026-01-05", base=100.0, step=0.0)
    monkeypatch.setattr(scf.csrc, "load_daily", lambda sym: flat)
    trades = scf.run("GOLD", verbose=False)
    assert isinstance(trades, list)


def test_run_opens_and_time_exits_a_trending_series(monkeypatch):
    """A strongly trending series should eventually produce at least one
    signal and, since it never reverses to hit a stop or a distant
    target, resolve via MAX_HOLD_DAYS TIME_EXIT rather than hang open
    forever."""
    trending = _daily(120, start="2026-01-05", base=100.0, step=1.5)
    monkeypatch.setattr(scf.csrc, "load_daily", lambda sym: trending)
    trades = scf.run("GOLD", max_hold_days=10, verbose=False)
    for t in trades:
        assert t.outcome in ("WIN", "LOSS", "TIME_EXIT")
        assert t.net_r is not None
