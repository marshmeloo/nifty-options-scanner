"""
Tests for snapshot recording, logic versioning, and replay.

These underpin every future "did that change help?" question, so their
failure modes matter more than most: a recorder that silently drops a
field, or a version stamp that doesn't change when behaviour does, would
quietly corrupt every later analysis rather than raising an error.

Run: python -m pytest tests/ -q
"""

import datetime
import gzip
import json

import pytest

import config
import logic_version
import snapshot_recorder as sr
from models import Candle, MarketSnapshot, OptionQuote


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    """Redirect recording at a temp dir so tests never touch real history."""
    d = tmp_path / "snapshots"
    monkeypatch.setattr(sr, "SNAPSHOT_DIR", d)
    return d


def _quote(strike=24000.0, opt="PE", **kw):
    base = dict(
        symbol="NIFTY", expiry="2026-08-04", strike=strike, option_type=opt,
        ltp=76.7, oi=50000, oi_change_pct=217.7, volume=1200, iv=12.5,
        iv_percentile=12.0, delta=-0.45, theta=-6.2, vega=4.1,
        price_change_pct=2.8, buildup_type="long_buildup",
        oi_change_pct_intraday=3.1, price_change_pct_intraday=1.4,
        buildup_window_minutes=15.0,
        timestamp=datetime.datetime(2026, 7, 28, 11, 4, 0),
    )
    base.update(kw)
    return OptionQuote(**base)


def _snapshot(ts=None, chain=None):
    ts = ts or datetime.datetime(2026, 7, 28, 11, 4, 0)
    return MarketSnapshot(
        symbol="NIFTY", spot=24010.5, vwap=24008.0, pcr=0.94,
        chain=chain if chain is not None else [_quote(), _quote(opt="CE", delta=0.5)],
        timestamp=ts, source="dhan",
    )


