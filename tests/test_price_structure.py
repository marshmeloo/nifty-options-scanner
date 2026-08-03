"""
Tests for price_structure.py -- the pure price-action structure/signal
module (no OI, no IV, no indicators).

Each function is tested in isolation with small, precisely controlled
fixtures rather than chained through one big end-to-end scenario:
hand-building OHLC that produces a specific swing/leg/zone shape by
construction is far more reliable than tuning one large series until
generate_signal() happens to fire.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config_price_action as pcfg
import price_structure as ps
from models import Candle


def _candles(ohlc, start="2026-06-01T09:15:00", minutes=60, vol=1000):
    base = datetime.fromisoformat(start)
    return [
        Candle(timestamp=base + timedelta(minutes=minutes * i),
               open=o, high=h, low=l, close=c, volume=vol)
        for i, (o, h, l, c) in enumerate(ohlc)
    ]


@pytest.fixture(autouse=True)
def small_lookback(monkeypatch):
    """
    A small, deterministic lookback so short, hand-built fixtures can
    actually contain swing points -- SWING_LOOKBACK=5 (the live default)
    would need 11+ candles before even ONE swing point is possible.
    """
    monkeypatch.setattr(pcfg, "SWING_LOOKBACK", 2)
    monkeypatch.setattr(pcfg, "TREND_SWING_LOOKBACK", 2)


# --- resample_candles ---------------------------------------------------

def test_resample_aggregates_ohlc_correctly():
    src = _candles([(100, 102, 99, 101), (101, 103, 100, 102),
                    (102, 104, 101, 103), (103, 105, 102, 104)], minutes=5)
    out = ps.resample_candles(src, target_minutes=20, source_minutes=5)
    assert len(out) == 1
    bar = out[0]
    assert bar.open == 100          # first sub-candle's open
    assert bar.close == 104         # last sub-candle's close
    assert bar.high == 105          # max across the group
    assert bar.low == 99            # min across the group
    assert bar.volume == 4000       # summed
    assert bar.timestamp == src[0].timestamp   # stamped at group start


def test_resample_drops_a_partial_trailing_group():
    """5 source candles at ratio 4 -> one full group, one partial dropped."""
    src = _candles([(100, 101, 99, 100)] * 5, minutes=5)
    out = ps.resample_candles(src, target_minutes=20, source_minutes=5)
    assert len(out) == 1


def test_resample_passthrough_when_target_not_coarser():
    src = _candles([(100, 101, 99, 100)] * 3, minutes=5)
    assert ps.resample_candles(src, target_minutes=5, source_minutes=5) == src


def test_resample_rejects_a_non_exact_multiple():
    src = _candles([(100, 101, 99, 100)] * 3, minutes=5)
    with pytest.raises(ValueError):
        ps.resample_candles(src, target_minutes=12, source_minutes=5)


# --- classify_legs -------------------------------------------------------

def test_impulse_leg_needs_large_uniform_bodies():
    """
    Doc's three criteria: large candles, strong same-direction moves,
    same color. Swing points verified by hand against find_swing_points
    before writing this (lookback=2): index 2 is the only swing low,
    index 5 the only swing high, giving one clean leg [2,5] of two big
    green candles between them.
    """
    ohlc = [
        (105, 106, 104, 105),
        (103, 104, 102, 103),
        (102, 102.5, 100, 100.2),     # swing low
        (100.2, 110, 100.1, 109),     # big green (impulse)
        (109, 120, 108.9, 119),       # big green (impulse)
        (119, 121, 118, 120),         # swing high
        (120, 121, 119, 119.5),
        (119.5, 120, 118.5, 119),
    ]
    candles = _candles(ohlc)
    legs = ps.classify_legs(candles)
    assert legs, "expected at least one leg between swing points"
    assert legs[0].kind == "impulse"
    assert legs[0].direction == "up"


def test_correction_leg_is_small_and_mixed_colored():
    """Swing points verified by hand: unique low at index 2, unique high at index 7."""
    ohlc = [
        (105, 106, 104, 105),
        (103, 104, 102, 103),
        (102, 102.5, 100.0, 100.2),     # swing low
        (100.2, 100.5, 100.1, 100.3),   # small, choppy candles between
        (100.3, 100.6, 100.2, 100.2),
        (100.2, 100.5, 100.15, 100.4),
        (100.4, 100.7, 100.25, 100.3),
        (100.3, 101.0, 100.2, 100.9),   # swing high
        (100.9, 101.0, 100.5, 100.6),
        (100.6, 100.9, 100.4, 100.5),
    ]
    candles = _candles(ohlc)
    legs = ps.classify_legs(candles)
    assert legs
    assert legs[0].kind == "correction"
    assert legs[0].color_uniformity_pct < pcfg.IMPULSE_COLOR_UNIFORMITY_PCT


def test_single_candle_legs_are_skipped():
    """A 1-candle span has no average/uniformity to measure."""
    ohlc = [(100, 101, 99, 100)] * 6
    candles = _candles(ohlc)
    legs = ps.classify_legs(candles)
    assert all(l.end_index > l.start_index for l in legs)


def test_no_swings_yields_no_legs():
    ohlc = [(100, 100.1, 99.9, 100)] * 3
    assert ps.classify_legs(_candles(ohlc)) == []


# --- zone_touch ----------------------------------------------------------

def _level(kind, low, high, note="test"):
    from models import PriceLevel
    return PriceLevel(kind=kind, low=low, high=high, note=note, strength=2.0)


def test_zone_touch_finds_price_inside_the_zone():
    zones = [_level("support", 99.0, 99.0)]
    assert ps.zone_touch(zones, 99.0) is zones[0]


def test_zone_touch_respects_tolerance_band():
    zones = [_level("support", 100.0, 100.0)]
    # 0.3% of 100 = 0.3 -> [99.7, 100.3]
    assert ps.zone_touch(zones, 100.2, tolerance_pct=0.3) is zones[0]
    assert ps.zone_touch(zones, 101.0, tolerance_pct=0.3) is None


def test_zone_touch_returns_none_when_no_zones():
    assert ps.zone_touch([], 100.0) is None


# --- latest_consolidation_box / structural_break --------------------------

def _consolidation_then_break(direction: str):
    """
    A confirmed swing (the extreme of an impulse move), a small choppy
    consolidation AFTER it, then a decisive break candle in `direction`.
    Every index verified by hand against find_swing_points/
    latest_consolidation_box before use -- see this module's docstring
    on why hand-building precise fixtures beats tuning one big scenario.
    """
    if direction == "bullish":
        return _candles([
            (120, 121, 119, 120),
            (118, 119, 116, 117),
            (115, 116, 100.0, 100.3),        # swing low candle (impulse down into it)
            (100.3, 100.6, 100.1, 100.4),    # consolidation starts AFTER the swing candle
            (100.4, 100.7, 100.2, 100.3),
            (100.3, 100.6, 100.15, 100.5),
            (100.5, 105, 100.4, 104.5),       # decisive break ABOVE the box
        ])
    return _candles([
        (100, 99, 98, 99),
        (101, 102, 100, 101),
        (103, 116, 102, 115.7),              # swing high candle (impulse up into it)
        (115.7, 115.9, 115.4, 115.6),        # consolidation starts AFTER the swing candle
        (115.6, 115.8, 115.3, 115.5),
        (115.5, 115.9, 115.2, 115.6),
        (115.6, 115.7, 110, 110.5),           # decisive break BELOW the box
    ])


def test_latest_consolidation_box_reports_the_correction_range():
    candles = _consolidation_then_break("bullish")
    box = ps.latest_consolidation_box(candles)
    assert box is not None
    assert box["box_low"] == pytest.approx(100.1)
    assert box["box_high"] == pytest.approx(100.7)


def test_structural_break_bullish_uses_box_low_as_stop():
    candles = _consolidation_then_break("bullish")
    brk = ps.structural_break(candles, "bullish")
    assert brk is not None
    assert brk["break_price"] == candles[-1].close
    assert brk["stop_reference"] == pytest.approx(brk["box_low"])
    assert brk["stop_reference"] < brk["break_price"]


def test_structural_break_bearish_uses_box_high_as_stop():
    candles = _consolidation_then_break("bearish")
    brk = ps.structural_break(candles, "bearish")
    assert brk is not None
    assert brk["stop_reference"] == pytest.approx(brk["box_high"])
    assert brk["stop_reference"] > brk["break_price"]


def test_structural_break_returns_none_without_a_real_break():
    """The consolidation exists but the latest candle never left it."""
    candles = _candles([
        (110, 110.5, 109.5, 110.1),
        (110.1, 110.6, 109.6, 109.9),
        (109.9, 110.4, 100.0, 100.0),
        (100.0, 100.3, 99.9, 100.1),
        (100.1, 100.4, 99.8, 100.0),
        (100.0, 100.3, 99.9, 100.2),
        (100.2, 100.35, 100.05, 100.25),   # still inside the box
    ])
    assert ps.structural_break(candles, "bullish") is None


def test_structural_break_none_when_no_consolidation_exists():
    assert ps.structural_break(_candles([(100, 100.1, 99.9, 100)] * 3), "bullish") is None


# --- generate_signal (full pipeline, still built from precise fixtures) --

def test_generate_signal_none_with_no_htf_zones():
    """An HTF series with no repeated touches has no zones to react from."""
    htf = _candles([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(10)])
    ltf = _consolidation_then_break("bullish")
    assert ps.generate_signal(htf, ltf) is None


def test_generate_signal_bullish_end_to_end(monkeypatch):
    """
    HTF has a real support zone touched twice right at 100.1 -- exactly
    where the LTF's verified consolidation box (box_low=100.1,
    box_high=100.7, see test_latest_consolidation_box_reports_the_
    correction_range) sits, so the box itself (not the post-breakout
    price) should register as a zone touch.
    """
    htf = _candles([
        (130, 131, 129, 130), (128, 129, 127, 128), (126, 127, 125, 126),
        (124, 125, 100.1, 100.1),      # first touch of the zone
        (105, 106, 104, 105), (110, 111, 109, 110), (115, 116, 114, 115),
        (120, 121, 119, 120), (118, 119, 100.1, 100.1),  # second touch
        (108, 109, 107, 108), (112, 113, 111, 112), (116, 117, 115, 116),
    ])
    ltf = _consolidation_then_break("bullish")
    sig = ps.generate_signal(htf, ltf)
    assert sig is not None
    assert sig.direction == "bullish"
    assert sig.zone.kind == "support"
    assert sig.stop_reference < sig.entry_reference
    assert len(sig.reasons) >= 2


def test_generate_signal_too_short_series_returns_none():
    short = _candles([(100, 101, 99, 100)] * 3)
    assert ps.generate_signal(short, short) is None
