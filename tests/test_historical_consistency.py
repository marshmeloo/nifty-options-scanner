"""
Tests for historical_consistency.py -- the internal-plausibility sweep
that stands in for validate_historical.py on days we have no live
recording to cross-check against (almost all of a multi-year backfill).

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import historical_consistency as hc
from models import MarketSnapshot, OptionQuote


def _quote(strike, opt_type, oi=1000, iv=15.0):
    return OptionQuote(
        symbol="NIFTY", expiry="rolling:week1", strike=strike, option_type=opt_type,
        ltp=100.0, oi=oi, oi_change_pct=0.0, volume=10, iv=iv, iv_percentile=50.0,
        timestamp=datetime.now(),
    )


def _day(day_str, spots, oi=1000, iv=15.0, n_strikes=1):
    base = datetime.fromisoformat(f"{day_str}T09:15:00")
    snaps = []
    for i, spot in enumerate(spots):
        chain = [_quote(24000.0 + 50 * k, "CE", oi=oi, iv=iv) for k in range(n_strikes)]
        snaps.append(MarketSnapshot(
            symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=chain,
            timestamp=base + timedelta(minutes=5 * i), source="dhan_historical",
        ))
    return snaps


def test_full_coverage_normal_day_passes():
    snaps = _day("2026-01-05", [24000 + i for i in range(75)])  # ~75 bars, small drift
    result = hc.check_day(snaps, interval_minutes=5)
    assert result["passed"]
    assert result["issues"] == []


def test_low_bar_coverage_flagged_as_partial_fetch():
    snaps = _day("2026-01-05", [24000, 24005, 24010])  # 3 of ~75 expected bars
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("partial or failed fetch" in i for i in result["issues"])


def test_extreme_daily_range_flagged():
    spots = [24000.0] * 74 + [24000.0 * 1.20]  # 20% single-day move
    snaps = _day("2026-01-05", spots)
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("plausibility ceiling" in i for i in result["issues"])


def test_negative_oi_flagged():
    snaps = _day("2026-01-05", [24000.0] * 75)
    snaps[10].chain[0].oi = -5
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("negative OI" in i for i in result["issues"])


def test_negative_iv_flagged():
    snaps = _day("2026-01-05", [24000.0] * 75)
    snaps[10].chain[0].iv = -1.0
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("negative IV" in i for i in result["issues"])


def test_oi_doubling_between_adjacent_bars_flagged():
    snaps = _day("2026-01-05", [24000.0] * 75, oi=1000)
    snaps[5].chain[0].oi = 5000  # >2x jump from the previous bar's 1000
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("OI jump" in i for i in result["issues"])


def test_organic_oi_growth_across_the_day_not_flagged():
    """OI climbing steadily bar to bar (real intraday buildup) must not trip the step check."""
    base = datetime.fromisoformat("2026-01-05T09:15:00")
    snaps = []
    for i in range(75):
        chain = [_quote(24000.0, "CE", oi=1000 + i * 10, iv=15.0)]
        snaps.append(MarketSnapshot(
            symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0, chain=chain,
            timestamp=base + timedelta(minutes=5 * i), source="dhan_historical",
        ))
    result = hc.check_day(snaps, interval_minutes=5)
    assert result["passed"]


def test_iv_jump_flagged():
    snaps = _day("2026-01-05", [24000.0] * 75, iv=15.0)
    snaps[5].chain[0].iv = 40.0  # 25-point jump from the previous bar's 15.0
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("IV jump" in i for i in result["issues"])


def test_empty_day_fails_rather_than_silently_passing():
    result = hc.check_day([], interval_minutes=5)
    assert not result["passed"]
    assert "no snapshots" in result["issues"][0]


def test_issue_list_is_truncated_with_a_count():
    snaps = _day("2026-01-05", [24000.0] * 75, oi=1000, n_strikes=30)
    for snap in snaps[1:]:
        for q in snap.chain:
            q.oi = 1  # every strike jumps from 1000 -> 1 each bar, far more than 20 issues
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert result["issues"][-1].startswith("... and")


def test_check_range_aggregates_pass_fail_across_days():
    good = _day("2026-01-05", [24000 + i for i in range(75)])
    bad = _day("2026-01-06", [24000, 24001])  # low coverage
    summary = hc.check_range({"2026-01-05": good, "2026-01-06": bad}, interval_minutes=5)
    assert not summary["passed"]
    assert summary["failed_days"] == ["2026-01-06"]
    assert summary["total_days"] == 2


def test_describe_mentions_the_live_validated_days_caveat():
    summary = hc.check_range({"2026-01-05": _day("2026-01-05", [24000 + i for i in range(75)])})
    text = hc.describe(summary)
    assert "2026-07-29" in text and "2026-07-30" in text
