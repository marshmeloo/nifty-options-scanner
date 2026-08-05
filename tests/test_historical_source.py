"""
Tests for historical_source.py -- reconstructing MarketSnapshots from
Dhan's Expired Options Data API.

These pin the things that would silently corrupt a backtest rather than
fail it: misaligned parallel arrays, and the ATM+/-10 offset cap that the
API enforces by returning HTTP 200 with an empty body instead of an error.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

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


# --- Bank Nifty parameterization: security_id/symbol/snapshot_dir ----------

def test_reconstruct_range_stamps_the_given_symbol(monkeypatch):
    """
    Added for Bank Nifty support: symbol must flow through to both the
    snapshot AND every quote in its chain, not just one or the other --
    a mismatch would make a backtest reading this data misidentify which
    underlying it's actually looking at.
    """
    monkeypatch.setattr(hs, "fetch_series", lambda o, t, *a, **kw: _block(57500, 57535.2, [852.6]))
    snap = hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=0,
                                security_id=hs.BANKNIFTY_SECURITY_ID, symbol="BANKNIFTY")[0]
    assert snap.symbol == "BANKNIFTY"
    assert all(q.symbol == "BANKNIFTY" for q in snap.chain)


def test_reconstruct_range_passes_security_id_to_fetch_series(monkeypatch):
    seen = []
    def fake_fetch(offset, option_type, *a, security_id=None, **kw):
        seen.append(security_id)
        return _block(57500, 57535.2, [852.6])
    monkeypatch.setattr(hs, "fetch_series", fake_fetch)
    hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=0, security_id=hs.BANKNIFTY_SECURITY_ID)
    assert all(sid == hs.BANKNIFTY_SECURITY_ID for sid in seen)


def test_backfill_writes_to_a_custom_snapshot_dir_without_touching_the_default(monkeypatch, tmp_path):
    """
    Pins the exact collision this parameterization exists to prevent:
    writing a second underlying's history into the SAME default
    directory would silently overwrite NIFTY's real recordings for any
    overlapping date, since both share the same YYYYMMDD.jsonl.gz naming.
    """
    import snapshot_recorder as sr

    default_dir = tmp_path / "default_nifty_snapshots"
    bn_dir = tmp_path / "banknifty_snapshots"
    monkeypatch.setattr(sr, "SNAPSHOT_DIR", default_dir)

    monkeypatch.setattr(hs, "reconstruct_range", lambda *a, **kw: [
        type("S", (), {
            "timestamp": datetime(2026, 7, 1, 9, 20), "spot": 57500.0, "vwap": 57500.0,
            "pcr": 1.0, "source": "dhan_historical", "chain": [],
        })()
    ])
    monkeypatch.setattr("historical_consistency.check_range", lambda *a, **kw: {"passed": True, "failed_days": []})
    monkeypatch.setattr("historical_consistency.describe", lambda *a, **kw: "")

    written = hs.backfill("2026-07-01", "2026-07-01", snapshot_dir=bn_dir, symbol="BANKNIFTY")

    assert written == {"2026-07-01": 1}
    assert (bn_dir / "20260701.jsonl.gz").exists()
    assert not default_dir.exists() or not list(default_dir.glob("*.jsonl.gz"))


def test_load_day_respects_custom_snapshot_dir_and_symbol(tmp_path):
    """snapshot_recorder's own parameterization, exercised end-to-end
    through a real write/read round trip rather than mocked."""
    import snapshot_recorder as sr
    import json, gzip

    bn_dir = tmp_path / "banknifty_snapshots"
    bn_dir.mkdir()
    path = sr.path_for("2026-07-01", snapshot_dir=bn_dir)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-07-01T09:20:00", "logic_version": None,
            "spot": 57500.0, "vwap": 57500.0, "pcr": 1.0, "source": "dhan_historical",
            "chain": [], "candles": [],
        }) + "\n")

    assert sr.available_days(snapshot_dir=bn_dir) == ["2026-07-01"]
    cycles = list(sr.load_day("2026-07-01", snapshot_dir=bn_dir, symbol="BANKNIFTY"))
    assert len(cycles) == 1
    assert cycles[0][0].symbol == "BANKNIFTY"


def test_load_day_default_behaviour_is_unchanged(tmp_path, monkeypatch):
    """No snapshot_dir/symbol given -> exact old behaviour (default dir, "NIFTY")."""
    import snapshot_recorder as sr
    import json, gzip

    monkeypatch.setattr(sr, "SNAPSHOT_DIR", tmp_path)
    path = sr.path_for("2026-07-01")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-07-01T09:20:00", "logic_version": None,
            "spot": 24000.0, "vwap": 24000.0, "pcr": 1.0, "source": "dhan_historical",
            "chain": [], "candles": [],
        }) + "\n")

    cycles = list(sr.load_day("2026-07-01"))
    assert cycles[0][0].symbol == "NIFTY"


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


def _write_day(tmp_path, monkeypatch, day, source="dhan_historical", candles=None):
    """Put one reconstructed day on disk in the recorder's own format."""
    import gzip
    import json
    import snapshot_recorder

    monkeypatch.setattr(snapshot_recorder, "SNAPSHOT_DIR", tmp_path)
    path = snapshot_recorder.path_for(day)
    base = datetime.fromisoformat(f"{day}T09:20:00")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({
                "timestamp": (base + timedelta(minutes=5 * i)).isoformat(),
                "logic_version": None,
                "spot": 24000.0, "vwap": 24000.0, "pcr": 1.0, "source": source,
                "chain": [], "candles": candles or [],
            }) + "\n")
    return path


