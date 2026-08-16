"""
instrument_master.py's underlying-parameterization (added 2026-08-16, for
Bank Nifty order-flow support) -- was hardcoded to NIFTY with a single
cache file; every function now takes an `underlying` param defaulting to
NIFTY so existing callers are unaffected.

Run: python -m pytest tests/test_instrument_master.py -q
"""
import json

import instrument_master as im


CSV_HEADER = ("UNDERLYING_SYMBOL,INSTRUMENT,OPTION_TYPE,STRIKE_PRICE,"
              "SECURITY_ID,SM_EXPIRY_DATE,LOT_SIZE")


def _csv(rows):
    return "\n".join([CSV_HEADER] + rows)


NIFTY_ROW_CE = "NIFTY,OPTIDX,CE,24000,111111,2026-08-28,75"
NIFTY_ROW_PE = "NIFTY,OPTIDX,PE,24000,222222,2026-08-28,75"
BANKNIFTY_ROW_CE = "BANKNIFTY,OPTIDX,CE,51000,333333,2026-08-27,30"
NIFTY_FUT_ROW = "NIFTY,FUTIDX,,24000,444444,2026-08-28,75"  # wrong INSTRUMENT, must be dropped
GARBAGE_STRIKE_ROW = "NIFTY,OPTIDX,CE,notanumber,555555,2026-08-28,75"


def _mock_get(text, monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass
        @property
        def text(self):
            return text
    monkeypatch.setattr(im.requests, "get", lambda *a, **kw: Resp())


def test_parse_options_filters_by_underlying_exact_match():
    """BANKNIFTY contains the substring NIFTY -- must not accidentally match."""
    text = _csv([NIFTY_ROW_CE, BANKNIFTY_ROW_CE])
    nifty_rows = im._parse_options(text, "NIFTY")
    bn_rows = im._parse_options(text, "BANKNIFTY")
    assert len(nifty_rows) == 1 and nifty_rows[0]["security_id"] == 111111
    assert len(bn_rows) == 1 and bn_rows[0]["security_id"] == 333333


def test_parse_options_drops_wrong_instrument_kind():
    text = _csv([NIFTY_ROW_CE, NIFTY_FUT_ROW])
    rows = im._parse_options(text, "NIFTY")
    assert len(rows) == 1
    assert rows[0]["security_id"] == 111111


def test_parse_options_skips_unparseable_strike():
    text = _csv([NIFTY_ROW_CE, GARBAGE_STRIKE_ROW])
    rows = im._parse_options(text, "NIFTY")
    assert len(rows) == 1


def test_cache_path_differs_per_underlying(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    assert im._cache_path_for("NIFTY") != im._cache_path_for("BANKNIFTY")
    assert im._cache_path_for("NIFTY").name == "instrument_master_nifty.json"
    assert im._cache_path_for("BANKNIFTY").name == "instrument_master_banknifty.json"


def test_refresh_caches_separately_per_underlying(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    _mock_get(_csv([NIFTY_ROW_CE, BANKNIFTY_ROW_CE]), monkeypatch)

    nifty_contracts = im.refresh(underlying="NIFTY")
    bn_contracts = im.refresh(underlying="BANKNIFTY")

    assert len(nifty_contracts) == 1 and nifty_contracts[0]["security_id"] == 111111
    assert len(bn_contracts) == 1 and bn_contracts[0]["security_id"] == 333333
    assert im._cache_path_for("NIFTY").exists()
    assert im._cache_path_for("BANKNIFTY").exists()


def test_refresh_uses_fresh_cache_without_a_network_call(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    cache_path = im._cache_path_for("NIFTY")
    cache_path.write_text(json.dumps({
        "fetched_at": 0, "count": 1,
        "contracts": [{"security_id": 999, "strike": 24000.0, "expiry": "2026-08-28", "option_type": "CE", "lot_size": 75}],
    }))

    def _boom(*a, **kw):
        raise AssertionError("should not hit the network with a fresh cache")
    monkeypatch.setattr(im.requests, "get", _boom)

    contracts = im.refresh(underlying="NIFTY")
    assert contracts[0]["security_id"] == 999


def test_refresh_falls_back_to_stale_cache_on_download_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    cache_path = im._cache_path_for("NIFTY")
    cache_path.write_text(json.dumps({
        "fetched_at": 0, "count": 1,
        "contracts": [{"security_id": 777, "strike": 24000.0, "expiry": "2026-08-28", "option_type": "CE", "lot_size": 75}],
    }))
    old_time = cache_path.stat().st_mtime - (im.CACHE_MAX_AGE_HOURS + 1) * 3600
    import os
    os.utime(cache_path, (old_time, old_time))

    def _boom(*a, **kw):
        raise ConnectionError("CDN down")
    monkeypatch.setattr(im.requests, "get", _boom)

    contracts = im.refresh(underlying="NIFTY")
    assert contracts[0]["security_id"] == 777


def test_refresh_raises_with_no_cache_and_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    def _boom(*a, **kw):
        raise ConnectionError("CDN down")
    monkeypatch.setattr(im.requests, "get", _boom)
    try:
        im.refresh(underlying="NIFTY")
        assert False, "expected an exception"
    except ConnectionError:
        pass


def test_refresh_raises_loudly_when_underlying_matches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    _mock_get(_csv([NIFTY_ROW_CE]), monkeypatch)
    try:
        im.refresh(underlying="SENSEX")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "SENSEX" in str(e)


def test_build_index_and_security_id_for_scoped_by_underlying(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    _mock_get(_csv([NIFTY_ROW_CE, BANKNIFTY_ROW_CE]), monkeypatch)

    nifty_index = im.build_index(underlying="NIFTY")
    bn_index = im.build_index(underlying="BANKNIFTY")

    assert im.security_id_for(24000.0, "2026-08-28", "CE", index=nifty_index) == 111111
    assert im.security_id_for(51000.0, "2026-08-27", "CE", index=bn_index) == 333333
    # Bank Nifty's strike isn't in NIFTY's own index.
    assert im.security_id_for(51000.0, "2026-08-27", "CE", index=nifty_index) is None


def test_security_id_for_unknown_contract_returns_none():
    index = {(24000.0, "2026-08-28", "CE"): 111111}
    assert im.security_id_for(99999.0, "2026-08-28", "CE", index=index) is None


def test_describe_reports_the_requested_underlying(tmp_path, monkeypatch):
    monkeypatch.setattr(im, "CACHE_DIR", tmp_path)
    _mock_get(_csv([BANKNIFTY_ROW_CE]), monkeypatch)
    out = im.describe(underlying="BANKNIFTY")
    assert "BANKNIFTY" in out
    assert "1 BANKNIFTY option contracts" in out
