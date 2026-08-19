"""
Tests for research/stocks_in_play.py's selection logic.

The property that matters most here is LOOK-AHEAD SAFETY. The whole
study rests on reconstructing "which stocks were in play this morning"
from bars, and a selection that peeks even slightly into the rest of the
day would produce a spectacular, entirely fake edge -- the single most
likely way this research goes wrong. These tests pin that the RVOL
denominator sees only PRIOR days, the numerator sees only the opening
window, and forward outcomes are measured strictly after the selection
point.

Run: python -m pytest tests/test_stocks_in_play.py -q
"""

import pytest

from research import stocks_in_play as sip


def _bars(open_vol_each=1000, rest_vol=500, o=100.0, drift=0.0, n_after=40):
    """A day: 3 opening bars (09:15/20/25) then `n_after` more."""
    out = []
    mins = 9 * 60 + 15
    for i in range(3):
        t = f"{mins // 60:02d}:{mins % 60:02d}"
        px = o + drift * i
        out.append([t, px, px + 0.5, px - 0.5, px + drift, open_vol_each])
        mins += 5
    px = o + drift * 3
    for i in range(n_after):
        t = f"{mins // 60:02d}:{mins % 60:02d}"
        out.append([t, px, px + 0.5, px - 0.5, px, rest_vol])
        mins += 5
    return out


def _data(n_days=25, **kw):
    return {f"2026-01-{d:02d}": _bars(**kw) for d in range(1, n_days + 1)}


# --------------------------------------------------------------------------
# Opening window
# --------------------------------------------------------------------------

def test_opening_slice_is_exactly_the_first_15_minutes():
    bars = _bars()
    head = sip.opening_slice(bars, 15)
    assert [b[0] for b in head] == ["09:15", "09:20", "09:25"]


def test_opening_slice_excludes_the_bar_at_the_boundary():
    """15 minutes from 09:15 ends at 09:30 EXCLUSIVE -- the 09:30 bar
    covers 09:30->09:35 and is after the decision point."""
    head = sip.opening_slice(_bars(), 15)
    assert "09:30" not in [b[0] for b in head]


# --------------------------------------------------------------------------
# Look-ahead safety -- the critical property
# --------------------------------------------------------------------------

def test_rvol_baseline_uses_only_prior_days():
    """
    Day N's RVOL denominator must not include day N's own volume. Built
    so every prior day has volume 1000 and the LAST day has a huge
    10000: if the current day leaked into its own baseline, the computed
    RVOL would be dragged toward 1.0 instead of showing the spike.
    """
    data = _data(n_days=20)
    spike_day = "2026-01-21"
    data[spike_day] = _bars(open_vol_each=10000)

    rows = {r["day"]: r for r in sip.day_rows("X", data)}
    assert spike_day in rows
    # 10000*3 opening volume against a 1000*3 baseline -> ~10x, not diluted.
    assert rows[spike_day]["rvol"] == pytest.approx(10.0, rel=0.05)


def test_no_row_emitted_before_enough_lookback_exists():
    """The first days of a series cannot have an RVOL, and must be
    dropped rather than given a guessed one."""
    rows = sip.day_rows("X", _data(n_days=25))
    days = sorted(r["day"] for r in rows)
    assert days[0] >= f"2026-01-{sip.MIN_LOOKBACK_DAYS + 1:02d}"


def test_rvol_numerator_ignores_volume_after_the_opening_window():
    """Volume later in the day must not affect the morning's read."""
    quiet = _data(n_days=20)
    quiet["2026-01-21"] = _bars(open_vol_each=1000, rest_vol=1)
    loud = _data(n_days=20)
    loud["2026-01-21"] = _bars(open_vol_each=1000, rest_vol=999999)

    a = {r["day"]: r for r in sip.day_rows("X", quiet)}["2026-01-21"]
    b = {r["day"]: r for r in sip.day_rows("X", loud)}["2026-01-21"]
    assert a["rvol"] == pytest.approx(b["rvol"])


def test_forward_return_is_measured_from_the_selection_point_not_the_open():
    """
    The tradeable move is what happens AFTER the decision. A day that
    gapped up hard before 09:30 and then went nowhere must show ~0
    forward return, not the gap.
    """
    data = _data(n_days=20)
    # Opening bars drift +1 each, then flat for the rest of the day.
    data["2026-01-21"] = _bars(drift=1.0)
    row = {r["day"]: r for r in sip.day_rows("X", data)}["2026-01-21"]

    assert row["open_ret_pct"] > 0          # the pre-decision move IS recorded
    assert row["fwd_eod_pct"] == pytest.approx(0.0, abs=0.6)   # but not counted as forward return


def test_forward_excursions_only_consider_bars_after_selection():
    data = _data(n_days=20)
    bars = _bars()
    # A huge spike INSIDE the opening window must not appear as forward upside.
    bars[1][2] = 500.0
    data["2026-01-21"] = bars
    row = {r["day"]: r for r in sip.day_rows("X", data)}["2026-01-21"]
    assert row["fwd_max_pct"] < 10, "an opening-window spike must not count as a forward excursion"


# --------------------------------------------------------------------------
# Profile / distribution reporting
# --------------------------------------------------------------------------

def _row(fwd, rvol=4.0, open_ret=2.0):
    return {"day": "d", "symbol": "X", "rvol": rvol, "open_ret_pct": open_ret,
            "ref_price": 100.0, "fwd_eod_pct": fwd, "fwd_max_pct": max(fwd, 0),
            "fwd_min_pct": min(fwd, 0)}


def test_profile_reports_zero_rows_safely():
    assert sip.profile([], "empty")["n"] == 0


def test_win_loss_ratio_captures_win_large_lose_small():
    """Three small losses and one big win -> low win rate but W/L well
    above 1, which is exactly the shape the hypothesis claims."""
    rows = [_row(-1), _row(-1), _row(-1), _row(+9)]
    p = sip.profile(rows, "t")
    assert p["win_pct"] == 25.0
    assert p["win_loss_ratio"] == pytest.approx(9.0)
    assert p["skew"] > 0


def test_symmetric_returns_give_no_skew_and_unit_ratio():
    rows = [_row(+2), _row(-2), _row(+2), _row(-2)]
    p = sip.profile(rows, "t")
    assert p["win_loss_ratio"] == pytest.approx(1.0)
    assert p["skew"] == pytest.approx(0.0, abs=0.01)


def test_analyse_buckets_by_rvol_without_dropping_rows():
    rows = ([_row(1.0, rvol=0.5)] * 5 + [_row(1.0, rvol=1.5)] * 5 +
            [_row(1.0, rvol=2.5)] * 5 + [_row(1.0, rvol=4.0)] * 5 +
            [_row(1.0, rvol=9.0)] * 5)
    summary = sip.analyse(rows)
    assert summary["n_rows"] == 25
    assert sum(b["n"] for b in summary["buckets"]) == 25
