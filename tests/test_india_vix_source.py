"""
Tests for india_vix_source.py's IV rank/percentile math -- network call
(fetch_history) is not exercised here, only the pure computation, using
a synthetic history built so the expected numbers are exact.

Run: python -m pytest tests/test_india_vix_source.py -q
"""
import pytest

import india_vix_source as ivs


def _history(n_days=260, start_value=10.0):
    """260 synthetic days, values 10.0..14.0 ramping linearly, then a
    known final day appended separately by each test."""
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    out = []
    for i in range(n_days):
        out.append(((base + timedelta(days=i)).isoformat(), round(start_value + i * (4.0 / n_days), 3)))
    return out


def test_iv_rank_and_percentile_at_the_extremes():
    hist = _history(260)
    last_date, _ = hist[-1]
    # Append a day AFTER the ramp with an extreme high value
    from datetime import date, timedelta
    extreme_date = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
    hist_with_extreme = hist + [(extreme_date, 100.0)]

    result = ivs.iv_rank_and_percentile(hist_with_extreme, extreme_date, lookback_days=252)
    assert result["vix"] == 100.0
    assert result["iv_rank"] == 100.0        # far above the whole trailing window
    assert result["iv_percentile"] == 100.0   # every trailing day closed below it


def test_iv_rank_and_percentile_at_the_low_extreme():
    hist = _history(260)
    last_date, _ = hist[-1]
    from datetime import date, timedelta
    low_date = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
    hist_with_low = hist + [(low_date, 0.01)]

    result = ivs.iv_rank_and_percentile(hist_with_low, low_date, lookback_days=252)
    assert result["iv_rank"] == 0.0
    assert result["iv_percentile"] == 0.0


def test_none_when_date_not_in_history():
    hist = _history(260)
    result = ivs.iv_rank_and_percentile(hist, "1999-01-01")
    assert result == {"vix": None, "iv_rank": None, "iv_percentile": None}


def test_none_rank_when_not_enough_trailing_history_yet():
    """Only 50 days of history before this date -- must not compute a
    rank/percentile as if a full year of context existed (that would
    silently understate/overstate how extreme today's reading is)."""
    hist = _history(60)
    as_of = hist[55][0]
    result = ivs.iv_rank_and_percentile(hist, as_of, lookback_days=252)
    assert result["vix"] is not None
    assert result["iv_rank"] is None
    assert result["iv_percentile"] is None


def test_todays_own_value_excluded_from_its_own_trailing_window():
    """A live decision on day N could never have seen day N's own
    close yet -- the window must be STRICTLY before, not inclusive."""
    hist = _history(260)
    as_of = hist[-1][0]
    # If today's own value leaked into the window, iv_percentile could
    # never legitimately read 100 (a day can't close below itself).
    # Force today's value to be the maximum and confirm it still scores
    # 100 using ONLY the prior 252 days, not n+1 including itself.
    hist[-1] = (hist[-1][0], 999.0)
    result = ivs.iv_rank_and_percentile(hist, as_of, lookback_days=252)
    assert result["iv_percentile"] == 100.0


def test_save_and_load_round_trip(tmp_path):
    hist = _history(10)
    path = tmp_path / "vix.json"
    ivs.save_history(hist, path)
    loaded = ivs.load_history(path)
    assert loaded == hist
