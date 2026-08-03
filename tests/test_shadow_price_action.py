"""
Tests for shadow_price_action.py.

Pins the real bug found while running this against the full 497-day
history: for the "daily_hourly" pair, resampling only the CURRENT day's
candles to 1-hour bars produces ~6 bars/day, far short of
generate_signal's 12-candle minimum -- the pair could never produce a
single signal across the whole dataset until the LTF series was made to
accumulate across days the same way the daily HTF series already does.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config_price_action as pcfg
import shadow_price_action as spa
from models import Candle, MarketSnapshot, OptionQuote


def _candles(n, start="2026-06-01T09:15:00", minutes=5, base=100.0):
    ts = datetime.fromisoformat(start)
    return [
        Candle(timestamp=ts + timedelta(minutes=minutes * i),
              open=base, high=base + 1, low=base - 1, close=base, volume=1000)
        for i in range(n)
    ]


def _quote(strike, option_type, ltp):
    return OptionQuote(
        symbol="NIFTY", expiry="rolling:week1", strike=strike, option_type=option_type,
        ltp=ltp, oi=1000, oi_change_pct=0.0, volume=500, iv=14.0, iv_percentile=50.0,
    )


def _snapshot(ts, chain, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0,
                          chain=chain, timestamp=ts, source="dhan_historical")


def _day_cycles(day: str, n_candles=75, chain=None, spot=24000.0):
    """One synthetic day: `n_candles` 5-min bars (a ~375-min session is
    ~75 of them), each cycle seeing the candles visible up to that point,
    exactly as snapshot_recorder's real cycles do."""
    all_candles = _candles(n_candles, start=f"{day}T09:15:00")
    chain = chain if chain is not None else [_quote(24000, "CE", 90.0), _quote(24000, "PE", 90.0)]
    cycles = []
    for i in range(n_candles):
        ts = all_candles[i].timestamp
        cycles.append((_snapshot(ts, chain, spot), all_candles[:i + 1], {}))
    return cycles


# --- _daily_bar / _intrinsic / _finalise (pure helpers) --------------------

def test_daily_bar_aggregates_a_days_candles():
    candles = [
        Candle(datetime(2026, 6, 1, 9, 15), 100, 105, 99, 102, 1000),
        Candle(datetime(2026, 6, 1, 9, 20), 102, 108, 101, 104, 1500),
        Candle(datetime(2026, 6, 1, 9, 25), 104, 106, 95, 96, 2000),
    ]
    bar = spa._daily_bar(candles)
    assert bar.open == 100 and bar.close == 96
    assert bar.high == 108 and bar.low == 95
    assert bar.volume == 4500
    assert bar.timestamp == candles[0].timestamp


def test_intrinsic_value_for_ce_and_pe():
    assert spa._intrinsic("CE", strike=24000, spot=24100) == 100
    assert spa._intrinsic("CE", strike=24000, spot=23900) == 0
    assert spa._intrinsic("PE", strike=24000, spot=23900) == 100
    assert spa._intrinsic("PE", strike=24000, spot=24100) == 0


def test_finalise_computes_net_r_and_costs():
    trade = spa.ShadowPATrade(pair="intraday", strike=24000, option_type="CE",
                              opened_at="2026-06-01T09:20:00", entry=100.0, stop=80.0, target=140.0)
    out = spa._finalise(trade, datetime(2026, 6, 1, 10, 0), 140.0, "WIN", peak=140.0, trough=100.0, lots=1)
    assert out.outcome == "WIN"
    assert out.exit_price == 140.0
    # gross = (140-100) * lot_size, net_r ~ 2.0 minus a small cost drag
    assert out.net_r == pytest.approx(2.0, abs=0.05)
    assert out.cost_inr > 0


# --- _walk_forward: target / stop / expiry-close ----------------------------

def test_walk_forward_exits_on_target():
    day = "2026-06-01"
    chain_hit = [_quote(24000, "CE", 140.0)]
    cycles = [
        (_snapshot(datetime(2026, 6, 1, 9, 20), [_quote(24000, "CE", 100.0)]), [], {}),
        (_snapshot(datetime(2026, 6, 1, 9, 25), [_quote(24000, "CE", 110.0)]), [], {}),
        (_snapshot(datetime(2026, 6, 1, 9, 30), chain_hit), [], {}),
    ]
    day_cache = {day: cycles}
    trade = spa.ShadowPATrade(pair="intraday", strike=24000, option_type="CE",
                              opened_at=cycles[0][0].timestamp.isoformat(),
                              entry=100.0, stop=80.0, target=140.0)
    out = spa._walk_forward(day_cache, [day], day, 0, 24000, "CE", trade, lots=1)
    assert out.outcome == "WIN"
    assert out.exit_price == 140.0