def test_backfill_candles_never_shows_a_cycle_its_own_future(tmp_path, monkeypatch):
    """
    A cycle at 09:25 must not see the 09:30 candle. Live, main_live.py can
    only ever pass candles up to 'now'; a backtest that leaked later ones
    would be reading the future and every result would be worthless.
    """
    import snapshot_recorder
    from models import Candle

    day = "2026-06-01"
    _write_day(tmp_path, monkeypatch, day)

    base = datetime.fromisoformat(f"{day}T09:20:00")
    fake = [Candle(timestamp=base + timedelta(minutes=5 * i), open=1.0, high=2.0,
                   low=0.5, close=1.5, volume=10) for i in range(5)]

    import dhan_source
    monkeypatch.setattr(dhan_source, "get_nifty_intraday_candles", lambda **kw: fake)
    hs.backfill_candles(days=[day])

    cycles = list(snapshot_recorder.load_day(day))
    assert len(cycles) == 3
    for snap, candles, _meta in cycles:
        assert candles, "every cycle should have gained candles"
        assert max(c.timestamp for c in candles) <= snap.timestamp


def test_backfill_candles_refuses_to_touch_live_recordings(tmp_path, monkeypatch):
    """
    A live day already holds the candles main_live.py saw at the time.
    Rewriting it would replace what actually happened with a re-fetch.
    """
    import dhan_source
    import snapshot_recorder

    day = "2026-06-02"
    _write_day(tmp_path, monkeypatch, day, source="dhan")

    called = []
    monkeypatch.setattr(dhan_source, "get_nifty_intraday_candles",
                        lambda **kw: called.append(1) or [])
    assert hs.backfill_candles(days=[day]) == {}
    assert not called, "must not even fetch candles for a live-recorded day"


def test_backfill_candles_skips_already_enriched_days(tmp_path, monkeypatch):
    import dhan_source

    day = "2026-06-03"
    existing = [{"timestamp": f"{day}T09:20:00", "open": 1.0, "high": 2.0,
                 "low": 0.5, "close": 1.5, "volume": 10}]
    _write_day(tmp_path, monkeypatch, day, candles=existing)

    called = []
    monkeypatch.setattr(dhan_source, "get_nifty_intraday_candles",
                        lambda **kw: called.append(1) or [])
    assert hs.backfill_candles(days=[day]) == {}
    assert not called


def test_backfill_candles_leaves_day_intact_when_fetch_fails(tmp_path, monkeypatch):
    """A failed candle fetch must not destroy the option chain already on disk."""
    import dhan_source
    import snapshot_recorder

    day = "2026-06-04"
    _write_day(tmp_path, monkeypatch, day)

    def boom(**kw):
        raise RuntimeError("api down")

    monkeypatch.setattr(dhan_source, "get_nifty_intraday_candles", boom)
    assert hs.backfill_candles(days=[day]) == {}
    assert len(list(snapshot_recorder.load_day(day))) == 3


def test_iv_percentile_is_ranked_not_left_flat(monkeypatch):
    """
    scanner.py scores IV rich/cheap off iv_percentile and nothing else
    reads it. A constant 50.0 sits between IV_PERCENTILE_LOW and HIGH, so
    neither branch could ever fire and the backtest would evaluate a
    momentum scorer with its IV component amputated.
    """
    def fake_fetch(offset, option_type, *a, **kw):
        block = _block(24000 + offset * 50, 24000.0, [100.0])
        block["iv"] = [10.0 + offset]  # a spread of IVs across strikes
        return block

    monkeypatch.setattr(hs, "fetch_series", fake_fetch)
    snap = hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=5)[0]

    ce = [q for q in snap.chain if q.option_type == "CE"]
    pcts = sorted(q.iv_percentile for q in ce)
    assert len(set(pcts)) > 1, "percentiles must vary across strikes"
    assert pcts[0] < 50.0 < pcts[-1]
    # Lowest IV ranks lowest, highest ranks 100.
    assert max(ce, key=lambda q: q.iv).iv_percentile == 100.0


