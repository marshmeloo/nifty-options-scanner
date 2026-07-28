"""
Tests for intraday OI-buildup freshness.

The defect these pin: `oi_change_pct` is measured against Dhan's
`previous_oi`, i.e. the PREVIOUS SESSION's closing OI. That figure only
grows as a session runs, so classifying buildup from it means a day-scale
read drives 30-second-cadence entry decisions.

On 2026-07-28 the 24050 PE was classified "long_buildup" at 10:21
(OI +112.2%), 13:14 (+338.2%) and 14:32 (+217.7%) -- three entries, all
reading identically, across completely different intraday price action,
because OI versus YESTERDAY had of course risen in all three cases.

Run: python -m pytest tests/ -q
"""

import pytest

import config
import dhan_source
from scanner import scan
from models import MarketSnapshot, OptionQuote


# --------------------------------------------------------------------------
# The rolling window itself
# --------------------------------------------------------------------------

def test_no_intraday_read_without_history():
    """
    A fresh session has no history. That must report None (unknown), NOT
    0.0 -- "we can't tell yet" and "nothing changed" are different claims,
    and conflating them would silently classify every contract as flat.
    """
    oi_change, price_change, window = dhan_source._intraday_change(
        samples=[], now_ts=1000.0, oi=50000, ltp=76.7
    )
    assert (oi_change, price_change, window) == (None, None, None)


def test_no_intraday_read_when_history_is_too_recent():
    """
    A baseline from seconds ago says nothing about a buildup. Requiring a
    minimum span stops the fast 5s position check from producing
    meaningless near-zero deltas.
    """
    now = 10_000.0
    samples = [{"t": now - 30, "oi": 50000, "ltp": 76.0}]   # 30s old
    oi_change, price_change, window = dhan_source._intraday_change(
        samples, now_ts=now, oi=60000, ltp=80.0
    )
    assert (oi_change, price_change, window) == (None, None, None)


def test_intraday_change_uses_oldest_sample_in_window():
    now = 10_000.0
    samples = [
        {"t": now - 900, "oi": 40000, "ltp": 70.0},   # 15 min ago -> the baseline
        {"t": now - 300, "oi": 55000, "ltp": 90.0},   # 5 min ago
    ]
    oi_change, price_change, window = dhan_source._intraday_change(
        samples, now_ts=now, oi=44000, ltp=77.0
    )
    assert oi_change == pytest.approx(10.0)      # 40000 -> 44000
    assert price_change == pytest.approx(10.0)   # 70.0 -> 77.0
    assert window == pytest.approx(15.0)


def test_samples_outside_the_window_are_ignored():
    """An hour-old sample must not be used as a 15-minute baseline."""
    now = 10_000.0
    samples = [{"t": now - 3600, "oi": 10000, "ltp": 20.0}]  # far outside
    assert dhan_source._intraday_change(samples, now, oi=50000, ltp=76.7) == (None, None, None)


def test_prune_samples_bounds_history_growth():
    now = 10_000.0
    samples = [{"t": now - offset, "oi": 1, "ltp": 1.0} for offset in (60, 600, 1800, 7200)]
    kept = dhan_source._prune_samples(samples, now)

    keep_seconds = config.OI_INTRADAY_LOOKBACK_MINUTES * 60 * 2
    assert all(now - s["t"] <= keep_seconds for s in kept)
    assert len(kept) < len(samples)


# --------------------------------------------------------------------------
# Classification on intraday vs day-cumulative figures
# --------------------------------------------------------------------------

def test_intraday_classification_can_disagree_with_the_daily_read():
    """
    The heart of it. OI is up hugely versus yesterday (day-cumulative
    reads 'buildup'), but over the last 15 minutes OI is FALLING while
    premium rises -- that's short covering, a different signal entirely.
    The old code could only ever see the first of those.
    """
    daily = dhan_source._classify_buildup(
        oi_change_pct=217.7, price_change_pct=2.8, oi_threshold=config.OI_BUILDUP_PCT
    )
    intraday = dhan_source._classify_buildup(
        oi_change_pct=-8.0, price_change_pct=3.0, oi_threshold=config.OI_INTRADAY_BUILDUP_PCT
    )
    assert daily == "long_buildup"
    assert intraday == "short_covering"
    assert daily != intraday


