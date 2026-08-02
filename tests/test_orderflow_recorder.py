"""
Tests for orderflow_recorder.py and spread_study.py.

The behaviour these protect is the one that motivated the module: a
single post-close capture read 0.6-1.6% and was NOT a usable estimate of
trading cost, because spreads widen when nobody is quoting. Every sample
therefore carries its market phase, and the analysis must keep phases
separate rather than blending them into one average that describes no
market anyone trades in.

Run: python -m pytest tests/ -q
"""

from datetime import datetime

import pytest

import orderflow_recorder as rec
import spread_study


# --- market phase boundaries ------------------------------------------

@pytest.mark.parametrize("clock,expected", [
    ("08:30", "pre_open"),
    ("09:14", "pre_open"),
    ("09:15", "opening"),     # session starts
    ("09:29", "opening"),
    ("09:30", "regular"),     # opening auction effects have passed
    ("12:00", "regular"),
    ("15:14", "regular"),
    ("15:15", "closing"),
    ("15:30", "closing"),     # close itself is still the session
    ("15:31", "post_close"),
    ("17:16", "post_close"),  # the capture that prompted this module
])
def test_market_phase_boundaries(clock, expected):
    ts = datetime.fromisoformat(f"2026-08-03T{clock}:00")
    assert rec.market_phase(ts) == expected


# --- sample construction ----------------------------------------------

def _contract(bid=99.0, ask=101.0, strike=24400.0, **over):
    c = {
        "strike": strike, "option_type": "CE", "ltp": 100.0,
        "volume": 500_000, "oi": 1_000_000,
        "depth": [{"bid_price": bid, "ask_price": ask, "bid_qty": 100, "ask_qty": 100,
                   "bid_orders": 5, "ask_orders": 5}],
    }
    c.update(over)
    return c


def test_spread_sample_computes_against_the_mid():
    s = rec._spread_sample(_contract(bid=99.0, ask=101.0))
    assert s["spread_pct"] == pytest.approx(2.0)
    assert s["mid"] == pytest.approx(100.0)


def test_empty_side_of_book_yields_no_sample_rather_than_a_200_percent_spread():
    """
    An empty side publishes 0.0. Treating that as a real bid would
    produce a ~200% spread -- an artifact that would drag the mean and
    make trading look far more expensive than it is.
    """
    assert rec._spread_sample(_contract(bid=0.0, ask=101.0)) is None
    assert rec._spread_sample(_contract(bid=99.0, ask=0.0)) is None


def test_crossed_book_yields_no_sample():
    assert rec._spread_sample(_contract(bid=101.0, ask=99.0)) is None


def test_missing_depth_yields_no_sample():
    assert rec._spread_sample({"strike": 24400.0, "option_type": "CE", "depth": []}) is None


def test_distance_from_spot_is_stamped_when_spot_is_known():
    s = rec._spread_sample(_contract(strike=24600.0), spot=24400.0)
    assert s["distance_from_spot"] == pytest.approx(200.0)


def test_distance_is_omitted_rather_than_faked_without_spot():
    assert "distance_from_spot" not in rec._spread_sample(_contract())


# --- recording --------------------------------------------------------

def test_record_samples_writes_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "RECORD_DIR", tmp_path)
    state = {"subscribed": 2, "receiving": 2,
             "contracts": [_contract(strike=24400.0), _contract(strike=24450.0)]}
    now = datetime.fromisoformat("2026-08-03T12:00:00")

    assert rec.record_samples(state, spot=24400.0, now=now) == 2

    rows = list(rec.load_day("2026-08-03"))
    assert len(rows) == 1
    assert rows[0]["phase"] == "regular"
    assert len(rows[0]["samples"]) == 2


def test_record_samples_never_raises_on_a_broken_write(tmp_path, monkeypatch):
    """
    Recording is observability. A full disk must not take down a feed
    that strategies may be reading -- same rule as snapshot_recorder.
    """
    monkeypatch.setattr(rec, "RECORD_DIR", tmp_path)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(rec.gzip, "open", boom)
    assert rec.record_samples({"contracts": [_contract()]}, spot=24400.0) == 0


