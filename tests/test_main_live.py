"""
Tests for main_live.py's market-hours boundary.

Pinned 2026-08-04: recorded snapshots showed NIFTY spot frozen at the
exact same value for 10+ consecutive 30s cycles starting ~15:15, both on
that day (an expiry day) and the day before (not an expiry day) -- see
main_live.py's MARKET_CLOSE comment for the full evidence. Momentum's
trades are always same-session, so MARKET_CLOSE itself moved to 15:15
rather than the exchange's nominal 15:30, so new entries stop and any
end-of-day force-close uses the last price that was still genuinely live.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import main_live as ml


def test_market_close_is_1515_not_the_nominal_1530():
    assert ml.MARKET_CLOSE.hour == 15
    assert ml.MARKET_CLOSE.minute == 15


def test_market_is_open_just_before_1515():
    assert ml.market_is_open(datetime(2026, 8, 4, 15, 14, 59))


def test_market_is_open_false_just_after_1515():
    # 15:15:00 itself is still the inclusive boundary (same semantics as
    # the original 15:30 comparison) -- closed starts the next instant.
    assert not ml.market_is_open(datetime(2026, 8, 4, 15, 15, 1))
    assert not ml.market_is_open(datetime(2026, 8, 4, 15, 25, 0))
    assert not ml.market_is_open(datetime(2026, 8, 4, 15, 30, 0))


def test_market_is_open_still_true_mid_session():
    assert ml.market_is_open(datetime(2026, 8, 4, 12, 0, 0))


def test_market_is_open_false_on_weekend():
    assert not ml.market_is_open(datetime(2026, 8, 8, 12, 0, 0))  # Saturday