def test_walk_forward_exits_on_stop():
    day = "2026-06-01"
    cycles = [
        (_snapshot(datetime(2026, 6, 1, 9, 20), [_quote(24000, "CE", 100.0)]), [], {}),
        (_snapshot(datetime(2026, 6, 1, 9, 25), [_quote(24000, "CE", 78.0)]), [], {}),
    ]
    day_cache = {day: cycles}
    trade = spa.ShadowPATrade(pair="intraday", strike=24000, option_type="CE",
                              opened_at=cycles[0][0].timestamp.isoformat(),
                              entry=100.0, stop=80.0, target=140.0)
    out = spa._walk_forward(day_cache, [day], day, 0, 24000, "CE", trade, lots=1)
    assert out.outcome == "LOSS"
    assert out.exit_price == 78.0


def test_walk_forward_settles_at_expiry_when_window_exhausted():
    day = "2026-06-01"
    cycles = [
        (_snapshot(datetime(2026, 6, 1, 9, 20), [_quote(24000, "CE", 100.0)]), [], {}),
        (_snapshot(datetime(2026, 6, 1, 15, 25), [_quote(24000, "CE", 105.0)]), [], {}),
    ]
    day_cache = {day: cycles}
    trade = spa.ShadowPATrade(pair="intraday", strike=24000, option_type="CE",
                              opened_at=cycles[0][0].timestamp.isoformat(),
                              entry=100.0, stop=80.0, target=140.0)
    out = spa._walk_forward(day_cache, [day], day, 0, 24000, "CE", trade, lots=1)
    assert out.outcome == "EXPIRY_CLOSE"
    assert out.exit_price == 105.0


def test_walk_forward_falls_back_to_intrinsic_when_strike_never_reappears():
    """The rolling-series identity hazard: this strike simply never shows
    up again (ATM window moved away), so settlement must fall back to
    intrinsic value rather than inventing a flat close."""
    day = "2026-06-01"
    cycles = [
        (_snapshot(datetime(2026, 6, 1, 9, 20), [_quote(24000, "CE", 100.0)]), [], {}),
        (_snapshot(datetime(2026, 6, 1, 15, 25), [_quote(25000, "CE", 50.0)], spot=24300.0), [], {}),
    ]
    day_cache = {day: cycles}
    trade = spa.ShadowPATrade(pair="intraday", strike=24000, option_type="CE",
                              opened_at=cycles[0][0].timestamp.isoformat(),
                              entry=100.0, stop=80.0, target=140.0)
    out = spa._walk_forward(day_cache, [day], day, 0, 24000, "CE", trade, lots=1)
    assert out.outcome == "EXPIRY_CLOSE"
    assert out.exit_price == 300.0  # intrinsic: max(24300 - 24000, 0)


def test_walk_forward_unresolved_when_no_data_in_window():
    trade = spa.ShadowPATrade(pair="intraday", strike=24000, option_type="CE",
                              opened_at="2026-06-01T09:20:00", entry=100.0, stop=80.0, target=140.0)
    out = spa._walk_forward({}, [], "2026-06-01", 0, 24000, "CE", trade, lots=1)
    assert out.closed_at is None
    assert out.outcome is None


# --- run_pair: LTF must accumulate across days for daily_hourly -------------

def test_daily_hourly_ltf_accumulates_across_days_not_reset_per_day(monkeypatch):
    """
    Pins the real bug this module was built to fix: a single day's
    candles resample to only ~6 hourly bars, well under
    SWING_LOOKBACK*2+2 (12) -- resetting the LTF every day meant
    generate_signal's length gate rejected every single cycle, forever.
    Runs two days and asserts the LTF passed to generate_signal on day 2
    is longer than what one day alone could produce.
    """
    monkeypatch.setattr(pcfg, "SWING_LOOKBACK", 2)   # HTF gate needs len(daily_bars) >= 6
    days = [f"2026-06-{d:02d}" for d in range(1, 8)]   # 7 days: HTF gate passes from day 7
    # 30 5-min candles/day -> 2 completed 1-hour bars/day (ratio 60/5=12,
    # remainder dropped) -- enough to accumulate past the 6-bar LTF gate
    # over several days, but far short of it from any single day alone.
    day_cache = {d: _day_cycles(d, n_candles=30) for d in days}
    monkeypatch.setattr(spa, "_load_cached", lambda cache, d: day_cache[d])

    seen_ltf_lengths = []

    def spy(htf, ltf):
        seen_ltf_lengths.append(len(ltf))
        return None  # never fire a real signal -- only observing the input size

    monkeypatch.setattr(spa.ps, "generate_signal", spy)
    spa.run_pair("daily_hourly", days, verbose=False)

    assert seen_ltf_lengths, "generate_signal was never called -- HTF gate never passed"
    # Day 7's OWN hourly resample alone tops out at 2 bars (10 5-min
    # candles / a 5-per-hour-bar ratio -> 2). Seeing anything longer is
    # proof prior days' completed hourly bars carried forward instead of
    # being reset each day.
    assert max(seen_ltf_lengths) > 2
