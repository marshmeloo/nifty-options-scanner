"""
Tests for commodity_source.py's instrument-resolution and candle-parsing
logic. Network calls (instrument master download, /charts/historical)
are mocked -- these pin the FILTERING and PARSING behaviour, not live
data quality (that's a one-off manual check, documented in BACKLOG.md).

Run: python -m pytest tests/test_commodity_source.py -q
"""
import io
from datetime import datetime
from types import SimpleNamespace

import pytest

import commodity_source as cs


_SAMPLE_CSV = (
    "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,SM_EXPIRY_DATE,LOT_SIZE\n"
    "MCX,M,483079,FUTCOM,GOLD,2026-10-05 00:00:00,1.0\n"
    "MCX,M,999999,FUTCOM,GOLD,2027-01-05 00:00:00,1.0\n"
    "MCX,M,471725,FUTCOM,SILVER,2026-09-04 00:00:00,1.0\n"
    "MCX,M,509665,OPTFUT,SILVERM,2026-08-24 00:00:00,1.0\n"
    "NSE,D,13,FUTIDX,NIFTY,2026-08-25 00:00:00,65.0\n"
)


def test_current_security_id_picks_nearest_expiry(monkeypatch):
    fake_resp = SimpleNamespace(text=_SAMPLE_CSV, raise_for_status=lambda: None)
    monkeypatch.setattr(cs.requests, "get", lambda url, timeout: fake_resp)

    result = cs._current_security_id("GOLD")
    assert result == {"security_id": 483079, "expiry": "2026-10-05", "lot_size": 1.0}


def test_current_security_id_ignores_options_on_future(monkeypatch):
    """SILVERM's row is OPTFUT, not FUTCOM -- must not be picked for SILVER
    (also a different UNDERLYING_SYMBOL, SILVERM vs SILVER, but the
    INSTRUMENT filter is the one that actually matters here)."""
    fake_resp = SimpleNamespace(text=_SAMPLE_CSV, raise_for_status=lambda: None)
    monkeypatch.setattr(cs.requests, "get", lambda url, timeout: fake_resp)

    result = cs._current_security_id("SILVER")
    assert result["security_id"] == 471725


def test_current_security_id_raises_on_unknown_symbol(monkeypatch):
    fake_resp = SimpleNamespace(text=_SAMPLE_CSV, raise_for_status=lambda: None)
    monkeypatch.setattr(cs.requests, "get", lambda url, timeout: fake_resp)

    with pytest.raises(ValueError, match="No MCX FUTCOM rows"):
        cs._current_security_id("PLATINUM")


def test_backfill_daily_parses_candles_sorted_oldest_first(monkeypatch):
    monkeypatch.setattr(cs, "_current_security_id",
                        lambda sym: {"security_id": 483079, "expiry": "2026-10-05", "lot_size": 1.0})
    monkeypatch.setattr(cs.dhan_rate_limiter, "wait_for_slot", lambda: None)

    ts_newer = int(datetime(2026, 8, 5).timestamp())
    ts_older = int(datetime(2026, 8, 4).timestamp())
    fake_json = {
        "timestamp": [ts_newer, ts_older],
        "open": [145000.0, 144000.0],
        "high": [146000.0, 145000.0],
        "low": [144500.0, 143500.0],
        "close": [145234.0, 144800.0],
        "volume": [100, 90],
    }
    fake_resp = SimpleNamespace(json=lambda: fake_json, raise_for_status=lambda: None)
    monkeypatch.setattr(cs.requests, "post", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(cs, "_headers", lambda: {})

    candles = cs.backfill_daily("GOLD", days_back=10)
    assert len(candles) == 2
    assert candles[0].timestamp < candles[1].timestamp  # sorted oldest-first
    assert candles[1].close == 145234.0


def test_save_and_load_daily_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "DATA_DIR", tmp_path)
    from models import Candle
    candles = [
        Candle(timestamp=datetime(2026, 8, 4), open=1.0, high=2.0, low=0.5, close=1.5, volume=10),
        Candle(timestamp=datetime(2026, 8, 5), open=1.5, high=2.5, low=1.0, close=2.0, volume=20),
    ]
    path = cs.save_daily("GOLD", candles)
    assert path.exists()

    loaded = cs.load_daily("GOLD")
    assert len(loaded) == 2
    assert loaded[0].close == 1.5
    assert loaded[1].close == 2.0
