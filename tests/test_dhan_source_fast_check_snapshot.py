"""
Tests for dhan_source.get_fast_check_snapshot() -- the orderflow-first,
/marketfeed/ltp-fallback lightweight snapshot builder added 2026-08-18 to
replace a full option-chain re-fetch in the fast stop/target check.

Mocks at the orderflow.py / instrument_master.py / get_ltp_batch()
function boundary throughout, matching this project's existing convention
(see test_dhan_source_second_underlying.py) rather than reconstructing
raw WebSocket packets or HTTP payloads -- this module's own job is just
to combine those three already-tested sources correctly.

Run: python -m pytest tests/test_dhan_source_fast_check_snapshot.py -q
"""

import requests
import pytest

import dhan_source as ds


def _contract(ltp, bid=None, ask=None, bid_qty=None, ask_qty=None):
    depth = [{"bid_price": bid, "ask_price": ask, "bid_qty": bid_qty, "ask_qty": ask_qty}] if bid or ask else []
    return {"ltp": ltp, "depth": depth}


def _top(bid=None, ask=None, bid_qty=None, ask_qty=None):
    return {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty}


def test_empty_positions_returns_empty_chain_without_touching_ltp(monkeypatch):
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds, "get_ltp_batch", lambda *a, **kw: pytest.fail("should not be called"))

    snapshot = ds.get_fast_check_snapshot([], expiry="2026-08-20")

    assert snapshot.chain == []
    assert snapshot.source == "fast_check"


def test_orderflow_covers_everything_skips_ltp_fallback_entirely(monkeypatch):
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {"fake": "state"})
    monkeypatch.setattr(ds.orderflow, "book_for", lambda strike, ot, state=None: _contract(101.5, 100.0, 103.0))
    monkeypatch.setattr(ds.orderflow, "top_of_book", lambda strike, ot, state=None: _top(100.0, 103.0, 50, 60))
    monkeypatch.setattr(ds, "get_ltp_batch", lambda *a, **kw: pytest.fail("should not be called"))
    monkeypatch.setattr(ds.instrument_master, "build_index", lambda **kw: pytest.fail("should not be called"))

    snapshot = ds.get_fast_check_snapshot([(24500.0, "CE"), (24500.0, "PE")], expiry="2026-08-20")

    assert len(snapshot.chain) == 2
    q = next(q for q in snapshot.chain if q.option_type == "CE")
    assert q.ltp == 101.5
    assert q.bid == 100.0 and q.ask == 103.0
    assert q.has_book is True
    assert q.sell_price == 100.0  # confirms exit_price_for() would use the real bid


def test_orderflow_miss_falls_back_to_ltp_batch(monkeypatch):
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds.orderflow, "book_for", lambda strike, ot, state=None: None)
    monkeypatch.setattr(ds.instrument_master, "build_index", lambda **kw: {"fake": "index"})
    monkeypatch.setattr(ds.instrument_master, "security_id_for",
                        lambda strike, expiry, ot, index: 555)
    monkeypatch.setattr(ds, "get_ltp_batch", lambda ids: {555: 88.25})

    snapshot = ds.get_fast_check_snapshot([(24500.0, "CE")], expiry="2026-08-20")

    assert len(snapshot.chain) == 1
    q = snapshot.chain[0]
    assert q.strike == 24500.0 and q.option_type == "CE"
    assert q.ltp == 88.25
    assert q.has_book is False       # no bid/ask from the LTP-only fallback
    assert q.sell_price == 88.25     # exit_price_for() falls back to ltp here, matching has_book=False elsewhere


def test_mixed_orderflow_hit_and_ltp_fallback(monkeypatch):
    def fake_book_for(strike, ot, state=None):
        return _contract(50.0, 49.0, 51.0) if strike == 100.0 else None

    def fake_top(strike, ot, state=None):
        return _top(49.0, 51.0, 10, 10) if strike == 100.0 else _top()

    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds.orderflow, "book_for", fake_book_for)
    monkeypatch.setattr(ds.orderflow, "top_of_book", fake_top)
    monkeypatch.setattr(ds.instrument_master, "build_index", lambda **kw: {})
    monkeypatch.setattr(ds.instrument_master, "security_id_for",
                        lambda strike, expiry, ot, index: 777)
    monkeypatch.setattr(ds, "get_ltp_batch", lambda ids: {777: 12.0})

    snapshot = ds.get_fast_check_snapshot([(100.0, "CE"), (200.0, "PE")], expiry="2026-08-20")

    by_strike = {q.strike: q for q in snapshot.chain}
    assert by_strike[100.0].ltp == 50.0 and by_strike[100.0].has_book
    assert by_strike[200.0].ltp == 12.0 and not by_strike[200.0].has_book


