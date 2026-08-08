"""
Tests for orderflow.py's top_of_book() and dhan_source.py's preference
of it over the REST chain's own best-effort bid/ask field guessing.

Run: python -m pytest tests/test_orderflow_top_of_book.py -q
"""
import time

import pytest

import orderflow as of


def _state(contracts, written_at=None):
    return {"written_at": written_at or time.time(), "contracts": contracts}


def _contract(strike, option_type, depth, updated_at=None):
    return {"strike": strike, "option_type": option_type,
            "updated_at": updated_at or time.time(), "depth": depth}


def test_top_of_book_returns_all_four_fields():
    state = _state([_contract(24000, "CE", [
        {"bid_price": 100.0, "ask_price": 101.5, "bid_qty": 300, "ask_qty": 450},
    ])])
    result = of.top_of_book(24000, "CE", state=state)
    assert result == {"bid": 100.0, "ask": 101.5, "bid_qty": 300, "ask_qty": 450}


def test_top_of_book_all_none_when_feed_stale():
    stale_state = _state([_contract(24000, "CE", [
        {"bid_price": 100.0, "ask_price": 101.5, "bid_qty": 300, "ask_qty": 450},
    ])], written_at=time.time() - 999)
    result = of.top_of_book(24000, "CE", state=stale_state)
    assert result == {"bid": None, "ask": None, "bid_qty": None, "ask_qty": None}


def test_top_of_book_all_none_when_contract_not_subscribed():
    state = _state([_contract(24000, "CE", [{"bid_price": 100.0, "ask_price": 101.5}])])
    result = of.top_of_book(25000, "PE", state=state)
    assert result == {"bid": None, "ask": None, "bid_qty": None, "ask_qty": None}


def test_top_of_book_all_none_on_empty_depth():
    state = _state([_contract(24000, "CE", [])])
    result = of.top_of_book(24000, "CE", state=state)
    assert result == {"bid": None, "ask": None, "bid_qty": None, "ask_qty": None}


# --- dhan_source.py's preference wiring -------------------------------

import dhan_source as ds

_FAKE_OC = {
    "24000": {
        "ce": {"implied_volatility": 12.0, "last_price": 90.0, "oi": 1000, "previous_oi": 900,
               "volume": 500, "greeks": {},
               "top_bid_price": 88.0, "top_ask_price": 92.0, "top_bid_quantity": 75, "top_ask_quantity": 60},
        "pe": {"implied_volatility": 13.0, "last_price": 60.0, "oi": 800, "previous_oi": 750,
               "volume": 400, "greeks": {}},
    },
}


def _snapshot_mocks(monkeypatch, orderflow_state):
    monkeypatch.setattr(ds, "_fetch_raw_chain", lambda expiry, *a, **kw: {"last_price": 24000.0, "oc": _FAKE_OC})
    monkeypatch.setattr(ds, "get_nearest_expiry", lambda *a, **kw: "2026-08-08")
    monkeypatch.setattr(ds, "_update_vwap_proxy", lambda spot, *a, **kw: spot)
    monkeypatch.setattr(ds, "_load_iv_history", lambda *a, **kw: [])
    monkeypatch.setattr(ds, "_save_iv_history", lambda *a, **kw: None)
    monkeypatch.setattr(ds, "_load_price_baseline", lambda *a, **kw: {"date": None, "prices": {}})
    monkeypatch.setattr(ds, "_save_price_baseline", lambda *a, **kw: None)
    monkeypatch.setattr(ds, "_load_oi_history", lambda *a, **kw: {"date": None, "samples": {}})
    monkeypatch.setattr(ds, "_save_oi_history", lambda *a, **kw: None)
    monkeypatch.setattr(ds.orderflow, "load_state", lambda *a, **kw: orderflow_state)


def test_snapshot_prefers_orderflow_bid_ask_when_fresh(monkeypatch):
    of_state = _state([_contract(24000, "CE", [
        {"bid_price": 89.5, "ask_price": 90.5, "bid_qty": 200, "ask_qty": 180},
    ])])
    _snapshot_mocks(monkeypatch, of_state)

    snap = ds.get_nifty_snapshot()
    ce = next(q for q in snap.chain if q.option_type == "CE")
    # orderflow's book (89.5/90.5), NOT the REST chain's own (88.0/92.0)
    assert ce.bid == 89.5
    assert ce.ask == 90.5
    assert ce.bid_qty == 200
    assert ce.ask_qty == 180


def test_snapshot_falls_back_to_rest_when_orderflow_has_nothing(monkeypatch):
    _snapshot_mocks(monkeypatch, {})  # no orderflow state at all

    snap = ds.get_nifty_snapshot()
    ce = next(q for q in snap.chain if q.option_type == "CE")
    pe = next(q for q in snap.chain if q.option_type == "PE")
    # REST chain's own guessed fields, unchanged from before this change
    assert ce.bid == 88.0
    assert ce.ask == 92.0
    assert ce.bid_qty == 75
    assert ce.ask_qty == 60
    # PE has no REST bid/ask fields either -> stays None, exactly as before
    assert pe.bid is None
    assert pe.ask is None


def test_snapshot_falls_back_per_field_when_orderflow_only_has_some(monkeypatch):
    # orderflow subscribed to this contract but its book only shows a bid
    # right now (e.g. thin liquidity) -- ask must still fall back to REST.
    of_state = _state([_contract(24000, "CE", [
        {"bid_price": 89.5, "ask_price": 0.0, "bid_qty": 200, "ask_qty": 0},
    ])])
    _snapshot_mocks(monkeypatch, of_state)

    snap = ds.get_nifty_snapshot()
    ce = next(q for q in snap.chain if q.option_type == "CE")
    assert ce.bid == 89.5          # orderflow's
    assert ce.ask == 92.0          # REST fallback (orderflow's ask_price was 0.0 -> None)
    assert ce.bid_qty == 200       # orderflow's
    assert ce.ask_qty == 60        # REST fallback
