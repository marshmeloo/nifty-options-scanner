"""
Pins dhan_source.py's Bank Nifty parameterization: every state file it
reads/writes (IV history, OI history, price baseline, VWAP proxy) must
be symbol-suffixed, so a Bank Nifty snapshot call can never read or
write Nifty's own live state -- Nifty's momentum scanner is actively
trading off that state in production.

Run: python -m pytest tests/test_dhan_source_second_underlying.py -q
"""
from pathlib import Path

import pytest

import dhan_source as ds


def test_symbol_path_unchanged_for_nifty():
    base = ds.STATE_DIR / "iv_history.json"
    assert ds._symbol_path(base, "NIFTY") == base


def test_symbol_path_suffixed_for_a_second_underlying():
    base = ds.STATE_DIR / "iv_history.json"
    result = ds._symbol_path(base, "BANKNIFTY")
    assert result == ds.STATE_DIR / "iv_history_banknifty.json"
    assert result != base


def test_iv_history_round_trips_per_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "IV_HISTORY_PATH", tmp_path / "iv_history.json")
    ds._save_iv_history([1.0, 2.0], symbol="NIFTY")
    ds._save_iv_history([9.0], symbol="BANKNIFTY")
    assert ds._load_iv_history(symbol="NIFTY") == [1.0, 2.0]
    assert ds._load_iv_history(symbol="BANKNIFTY") == [9.0]


def test_oi_history_round_trips_per_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "OI_HISTORY_PATH", tmp_path / "oi_history.json")
    ds._save_oi_history({"date": "2026-08-06", "samples": {"a": 1}}, symbol="NIFTY")
    ds._save_oi_history({"date": "2026-08-06", "samples": {"b": 2}}, symbol="BANKNIFTY")
    assert ds._load_oi_history(symbol="NIFTY")["samples"] == {"a": 1}
    assert ds._load_oi_history(symbol="BANKNIFTY")["samples"] == {"b": 2}


def test_vwap_proxy_state_round_trips_per_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "LIVE_STATE_PATH", tmp_path / "live_state.json")
    n1 = ds._update_vwap_proxy(100.0, symbol="NIFTY")
    b1 = ds._update_vwap_proxy(500.0, symbol="BANKNIFTY")
    n2 = ds._update_vwap_proxy(200.0, symbol="NIFTY")
    assert n1 == 100.0
    assert b1 == 500.0
    assert n2 == 150.0  # averages with NIFTY's own prior sample, not Bank Nifty's


_FAKE_OC = {
    "100": {"ce": {"implied_volatility": 12.0, "last_price": 5.0, "oi": 100, "previous_oi": 90,
                   "volume": 10, "greeks": {}},
            "pe": {"implied_volatility": 12.0, "last_price": 5.0, "oi": 100, "previous_oi": 90,
                   "volume": 10, "greeks": {}}},
}


def test_get_nifty_snapshot_defaults_reproduce_old_nifty_behaviour(monkeypatch):
    seen = {}

    def fake_fetch(expiry, underlying_scrip, underlying_seg):
        seen["fetch"] = (underlying_scrip, underlying_seg)
        return {"last_price": 100.0, "oc": _FAKE_OC}

    monkeypatch.setattr(ds, "_fetch_raw_chain", fake_fetch)
    monkeypatch.setattr(ds, "get_nearest_expiry", lambda *a, **kw: "2026-08-06")
    monkeypatch.setattr(ds, "_update_vwap_proxy", lambda spot, symbol="NIFTY": (seen.setdefault("vwap_symbol", symbol), spot)[1])
    monkeypatch.setattr(ds, "_load_iv_history", lambda symbol="NIFTY": [])
    monkeypatch.setattr(ds, "_save_iv_history", lambda h, symbol="NIFTY": None)
    monkeypatch.setattr(ds, "_load_price_baseline", lambda symbol="NIFTY": {"date": None, "prices": {}})
    monkeypatch.setattr(ds, "_save_price_baseline", lambda s, symbol="NIFTY": None)
    monkeypatch.setattr(ds, "_load_oi_history", lambda symbol="NIFTY": {"date": None, "samples": {}})
    monkeypatch.setattr(ds, "_save_oi_history", lambda h, symbol="NIFTY": None)

    snap = ds.get_nifty_snapshot()
    assert seen["fetch"] == (ds.NIFTY_UNDERLYING_SCRIP, ds.NIFTY_UNDERLYING_SEG)
    assert seen["vwap_symbol"] == "NIFTY"
    assert snap.symbol == "NIFTY"


def test_get_nifty_snapshot_passes_banknifty_params_through(monkeypatch):
    seen = {}

    def fake_fetch(expiry, underlying_scrip, underlying_seg):
        seen["fetch"] = (underlying_scrip, underlying_seg)
        return {"last_price": 500.0, "oc": _FAKE_OC}

    monkeypatch.setattr(ds, "_fetch_raw_chain", fake_fetch)
    monkeypatch.setattr(ds, "get_nearest_expiry", lambda *a, **kw: "2026-08-06")
    monkeypatch.setattr(ds, "_update_vwap_proxy", lambda spot, symbol="NIFTY": (seen.setdefault("vwap_symbol", symbol), spot)[1])
    monkeypatch.setattr(ds, "_load_iv_history", lambda symbol="NIFTY": [])
    monkeypatch.setattr(ds, "_save_iv_history", lambda h, symbol="NIFTY": None)
    monkeypatch.setattr(ds, "_load_price_baseline", lambda symbol="NIFTY": {"date": None, "prices": {}})
    monkeypatch.setattr(ds, "_save_price_baseline", lambda s, symbol="NIFTY": None)
    monkeypatch.setattr(ds, "_load_oi_history", lambda symbol="NIFTY": {"date": None, "samples": {}})
    monkeypatch.setattr(ds, "_save_oi_history", lambda h, symbol="NIFTY": None)

    snap = ds.get_nifty_snapshot(symbol="BANKNIFTY", underlying_scrip=ds.BANKNIFTY_UNDERLYING_SCRIP,
                                 underlying_seg=ds.BANKNIFTY_UNDERLYING_SEG)
    assert seen["fetch"] == (ds.BANKNIFTY_UNDERLYING_SCRIP, ds.BANKNIFTY_UNDERLYING_SEG)
    assert seen["vwap_symbol"] == "BANKNIFTY"
    assert snap.symbol == "BANKNIFTY"