def _candles(n=3):
    t0 = datetime.datetime(2026, 7, 28, 9, 15)
    return [
        Candle(t0 + datetime.timedelta(minutes=5 * i), 24000.0 + i, 24010.0 + i,
               23990.0 + i, 24005.0 + i, 1000 + i)
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# Recording round-trip
# --------------------------------------------------------------------------

def test_record_and_load_round_trip(snapshot_dir):
    snap, candles = _snapshot(), _candles()
    assert sr.record(snap, candles, "v1") is True

    loaded = list(sr.load_day("2026-07-28"))
    assert len(loaded) == 1
    got_snap, got_candles, meta = loaded[0]

    assert got_snap.spot == snap.spot
    assert got_snap.pcr == snap.pcr
    assert got_snap.timestamp == snap.timestamp
    assert meta["logic_version"] == "v1"
    assert len(got_candles) == 3
    assert got_candles[0].timestamp == candles[0].timestamp


def test_all_decision_relevant_quote_fields_survive(snapshot_dir):
    """
    The recorded chain must carry everything downstream logic reads --
    including the intraday OI fields and Greeks, which are the inputs to
    changes we already know we want to make (theta-aware targets, smile
    residual IV). A field silently lost here is a replay that lies.
    """
    sr.record(_snapshot(), _candles(), "v1")
    got = list(sr.load_day("2026-07-28"))[0][0].chain[0]
    original = _quote()

    for field in ("strike", "option_type", "ltp", "oi", "oi_change_pct", "volume",
                  "iv", "iv_percentile", "delta", "theta", "vega",
                  "price_change_pct", "buildup_type",
                  "oi_change_pct_intraday", "price_change_pct_intraday",
                  "buildup_window_minutes"):
        assert getattr(got, field) == getattr(original, field), f"{field} lost in round-trip"


def test_appending_multiple_cycles_preserves_order(snapshot_dir):
    for minute in (4, 5, 6):
        sr.record(_snapshot(ts=datetime.datetime(2026, 7, 28, 11, minute, 0)), [], "v1")
    stamps = [s.timestamp.minute for s, _c, _m in sr.load_day("2026-07-28")]
    assert stamps == [4, 5, 6]


def test_unknown_future_fields_are_ignored_on_load(snapshot_dir):
    """
    A recording made by a NEWER version with extra quote fields must still
    load, or upgrading the model would orphan all prior history.
    """
    sr.record(_snapshot(), [], "v1")
    path = sr.path_for("2026-07-28")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rec = json.loads(f.read().strip())
    rec["chain"][0]["some_future_field"] = 123
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

    loaded = list(sr.load_day("2026-07-28"))
    assert len(loaded) == 1
    assert loaded[0][0].chain[0].strike == 24000.0


def test_corrupt_line_is_skipped_not_fatal(snapshot_dir):
    """A kill mid-write leaves a truncated line; one bad cycle must not cost the day."""
    sr.record(_snapshot(ts=datetime.datetime(2026, 7, 28, 11, 4)), [], "v1")
    with gzip.open(sr.path_for("2026-07-28"), "at", encoding="utf-8") as f:
        f.write('{"timestamp": "2026-07-28T11:05:00", "spot": \n')   # truncated
    sr.record(_snapshot(ts=datetime.datetime(2026, 7, 28, 11, 6)), [], "v1")

    loaded = list(sr.load_day("2026-07-28"))
    assert len(loaded) == 2
    assert [s.timestamp.minute for s, _c, _m in loaded] == [4, 6]


def test_recording_never_raises_on_failure(snapshot_dir, monkeypatch):
    """
    Recording is observability. It must never take down a loop that may
    be managing open positions.
    """
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(sr.gzip, "open", boom)
    assert sr.record(_snapshot(), [], "v1") is False   # no exception


def test_recording_can_be_disabled(snapshot_dir, monkeypatch):
    monkeypatch.setattr(config, "RECORD_SNAPSHOTS", False)
    assert sr.record(_snapshot(), [], "v1") is False
    assert sr.available_days() == []


def test_available_days_sorted(snapshot_dir):
    for day in (28, 27, 29):
        sr.record(_snapshot(ts=datetime.datetime(2026, 7, day, 11, 0)), [], "v1")
    assert sr.available_days() == ["2026-07-27", "2026-07-28", "2026-07-29"]


# --------------------------------------------------------------------------
# Logic versioning
# --------------------------------------------------------------------------

def test_version_changes_when_decision_config_changes(monkeypatch):
    logic_version.compute.cache_clear()
    before = logic_version.compute()

    monkeypatch.setattr(config, "MIN_CONVICTION_SCORE_TO_TRACK", 99.0)
    logic_version.compute.cache_clear()
    after = logic_version.compute()

    assert before != after, "a changed conviction bar must produce a new logic version"


def test_version_stable_for_operational_only_changes(monkeypatch):
    """
    Polling intervals and cache durations can't change a trade. Churning
    the version on those would fragment samples for no reason.
    """
    logic_version.compute.cache_clear()
    before = logic_version.compute()

    monkeypatch.setattr(config, "NEWS_CACHE_MINUTES", 999)
    monkeypatch.setattr(config, "FAST_CHECK_INTERVAL_SECONDS", 99)
    logic_version.compute.cache_clear()
    after = logic_version.compute()

    assert before == after


def test_version_is_deterministic():
    logic_version.compute.cache_clear()
    a = logic_version.compute()
    logic_version.compute.cache_clear()
    b = logic_version.compute()
    assert a == b


def test_fingerprint_covers_the_key_decision_knobs():
    fp = logic_version.config_fingerprint()
    for critical in ("MIN_CONVICTION_SCORE_TO_TRACK", "DEFAULT_TARGET_RR",
                     "STOP_ATR_MULTIPLE", "MAX_DAILY_LOSS_PCT",
                     "OI_INTRADAY_LOOKBACK_MINUTES", "MAX_LEVEL_SCORE_CONTRIBUTION"):
        assert critical in fp


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------

def test_replay_is_deterministic(snapshot_dir):
    import replay
    for minute in range(0, 30, 5):
        sr.record(_snapshot(ts=datetime.datetime(2026, 7, 28, 11, minute)), _candles(12), "v1")

    a = replay.replay_day("2026-07-28")
    b = replay.replay_day("2026-07-28")
    assert a["cycles"] == b["cycles"] == 6
    assert a["opened"] == b["opened"], "same inputs + same logic must give identical decisions"


def test_replay_of_empty_day_is_safe(snapshot_dir):
    import replay
    result = replay.replay_day("2026-07-28")
    assert result["cycles"] == 0
    assert result["opened"] == []


def test_compare_reports_added_and_removed_trades():
    import replay
    baseline = {"logic_version": "a", "opened": [
        {"opened_at": "2026-07-28T11:04:00", "strike": 24050.0, "option_type": "PE",
         "entry": 76.7, "target": 122.7, "stop": 53.7},
    ]}
    current = {"logic_version": "b", "opened": [
        {"opened_at": "2026-07-28T12:00:00", "strike": 24000.0, "option_type": "CE",
         "entry": 50.0, "target": 80.0, "stop": 35.0},
    ]}
    out = replay.compare(baseline, current)
    assert "NO LONGER opened (1)" in out
    assert "NEWLY opened (1)" in out
    assert "24050.0 PE" in out and "24000.0 CE" in out


def test_compare_flags_changed_geometry_on_same_trade():
    import replay
    trade = {"opened_at": "2026-07-28T11:04:00", "strike": 24050.0, "option_type": "PE",
             "entry": 76.7, "target": 122.7, "stop": 53.7}
    changed = {**trade, "target": 103.7, "stop": 63.2}
    out = replay.compare({"logic_version": "a", "opened": [trade]},
                         {"logic_version": "b", "opened": [changed]})
    assert "CHANGED geometry (1)" in out
    assert "122.7->103.7" in out