def test_record_samples_with_nothing_priceable_writes_no_row(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "RECORD_DIR", tmp_path)
    state = {"contracts": [_contract(bid=0.0, ask=0.0)]}
    assert rec.record_samples(state, now=datetime.now()) == 0
    assert rec.available_days() == []


def test_load_day_skips_a_corrupt_trailing_line(tmp_path, monkeypatch):
    """A gzip stream cut mid-write by a kill leaves exactly this."""
    import gzip
    monkeypatch.setattr(rec, "RECORD_DIR", tmp_path)
    path = rec.path_for("2026-08-03")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write('{"timestamp": "2026-08-03T12:00:00", "phase": "regular", "samples": []}\n')
        f.write('{"timestamp": "2026-08-03T12:0')   # truncated
    assert len(list(rec.load_day("2026-08-03"))) == 1


# --- analysis ---------------------------------------------------------

def _write_day(tmp_path, monkeypatch, rows):
    import gzip
    import json
    monkeypatch.setattr(rec, "RECORD_DIR", tmp_path)
    with gzip.open(rec.path_for("2026-08-03"), "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _row(phase, spreads, mid=100.0):
    return {
        "timestamp": "2026-08-03T12:00:00", "phase": phase, "spot": 24400.0,
        "samples": [{"strike": 24400.0, "option_type": "CE", "mid": mid,
                     "spread_pct": s, "bid": mid - s / 2, "ask": mid + s / 2}
                    for s in spreads],
    }


def test_collect_keeps_phases_separate(tmp_path, monkeypatch):
    """
    The whole point: a post-close spread must never be averaged into the
    figure used to judge trading cost.
    """
    _write_day(tmp_path, monkeypatch, [
        _row("regular", [0.30, 0.40]),
        _row("post_close", [1.50, 1.60]),
    ])
    c = spread_study.collect(["2026-08-03"])

    assert sorted(c["by_phase"]["regular"]) == [0.30, 0.40]
    assert sorted(c["by_phase"]["post_close"]) == [1.50, 1.60]
    # `regular` is the series everything downstream uses.
    assert sorted(c["regular"]) == [0.30, 0.40]


def test_premium_bands_only_use_regular_session_samples(tmp_path, monkeypatch):
    _write_day(tmp_path, monkeypatch, [
        _row("regular", [0.30], mid=75.0),
        _row("post_close", [2.00], mid=75.0),
    ])
    c = spread_study.collect(["2026-08-03"])
    assert c["by_premium"]["50-100"] == [0.30]


def test_strategy_band_filters_to_that_strategy_premium_range(tmp_path, monkeypatch):
    """
    A chain-wide average applies to no strategy. Each one's figure must
    come from contracts inside its own configured premium band.
    """
    import config_directional_spread as dcfg
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MIN", 40.0)
    monkeypatch.setattr(dcfg, "SHORT_PREMIUM_MAX", 70.0)

    _write_day(tmp_path, monkeypatch, [
        _row("regular", [0.50], mid=55.0),    # inside the spread's band
        _row("regular", [9.90], mid=500.0),   # far outside it
    ])
    bands = spread_study.strategy_band_spread(spread_study.collect(["2026-08-03"]))
    spread_band = bands["directional spread (SHORT_PREMIUM_MIN/MAX)"]
    assert spread_band["n"] == 1
    assert spread_band["median"] == pytest.approx(0.50)


def test_describe_refuses_to_report_a_number_from_post_close_only_data(tmp_path, monkeypatch):
    _write_day(tmp_path, monkeypatch, [_row("post_close", [1.50, 1.60])])
    c = spread_study.collect(["2026-08-03"])
    assert c["regular"] == []
    text = spread_study.describe(c)
    assert "post_close" in text


def test_describe_with_no_recordings_says_so():
    assert "No order-flow recordings yet" in spread_study.describe(
        {"days": [], "rows": 0, "by_phase": {}, "by_premium": {},
         "by_distance": {}, "regular": []})
