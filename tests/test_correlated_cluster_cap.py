"""
Tests for shadow.correlated_cluster_blocked() -- a BACKTEST-ONLY gate,
not wired into risk_checker.check() or any live process. Investigated
after finding real sessions where the scanner fired the same
underlying signal repeatedly across adjacent strikes within minutes:
NIFTY 2026-08-12 (23 trades, all tagged "support", in three bursts of
5-11 adjacent strikes) and Bank Nifty 2026-08-14 (6 adjacent PE
strikes, all reaching ~0.75R together then reversing together).
MAX_TOTAL_EXPOSURE_PCT never caught this -- it only sums risk rupees,
and seven small positions on adjacent strikes are each too small
individually to breach it.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import pytest

import shadow
from models import Setup


def _setup(strike=24050.0, option_type="PE"):
    return Setup("NIFTY", strike, option_type, "2026-08-13", ["support"], 6.0)


def _position(strike, option_type, opened_ts, closed_ts=None):
    return {"key": (strike, option_type), "opened_ts": opened_ts, "closed_ts": closed_ts,
            "entry": 100.0, "stop": 70.0, "lots": 1, "net_inr": 0.0}


T0 = datetime(2026, 8, 12, 10, 15)
T1 = datetime(2026, 8, 12, 10, 16)
T2 = datetime(2026, 8, 12, 10, 20)


# --------------------------------------------------------------------------
# Disabled by default -- must reproduce today's real, uncapped behaviour
# --------------------------------------------------------------------------

def test_both_caps_none_never_blocks():
    policy = shadow.Policy()  # defaults: both None
    positions = [_position(24000.0, "PE", T0)]
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is False


# --------------------------------------------------------------------------
# max_open_per_direction
# --------------------------------------------------------------------------

def test_max_open_per_direction_blocks_at_the_limit():
    policy = shadow.Policy(max_open_per_direction=1)
    positions = [_position(23850.0, "PE", T0)]   # 1 already open
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is True


def test_max_open_per_direction_allows_under_the_limit():
    policy = shadow.Policy(max_open_per_direction=2)
    positions = [_position(23850.0, "PE", T0)]   # only 1 open, limit is 2
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is False


def test_max_open_per_direction_is_direction_specific():
    """Two open PE positions must not block a NEW CE candidate -- the
    whole point is these are independent bets, only same-direction
    pile-ups are the problem."""
    policy = shadow.Policy(max_open_per_direction=1)
    positions = [_position(23850.0, "PE", T0), _position(24200.0, "PE", T0)]
    assert shadow.correlated_cluster_blocked(_setup(24300.0, "CE"), positions, T1, policy) is False


def test_closed_positions_dont_count(monkeypatch):
    policy = shadow.Policy(max_open_per_direction=1)
    positions = [_position(23850.0, "PE", T0, closed_ts=T1)]   # closed before ts
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T2, policy) is False


def test_not_yet_opened_positions_dont_count():
    """Causality: a position opened LATER than `ts` must not count as
    already-open -- resolving trades instantly in memory must not leak
    look-ahead information into this gate."""
    policy = shadow.Policy(max_open_per_direction=1)
    positions = [_position(23850.0, "PE", T2)]   # opens AFTER ts
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T0, policy) is False


# --------------------------------------------------------------------------
# strike_adjacency_band_points
# --------------------------------------------------------------------------

def test_adjacency_blocks_a_nearby_same_type_strike():
    policy = shadow.Policy(strike_adjacency_band_points=200)
    positions = [_position(23900.0, "PE", T0)]
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is True  # 150pt away


def test_adjacency_allows_a_far_same_type_strike():
    policy = shadow.Policy(strike_adjacency_band_points=200)
    positions = [_position(23000.0, "PE", T0)]
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is False  # 1050pt away


def test_adjacency_ignores_the_opposite_direction_regardless_of_distance():
    policy = shadow.Policy(strike_adjacency_band_points=200)
    positions = [_position(24060.0, "CE", T0)]   # 10pt away but opposite direction
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is False


def test_adjacency_is_inclusive_at_the_boundary():
    """Distance exactly equal to the band DOES block ('<= band').

    This test asserted the opposite until 2026-08-17, when the live
    session proved the exclusive version silently never fired for Bank
    Nifty at all: its strikes sit 100pts apart and the scanner's bursts
    skip every other one, so candidates land EXACTLY 200pts apart -- the
    configured band -- and `200 < 200` is False. Sentinel opened 5
    strikes of a 9-strike correlated PE cluster it exists to block. The
    exact-boundary case is the TYPICAL case here, not an edge case.
    """
    policy = shadow.Policy(strike_adjacency_band_points=200)
    positions = [_position(23850.0, "PE", T0)]   # exactly 200pt away
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is True


def test_just_outside_the_band_still_does_not_block():
    """The band still has an outer edge -- inclusive at exactly `band`,
    but anything beyond it is genuinely unrelated."""
    policy = shadow.Policy(strike_adjacency_band_points=200)
    positions = [_position(23849.0, "PE", T0)]   # 201pt away
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, T1, policy) is False


# --------------------------------------------------------------------------
# Reproducing the real 2026-08-12 shape directly
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# cluster_window_minutes (Sentinel v1.1-dev): narrows either cap above to
# only apply while the blocking position is RECENTLY opened, instead of
# for its entire open lifetime.
# --------------------------------------------------------------------------

def test_window_none_matches_original_untimed_behaviour():
    """Explicitly confirms the refinement doesn't change anything when
    left off -- Sentinel's window is additive, not a silent behaviour
    change to the two original caps."""
    policy = shadow.Policy(max_open_per_direction=1, cluster_window_minutes=None)
    positions = [_position(23850.0, "PE", T0)]
    long_after = datetime(2026, 8, 12, 15, 0)   # ~5 hours later, still "open"
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, long_after, policy) is True


def test_window_stops_blocking_once_the_position_is_stale():
    """The exact fix this refinement is for: a position opened hours
    ago must stop counting as 'the same burst', even though it may
    still technically be open (an EOD-outcome trade can sit open for
    hours)."""
    policy = shadow.Policy(max_open_per_direction=1, cluster_window_minutes=15)
    positions = [_position(23850.0, "PE", T0)]   # opened 10:15
    long_after = datetime(2026, 8, 12, 15, 0)     # ~5 hours later, still open, but stale
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, long_after, policy) is False


def test_window_still_blocks_within_the_window():
    policy = shadow.Policy(max_open_per_direction=1, cluster_window_minutes=15)
    positions = [_position(23850.0, "PE", T0)]   # opened 10:15
    ten_min_later = datetime(2026, 8, 12, 10, 25)
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, ten_min_later, policy) is True


def test_window_applies_to_adjacency_cap_too():
    policy = shadow.Policy(strike_adjacency_band_points=200, cluster_window_minutes=15)
    positions = [_position(23900.0, "PE", T0)]   # 150pt away, opened 10:15
    long_after = datetime(2026, 8, 12, 15, 0)
    assert shadow.correlated_cluster_blocked(_setup(24050.0, "PE"), positions, long_after, policy) is False


def test_reproduces_the_real_2026_08_12_morning_burst():
    """
    The actual real burst: 7 adjacent PE strikes (23850..24150, 50pt
    apart) opened within 4 minutes. A band wide enough to cover that
    full spread must block every strike after the first.
    """
    policy = shadow.Policy(strike_adjacency_band_points=350)
    strikes = [23850.0, 23900.0, 23950.0, 24000.0, 24050.0, 24100.0, 24150.0]
    positions = []
    ts = T0
    blocked_count = 0
    for strike in strikes:
        if shadow.correlated_cluster_blocked(_setup(strike, "PE"), positions, ts, policy):
            blocked_count += 1
        else:
            positions.append(_position(strike, "PE", ts))
    assert blocked_count == len(strikes) - 1   # only the first ever opens
    assert len(positions) == 1
