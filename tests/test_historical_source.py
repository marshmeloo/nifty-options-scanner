"""
Tests for historical_source.py -- reconstructing MarketSnapshots from
Dhan's Expired Options Data API.

These pin the things that would silently corrupt a backtest rather than
fail it: misaligned parallel arrays, and the ATM+/-10 offset cap that the
API enforces by returning HTTP 200 with an empty body instead of an error.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import pytest

import historical_source as hs


def test_offset_label_matches_api_spelling():
    assert hs._offset_label(0) == "ATM"
    assert hs._offset_label(3) == "ATM+3"
    assert hs._offset_label(-3) == "ATM-3"


def test_offset_beyond_api_cap_raises_rather_than_returning_empty():
    """
    The API answers an out-of-range offset with HTTP 200 and an empty
    data block -- indistinguishable from "this strike genuinely never
    traded" unless we refuse the request up front.
    """
    with pytest.raises(ValueError, match="exceeds the API"):
        hs.fetch_series(hs.MAX_STRIKE_OFFSET + 1, "CE", "2026-07-01", "2026-07-05")


def test_rows_zips_to_shortest_array_instead_of_misaligning():
    """
    A truncated array must drop the unpaired tail, not pair one bar's
    price with another bar's OI -- corruption that would look like data.
    """
    block = {
        "timestamp": [1782877500, 1782877800, 1782878100],
        "close": [100.0, 101.0, 102.0],
        "oi": [500, 600],  # one short
    }
    rows = list(hs._rows(block))
    assert len(rows) == 2
    assert rows[0]["close"] == 100.0 and rows[0]["oi"] == 500
    assert rows[1]["close"] == 101.0 and rows[1]["oi"] == 600


def test_bar_timestamp_is_advanced_to_the_bars_close():
    """
    The API stamps each bar with its START, but we read its CLOSE. Without
    advancing by one interval every backtest runs one bar behind the
    market -- silently, since nothing errors. Validated against the live
    2026-07-30 recording; see _rows' docstring for the shift sweep.
    """
    base = 1782877500
    block = {"timestamp": [base], "close": [100.0]}

    five = list(hs._rows(block, interval_minutes=5))[0]["timestamp"]
    one = list(hs._rows(block, interval_minutes=1))[0]["timestamp"]

    assert (five - datetime.fromtimestamp(base)).total_seconds() == 300
    assert (one - datetime.fromtimestamp(base)).total_seconds() == 60


def test_reconstruct_range_applies_the_interval_shift(monkeypatch):
    """The shift must survive the pivot, not just exist inside _rows."""
    base = 1782877500
    monkeypatch.setattr(hs, "fetch_series", lambda o, t, *a, **kw: {
        "timestamp": [base], "close": [100.0], "strike": [24000.0],
        "spot": [24010.0], "oi": [1000], "volume": [10], "iv": [12.5],
    })
    snap = hs.reconstruct_range("2026-07-01", "2026-07-02", interval="15", max_offset=0)[0]
    assert (snap.timestamp - datetime.fromtimestamp(base)).total_seconds() == 900


def test_rows_empty_when_no_timestamps():
    assert list(hs._rows({})) == []
    assert list(hs._rows({"close": [1.0]})) == []


def test_chunks_respect_the_30_day_per_call_cap():
    chunks = list(hs._chunks("2026-01-01", "2026-03-01"))
    assert all(
        (hs.date.fromisoformat(b) - hs.date.fromisoformat(a)).days < hs.MAX_DAYS_PER_CALL
        for a, b in chunks
    )
    # Contiguous and complete: no gaps, no overlap, full range covered.
    assert chunks[0][0] == "2026-01-01"
    assert chunks[-1][1] == "2026-03-01"
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert (hs.date.fromisoformat(next_start)
                - hs.date.fromisoformat(prev_end)).days == 1


def test_single_short_range_is_one_chunk():
    assert list(hs._chunks("2026-01-01", "2026-01-05")) == [("2026-01-01", "2026-01-05")]


def _block(strike, spot, closes):
    n = len(closes)
    base = 1782877500
    return {
        "timestamp": [base + 300 * i for i in range(n)],
        "close": closes,
        "strike": [strike] * n,
        "spot": [spot] * n,
        "oi": [1000] * n,
        "volume": [10] * n,
        "iv": [12.5] * n,
    }


def test_reconstruct_range_pivots_series_into_snapshots(monkeypatch):
    """Two offsets x two option types should collapse into one chain per timestamp."""
    def fake_fetch(offset, option_type, *a, **kw):
        if abs(offset) > 1:
            return {}
        return _block(24000 + offset * 50, 24010.0, [100.0, 101.0])

    monkeypatch.setattr(hs, "fetch_series", fake_fetch)
    snaps = hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=1)

    assert len(snaps) == 2  # two timestamps
    first = snaps[0]
    assert first.spot == 24010.0
    assert first.source == "dhan_historical"
    # 3 offsets (-1, 0, +1) x CE/PE
    assert len(first.chain) == 6
    assert {q.option_type for q in first.chain} == {"CE", "PE"}
    assert sorted({q.strike for q in first.chain}) == [23950.0, 24000.0, 24050.0]
    assert snaps[0].timestamp < snaps[1].timestamp


def test_reconstruct_range_leaves_bid_ask_and_greeks_unset(monkeypatch):
    """
    The endpoint returns OHLC only. These must stay None so shadow.py
    falls back to LTP fills rather than silently inventing a spread.
    """
    monkeypatch.setattr(hs, "fetch_series",
                        lambda o, t, *a, **kw: _block(24000, 24010.0, [100.0]))
    snap = hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=0)[0]
    q = snap.chain[0]
    assert q.bid is None and q.ask is None
    assert q.delta is None and q.theta is None and q.vega is None


def test_cycle_without_spot_is_dropped(monkeypatch):
    """No spot means no ATM reference -- the cycle is unusable, not zero."""
    block = _block(24000, 24010.0, [100.0])
    block["spot"] = [None]
    monkeypatch.setattr(hs, "fetch_series", lambda o, t, *a, **kw: block)
    assert hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=0) == []


def test_coverage_reports_narrowest_side_of_the_strike_window(monkeypatch):
    """
    Coverage must report the NEARER edge: a condor leg outside it finds
    no quote and would otherwise look like "no opportunity" rather than
    "the data never contained this strike".
    """
    monkeypatch.setattr(hs, "fetch_series",
                        lambda o, t, *a, **kw: (
                            {} if abs(o) > 2 else _block(24000 + o * 50, 24080.0, [100.0])))
    snap = hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=2)[0]
    cov = hs.coverage(snap)

    assert cov["strikes"] == 5
    assert cov["min_strike"] == 23900.0 and cov["max_strike"] == 24100.0
    # spot 24080 sits nearer the top: 24100-24080 = 20, vs 24080-23900 = 180.
    assert cov["points_from_spot"] == 20.0


def test_coverage_of_empty_chain_is_zero_not_a_crash():
    from models import MarketSnapshot
    snap = MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=0.0,
                          chain=[], timestamp=datetime.now(), source="dhan_historical")
    assert hs.coverage(snap)["strikes"] == 0