def test_iv_percentile_stays_neutral_when_too_few_strikes(monkeypatch):
    """Mirrors dhan_source's <5-strike bail-out rather than inventing a rank."""
    monkeypatch.setattr(hs, "fetch_series",
                        lambda o, t, *a, **kw: _block(24000 + o * 50, 24000.0, [100.0]))
    snap = hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=1)[0]
    assert all(q.iv_percentile == 50.0 for q in snap.chain)


def test_iv_percentile_ranks_ce_and_pe_independently(monkeypatch):
    """Live ranks each option type against its own side; a pooled rank would differ."""
    def fake_fetch(offset, option_type, *a, **kw):
        block = _block(24000 + offset * 50, 24000.0, [100.0])
        # PE IVs sit entirely above CE IVs; if pooled, every PE would rank high.
        block["iv"] = [10.0 + offset if option_type == "CE" else 50.0 + offset]
        return block

    monkeypatch.setattr(hs, "fetch_series", fake_fetch)
    snap = hs.reconstruct_range("2026-07-01", "2026-07-02", max_offset=5)[0]

    for opt in ("CE", "PE"):
        side = [q for q in snap.chain if q.option_type == opt]
        assert max(q.iv_percentile for q in side) == 100.0
        assert min(q.iv_percentile for q in side) < 50.0


def _buildup_cycles(oi_series, ltp_series, start="2026-06-01T09:20:00"):
    """One strike tracked across cycles 5 minutes apart."""
    from models import MarketSnapshot, OptionQuote

    base = datetime.fromisoformat(start)
    cycles = []
    for i, (oi, ltp) in enumerate(zip(oi_series, ltp_series)):
        ts = base + timedelta(minutes=5 * i)
        q = OptionQuote(symbol="NIFTY", expiry="rolling:week1", strike=24000.0,
                        option_type="CE", ltp=ltp, oi=oi, oi_change_pct=0.0,
                        volume=10, iv=15.0, iv_percentile=50.0, timestamp=ts)
        cycles.append((MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0,
                                      pcr=1.0, chain=[q], timestamp=ts,
                                      source="dhan_historical"), [], {}))
    return cycles


def test_oi_buildup_is_classified_from_sequential_snapshots():
    """
    OI buildup is the momentum scanner's largest scoring input -- it fired
    5,517 times on a live day and zero times on a reconstructed one, which
    capped historical scores at 3.0 against a 5.0 threshold and produced a
    backtest that took no trades at all.
    """
    # OI rising and price rising together = long buildup.
    cycles = _buildup_cycles([1000, 1100, 1210, 1330], [100.0, 105.0, 110.0, 116.0])
    hs.apply_oi_buildup(cycles)

    kinds = [c[0].chain[0].buildup_type for c in cycles]
    assert kinds[0] is None, "first cycle has no history to compare against"
    assert "long_buildup" in kinds


def test_oi_buildup_distinguishes_short_buildup_from_long():
    """OI up with price DOWN is writers piling in, the opposite read."""
    cycles = _buildup_cycles([1000, 1100, 1210, 1330], [100.0, 95.0, 90.0, 85.0])
    hs.apply_oi_buildup(cycles)
    assert "short_buildup" in [c[0].chain[0].buildup_type for c in cycles]


def test_oi_buildup_leaves_early_cycles_unclassified_not_zero():
    """
    Too little history must read as "unknown", never as "no change" --
    the distinction _intraday_change exists to preserve.
    """
    cycles = _buildup_cycles([1000], [100.0])
    hs.apply_oi_buildup(cycles)
    q = cycles[0][0].chain[0]
    assert q.buildup_type is None
    assert q.oi_change_pct_intraday is None
    assert q.price_change_pct_intraday is None


def test_oi_buildup_ignores_moves_below_the_threshold():
    """A flat book must not be classified as a buildup in either direction."""
    cycles = _buildup_cycles([1000, 1001, 1002, 1003], [100.0, 100.1, 100.2, 100.3])
    hs.apply_oi_buildup(cycles)
    assert all(c[0].chain[0].buildup_type is None for c in cycles)


def test_coverage_of_empty_chain_is_zero_not_a_crash():
    from models import MarketSnapshot
    snap = MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=0.0,
                          chain=[], timestamp=datetime.now(), source="dhan_historical")
    assert hs.coverage(snap)["strikes"] == 0
