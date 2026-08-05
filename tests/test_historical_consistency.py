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


def test_transient_oi_spike_flagged():
    """A one-bar spike that snaps back is the bad-print signature."""
    snaps = _day("2026-01-05", [24000.0] * 75, oi=1000)
    snaps[5].chain[0].oi = 5000  # spikes, then bar 6 is back at 1000
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("transient OI spike" in i for i in result["issues"])


def test_persistent_oi_jump_is_not_flagged():
    """
    Measured 2026-08-04: 93 of 146 raw ratio-flags across a 20-day
    sample were real market moves, not corruption -- OI on liquid NIFTY
    weekly strikes genuinely can more than double in five minutes
    (2,133,625 -> 4,811,850 -> 6,515,375 was one real example). A jump
    that STAYS jumped is a real move and must not be reported.
    """
    snaps = _day("2026-01-05", [24000.0] * 75, oi=1000)
    for snap in snaps[5:]:
        snap.chain[0].oi = 5000  # steps up once and stays there
    result = hc.check_day(snaps, interval_minutes=5)
    assert result["passed"], result["issues"]


def test_oi_spike_on_the_final_bar_is_not_flagged():
    """
    A jump on the last reading has no "after" to confirm against, so it
    cannot be told apart from a real move -- deliberately not reported
    rather than guessed at in either direction.
    """
    snaps = _day("2026-01-05", [24000.0] * 75, oi=1000)
    snaps[-1].chain[0].oi = 99999
    result = hc.check_day(snaps, interval_minutes=5)
    assert result["passed"], result["issues"]


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


def test_transient_iv_spike_flagged():
    snaps = _day("2026-01-05", [24000.0] * 75, iv=15.0)
    snaps[5].chain[0].iv = 40.0  # 25-point spike, then back to 15.0
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("transient IV spike" in i for i in result["issues"])


def test_persistent_iv_shift_is_not_flagged():
    """Same reasoning as the OI case -- a vol regime that steps up and
    stays up is a real move, not a decode error."""
    snaps = _day("2026-01-05", [24000.0] * 75, iv=15.0)
    for snap in snaps[5:]:
        snap.chain[0].iv = 40.0
    result = hc.check_day(snaps, interval_minutes=5)
    assert result["passed"], result["issues"]


def test_iv_flickering_through_zero_is_not_flagged():
    """
    Dhan reports 0.0 for strikes it doesn't publish IV on, so a thin
    strike drifting in and out of coverage yields 0.0 -> 22.9 -> 0.0 all
    day. That is missing data, not a vol move -- counting it flagged 391
    of 493 days and buried every real signal.
    """
    snaps = _day("2026-01-05", [24000.0] * 75, iv=20.0)
    for i, snap in enumerate(snaps):
        snap.chain[0].iv = 0.0 if i % 2 else 20.0
    result = hc.check_day(snaps, interval_minutes=5)
    assert result["passed"], result["issues"]


def test_real_iv_spike_between_two_nonzero_values_is_still_flagged():
    """The zero exemption must not blind the check to a genuine decode error."""
    snaps = _day("2026-01-05", [24000.0] * 75, iv=15.0)
    snaps[5].chain[0].iv = 45.0
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert any("transient IV spike" in i for i in result["issues"])


def test_empty_day_fails_rather_than_silently_passing():
    result = hc.check_day([], interval_minutes=5)
    assert not result["passed"]
    assert "no snapshots" in result["issues"][0]


def test_issue_list_is_truncated_with_a_count():
    snaps = _day("2026-01-05", [24000.0] * 75, oi=1000, n_strikes=30)
    # Every strike spikes on every other bar and snaps straight back --
    # a genuine transient-spike pattern, repeated far more than 20 times.
    for i, snap in enumerate(snaps):
        if i % 2:
            for q in snap.chain:
                q.oi = 100000
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert result["issues"][-1].startswith("... and")


def test_flagged_issues_carry_distance_from_spot():
    """
    WHERE the corruption sits decides who it affects. Measured
    2026-08-04: contamination is zero within 150pts of spot across both
    datasets and climbs steeply beyond ~350pts, so a near-ATM strategy
    is untouched by a day that fails purely on far-OTM strikes. A bare
    pass/fail cannot express that; the distance can.
    """
    # Spot at 24000, spiking strike at 24400 -> 400pts out.
    base = datetime.fromisoformat("2026-01-05T09:15:00")
    snaps = []
    for i in range(75):
        chain = [_quote(24400.0, "CE", oi=5000 if i == 5 else 1000)]
        snaps.append(MarketSnapshot(
            symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0, chain=chain,
            timestamp=base + timedelta(minutes=5 * i), source="dhan_historical",
        ))
    result = hc.check_day(snaps, interval_minutes=5)
    assert not result["passed"]
    assert result["nearest_flagged_pts"] == 400.0
    assert any("400pts from spot" in i for i in result["issues"])


def test_clean_day_reports_no_flagged_distance():
    snaps = _day("2026-01-05", [24000 + i for i in range(75)])
    result = hc.check_day(snaps, interval_minutes=5)
    assert result["passed"]
    assert result["nearest_flagged_pts"] is None


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