def test_flat_intraday_oi_is_not_classified_as_buildup():
    """
    The 2026-07-28 failure mode: OI massively up versus yesterday, but
    nothing actually happening right now. Must produce no signal.
    """
    assert dhan_source._classify_buildup(
        oi_change_pct=0.4, price_change_pct=0.1, oi_threshold=config.OI_INTRADAY_BUILDUP_PCT
    ) is None


def test_20260728_daily_figures_all_read_the_same():
    """
    Documents the original defect using the three real entries' OI
    figures: wildly different numbers, three different times of day,
    identical classification -- so the daily read could not distinguish
    them and all three cleared as fresh 'long buildup'.
    """
    real_entries = [
        (112.2, 1.4),    # 10:21
        (338.2, 2.8),    # 13:14
        (217.7, 2.8),    # 14:32
    ]
    classifications = {
        dhan_source._classify_buildup(oi, px, config.OI_BUILDUP_PCT)
        for oi, px in real_entries
    }
    assert classifications == {"long_buildup"}

    # And each one saturates the scorer's 3.0 magnitude cap, so the
    # multiplier carried no information either.
    for oi, _ in real_entries:
        assert min(abs(oi) / config.OI_BUILDUP_PCT, 3.0) == 3.0


# --------------------------------------------------------------------------
# Scanner integration
# --------------------------------------------------------------------------

def _quote(**overrides):
    base = dict(
        symbol="NIFTY", expiry="2026-08-04", strike=24050.0, option_type="PE",
        ltp=76.7, oi=50000, oi_change_pct=217.7, volume=1000, iv=12.0,
        iv_percentile=50.0, delta=-0.45, price_change_pct=2.8,
        buildup_type="long_buildup",
    )
    base.update(overrides)
    return OptionQuote(**base)


def _snapshot(quote):
    return MarketSnapshot(
        symbol="NIFTY", spot=24010.0, vwap=24010.0, pcr=1.0,
        chain=[quote], source="test",
    )


def test_scanner_prefers_the_intraday_magnitude():
    """
    A small, genuine intraday buildup should score LOWER than the
    saturated day-cumulative figure did -- the old multiplier was pinned
    at its 3.0 cap for any meaningful move, so it never varied.
    """
    fresh = _quote(
        oi_change_pct_intraday=3.0, price_change_pct_intraday=1.5,
        buildup_window_minutes=15.0,
    )
    stale_only = _quote()

    fresh_setup = scan(_snapshot(fresh))[0]
    stale_setup = scan(_snapshot(stale_only))[0]

    assert fresh_setup.score < stale_setup.score
    assert any("over the last 15 min" in r for r in fresh_setup.reasons)
    assert any("not enough intraday history yet" in r for r in stale_setup.reasons)


def test_scanner_reason_shows_both_windows():
    """The daily figure stays visible as context, clearly labelled."""
    q = _quote(
        oi_change_pct_intraday=6.0, price_change_pct_intraday=2.0,
        buildup_window_minutes=15.0,
    )
    reason = next(r for r in scan(_snapshot(q))[0].reasons if "Long buildup" in r)

    assert "+6.0%" in reason                  # intraday
    assert "+217.7% vs prev close" in reason  # day-cumulative, labelled


def test_scanner_still_works_on_sources_without_intraday_history():
    """
    NSE and CSV sources never populate the intraday fields. The scanner
    must fall back cleanly rather than crash on None.
    """
    setups = scan(_snapshot(_quote(oi_change_pct_intraday=None,
                                   price_change_pct_intraday=None,
                                   buildup_window_minutes=None)))
    assert setups
    assert setups[0].score != 0