def test_strike_missing_from_instrument_master_is_skipped_not_crashed(monkeypatch):
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds.orderflow, "book_for", lambda strike, ot, state=None: None)
    monkeypatch.setattr(ds.instrument_master, "build_index", lambda **kw: {})
    monkeypatch.setattr(ds.instrument_master, "security_id_for", lambda strike, expiry, ot, index: None)
    monkeypatch.setattr(ds, "get_ltp_batch", lambda *a, **kw: pytest.fail("should not be called"))

    snapshot = ds.get_fast_check_snapshot([(24500.0, "CE")], expiry="2026-08-20")

    assert snapshot.chain == []


def test_ltp_batch_failure_degrades_gracefully_not_raised(monkeypatch):
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds.orderflow, "book_for", lambda strike, ot, state=None: None)
    monkeypatch.setattr(ds.instrument_master, "build_index", lambda **kw: {})
    monkeypatch.setattr(ds.instrument_master, "security_id_for", lambda strike, expiry, ot, index: 1)

    def fake_get_ltp_batch(ids):
        raise requests.exceptions.ConnectionError("boom")
    monkeypatch.setattr(ds, "get_ltp_batch", fake_get_ltp_batch)

    snapshot = ds.get_fast_check_snapshot([(24500.0, "CE")], expiry="2026-08-20")

    assert snapshot.chain == []  # degraded, not raised -- caller keeps this trade open untouched


def test_ltp_batch_failure_does_not_lose_the_orderflow_covered_strikes(monkeypatch):
    """The whole point of catching the LTP failure INSIDE the builder
    rather than letting the caller's blanket try/except lose everything:
    strikes orderflow already answered must survive an LTP outage."""
    def fake_book_for(strike, ot, state=None):
        return _contract(10.0, 9.5, 10.5) if strike == 100.0 else None

    def fake_top(strike, ot, state=None):
        return _top(9.5, 10.5, 5, 5) if strike == 100.0 else _top()

    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds.orderflow, "book_for", fake_book_for)
    monkeypatch.setattr(ds.orderflow, "top_of_book", fake_top)
    monkeypatch.setattr(ds.instrument_master, "build_index", lambda **kw: {})
    monkeypatch.setattr(ds.instrument_master, "security_id_for", lambda strike, expiry, ot, index: 1)

    def fake_get_ltp_batch(ids):
        raise requests.exceptions.HTTPError("429")
    monkeypatch.setattr(ds, "get_ltp_batch", fake_get_ltp_batch)

    snapshot = ds.get_fast_check_snapshot([(100.0, "CE"), (200.0, "PE")], expiry="2026-08-20")

    assert len(snapshot.chain) == 1
    assert snapshot.chain[0].strike == 100.0


def test_symbol_passed_through_to_instrument_master_underlying(monkeypatch):
    seen = {}
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds.orderflow, "book_for", lambda strike, ot, state=None: None)

    def fake_build_index(underlying):
        seen["underlying"] = underlying
        return {}
    monkeypatch.setattr(ds.instrument_master, "build_index", fake_build_index)
    monkeypatch.setattr(ds.instrument_master, "security_id_for", lambda strike, expiry, ot, index: None)

    ds.get_fast_check_snapshot([(52000.0, "CE")], expiry="2026-08-20", symbol="BANKNIFTY")

    assert seen["underlying"] == "BANKNIFTY"


def test_snapshot_quotes_carry_the_requested_expiry_and_symbol(monkeypatch):
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: {})
    monkeypatch.setattr(ds.orderflow, "book_for", lambda strike, ot, state=None: _contract(1.0))
    monkeypatch.setattr(ds.orderflow, "top_of_book", lambda strike, ot, state=None: _top())

    snapshot = ds.get_fast_check_snapshot([(100.0, "CE")], expiry="2026-08-27", symbol="BANKNIFTY")

    q = snapshot.chain[0]
    assert q.expiry == "2026-08-27"
    assert q.symbol == "BANKNIFTY"


def test_only_one_orderflow_state_load_for_the_whole_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(ds.orderflow, "load_state", lambda: calls.append(1) or {})
    monkeypatch.setattr(ds.orderflow, "book_for", lambda strike, ot, state=None: _contract(1.0))
    monkeypatch.setattr(ds.orderflow, "top_of_book", lambda strike, ot, state=None: _top())

    ds.get_fast_check_snapshot([(100.0, "CE"), (200.0, "PE"), (300.0, "CE")], expiry="2026-08-20")

    assert len(calls) == 1
