"""
Tests for market_regime.py.

The single most important behaviour here is the PARTIAL-DAY guard. An
in-progress session's range is necessarily incomplete -- at 09:30 it is
near zero and would score as the quietest day on record. Reporting that
percentile without saying so would be actively misleading, and would be
exactly the kind of confident-but-wrong number this project has been
burned by before (see the score-ratchet and stop_after_loss look-ahead
bugs). Early readings are a FLOOR on the final value, not an estimate.

Run: python -m pytest tests/ -q
"""

import datetime

import pytest

import config
import market_regime
from models import Candle


@pytest.fixture
def baseline(monkeypatch, tmp_path):
    """A known trailing distribution: ranges 0.1% .. 2.0% in 0.1 steps."""
    ranges = [round(0.1 * i, 2) for i in range(1, 21)]
    monkeypatch.setattr(market_regime, "BASELINE_PATH", tmp_path / "regime_baseline.json")
    monkeypatch.setattr(
        market_regime, "get_range_distribution",
        lambda force_refresh=False: {
            "date": datetime.date.today().isoformat(),
            "ranges": ranges, "median": 1.05, "days": len(ranges),
        },
    )
    return ranges


def _candles(open_price, high, low, n=10, start_hour=9, start_min=15):
    """n candles spanning the given high/low, opening at open_price."""
    t0 = datetime.datetime(2026, 7, 30, start_hour, start_min)
    out = []
    for i in range(n):
        out.append(Candle(
            timestamp=t0 + datetime.timedelta(minutes=5 * i),
            open=open_price, high=high if i == 0 else open_price,
            low=low if i == 0 else open_price, close=open_price, volume=1000,
        ))
    return out


# --------------------------------------------------------------------------
# The partial-day guard
# --------------------------------------------------------------------------

def test_early_session_reading_is_flagged_provisional(baseline):
    """At 10:00 only ~12% of the session has run -- must say so."""
    candles = _candles(24000, 24030, 23990)     # tiny range so far
    r = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 10, 0))
    assert r["is_provisional"] is True
    assert r["elapsed_pct"] < 20
    assert "PARTIAL" in market_regime.describe(r)
    assert "can only grow" in market_regime.describe(r)


def test_end_of_session_reading_is_not_provisional(baseline):
    candles = _candles(24000, 24240, 23990)
    r = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 15, 30))
    assert r["is_provisional"] is False
    assert "PARTIAL" not in market_regime.describe(r)


def test_elapsed_pct_is_clamped_outside_market_hours(baseline):
    candles = _candles(24000, 24100, 23950)
    before = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 8, 0))
    after = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 18, 0))
    assert before["elapsed_pct"] == 0
    assert after["elapsed_pct"] == 100


# --------------------------------------------------------------------------
# Percentile and labelling
# --------------------------------------------------------------------------

def test_quiet_day_is_labelled_quiet(baseline):
    # 0.2% range -> near the bottom of a 0.1-2.0 distribution
    candles = _candles(24000, 24048, 24000)
    r = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 15, 30))
    assert r["range_pct"] == pytest.approx(0.2, abs=0.01)
    assert r["percentile"] < config.REGIME_QUIET_PERCENTILE
    assert r["label"] == "quiet"


def test_volatile_day_is_labelled_volatile(baseline):
    # 1.8% range -> near the top
    candles = _candles(24000, 24432, 24000)
    r = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 15, 30))
    assert r["percentile"] > config.REGIME_VOLATILE_PERCENTILE
    assert r["label"] == "volatile"


def test_middling_day_is_labelled_normal(baseline):
    candles = _candles(24000, 24252, 24000)   # ~1.05%, the median
    r = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 15, 30))
    assert r["label"] == "normal"


def test_range_is_measured_high_to_low_not_open_to_close(baseline):
    """A day that round-trips has a real range even if it closes flat."""
    candles = _candles(24000, 24240, 23760)   # +/-1% swing, closes at open
    r = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 15, 30))
    assert r["range_pct"] == pytest.approx(2.0, abs=0.01)


# --------------------------------------------------------------------------
# Degradation -- regime context is a nice-to-have, never load-bearing
# --------------------------------------------------------------------------

def test_no_candles_returns_empty(baseline):
    assert market_regime.classify([]) == {}
    assert market_regime.classify(None) == {}


def test_missing_baseline_still_reports_range(monkeypatch, tmp_path):
    """
    If the daily-history fetch fails, we should still report today's raw
    range -- just without a percentile. Losing the benchmark must not
    lose the observation.
    """
    monkeypatch.setattr(market_regime, "BASELINE_PATH", tmp_path / "b.json")
    monkeypatch.setattr(market_regime, "get_range_distribution", lambda force_refresh=False: {})
    candles = _candles(24000, 24120, 23880)
    r = market_regime.classify(candles, now=datetime.datetime(2026, 7, 30, 15, 30))
    assert r["range_pct"] == pytest.approx(1.0, abs=0.01)
    assert r["percentile"] is None
    assert r["label"] == "unknown"


def test_describe_handles_empty_regime():
    assert "unavailable" in market_regime.describe({})


def test_zero_open_price_is_handled(baseline):
    candles = [Candle(datetime.datetime(2026, 7, 30, 9, 15), 0, 0, 0, 0, 0)]
    assert market_regime.classify(candles) == {}


# --------------------------------------------------------------------------
# Real recorded sessions
# --------------------------------------------------------------------------

def test_percentile_of_helper():
    ranges = [0.5, 1.0, 1.5, 2.0]
    assert market_regime._percentile_of(0.4, ranges) == 0.0
    assert market_regime._percentile_of(1.0, ranges) == 50.0
    assert market_regime._percentile_of(2.5, ranges) == 100.0
    assert market_regime._percentile_of(1.0, []) is None
