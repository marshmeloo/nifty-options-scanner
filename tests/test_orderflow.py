"""
Tests for orderflow.py -- the read side of the live order-flow feed.

The behaviour these pin hardest is STALENESS. orderflow_feed.py is a
separate process; if it dies, its state file does not disappear, it just
stops being updated. A reader that checks only for the file's existence
would trade on a book from hours ago while everything looked healthy.

So every accessor is age-gated and returns None on unavailability --
never a plausible default. A missing book is missing information, not a
balanced book: the same distinction behind the 2026-07-30 condor
"MTM unavailable" incident, where treating a missing quote as a valid
mark would have invented exits that never happened.

Run: python -m pytest tests/ -q
"""

import json
import time

import pytest

import orderflow


def _depth(bid_qty=100, ask_qty=100, bid_price=99.0, ask_price=101.0):
    """Five levels; only the top one is varied since that is what's asserted."""
    return [
        {"bid_qty": bid_qty, "ask_qty": ask_qty, "bid_orders": 5, "ask_orders": 5,
         "bid_price": bid_price, "ask_price": ask_price}
    ] + [
        {"bid_qty": 10, "ask_qty": 10, "bid_orders": 1, "ask_orders": 1,
         "bid_price": bid_price - i, "ask_price": ask_price + i}
        for i in range(1, 5)
    ]


def _state(now=None, written_offset=0.0, contract_offset=0.0, **contract_over):
    now = now or time.time()
    contract = {
        "strike": 24400.0, "option_type": "CE", "security_id": 65854,
        "ltp": 100.0, "oi": 1_000_000, "volume": 500_000,
        "total_buy_qty": 60_000, "total_sell_qty": 40_000,
        "depth": _depth(),
        "updated_at": now - contract_offset,
    }
    contract.update(contract_over)
    return {
        "written_at": now - written_offset,
        "subscribed": 1, "receiving": 1, "packets_seen": 42,
        "contracts": [contract],
    }


def test_fresh_state_is_readable():
    s = _state()
    assert orderflow.is_fresh(s)
    assert orderflow.book_for(24400.0, "CE", s) is not None


def test_stale_feed_returns_none_not_last_known_values():
    """
    The core guarantee: a dead feed must read as unavailable, not as
    whatever it last happened to see.
    """
    s = _state(written_offset=orderflow.MAX_AGE_SECONDS + 60)
    assert not orderflow.is_fresh(s)
    assert orderflow.book_for(24400.0, "CE", s) is None
    assert orderflow.best_bid_ask(24400.0, "CE", s) == (None, None)
    assert orderflow.spread_pct(24400.0, "CE", s) is None
    assert orderflow.book_imbalance(24400.0, "CE", s) is None


def test_a_single_stale_contract_is_unavailable_while_the_feed_is_healthy():
    """
    An illiquid strike can go quiet while the feed as a whole is fine, so
    contract age is checked separately from feed age.
    """
    s = _state(contract_offset=orderflow.MAX_CONTRACT_AGE_SECONDS + 10)
    assert orderflow.is_fresh(s), "feed itself should still read healthy"
    assert orderflow.book_for(24400.0, "CE", s) is None


def test_missing_state_file_reads_as_unavailable(tmp_path):
    missing = tmp_path / "nope.json"
    assert orderflow.load_state(missing) == {}
    assert orderflow.feed_age_seconds({}) == float("inf")
    assert not orderflow.is_fresh({})


def test_corrupt_state_file_reads_as_unavailable(tmp_path):
    path = tmp_path / "orderflow.json"
    path.write_text("{not json")
    assert orderflow.load_state(path) == {}


def test_unsubscribed_contract_returns_none():
    s = _state()
    assert orderflow.book_for(99999.0, "PE", s) is None


def test_best_bid_ask_reads_the_top_of_book():
    s = _state(depth=_depth(bid_price=99.5, ask_price=100.5))
    assert orderflow.best_bid_ask(24400.0, "CE", s) == (99.5, 100.5)


def test_zero_prices_are_treated_as_unavailable_not_as_a_real_price():
    """
    An empty side of the book publishes 0.0. A 0.0 "bid" flowing into a
    fill calculation would price an exit at nothing.
    """
    s = _state(depth=_depth(bid_price=0.0, ask_price=100.5))
    assert orderflow.best_bid_ask(24400.0, "CE", s) == (None, 100.5)
    assert orderflow.spread_pct(24400.0, "CE", s) is None


def test_spread_pct_is_computed_against_the_mid():
    s = _state(depth=_depth(bid_price=99.0, ask_price=101.0))
    # mid 100, spread 2 -> 2%
    assert orderflow.spread_pct(24400.0, "CE", s) == pytest.approx(2.0)


def test_crossed_book_returns_none_rather_than_a_negative_spread():
    s = _state(depth=_depth(bid_price=101.0, ask_price=99.0))
    assert orderflow.spread_pct(24400.0, "CE", s) is None


def test_book_imbalance_sign_and_range():
    heavy_bid = _state(depth=_depth(bid_qty=900, ask_qty=100))
    heavy_ask = _state(depth=_depth(bid_qty=100, ask_qty=900))
    assert orderflow.book_imbalance(24400.0, "CE", state=heavy_bid) > 0
    assert orderflow.book_imbalance(24400.0, "CE", state=heavy_ask) < 0
    for s in (heavy_bid, heavy_ask):
        assert -1.0 <= orderflow.book_imbalance(24400.0, "CE", state=s) <= 1.0


def test_balanced_book_reads_zero_and_zero_is_distinguishable_from_unavailable():
    """
    0.0 is a real reading -- a perfectly balanced book -- and must not
    double as "no idea". Unavailable is None.
    """
    s = _state(depth=_depth(bid_qty=500, ask_qty=500))
    assert orderflow.book_imbalance(24400.0, "CE", state=s) == 0.0
    stale = _state(written_offset=orderflow.MAX_AGE_SECONDS + 60)
    assert orderflow.book_imbalance(24400.0, "CE", state=stale) is None


def test_empty_book_returns_none():
    s = _state(depth=[])
    assert orderflow.book_imbalance(24400.0, "CE", state=s) is None
    assert orderflow.best_bid_ask(24400.0, "CE", s) == (None, None)


def test_total_quantity_imbalance_uses_exchange_wide_totals():
    s = _state(total_buy_qty=75_000, total_sell_qty=25_000)
    # (75000 - 25000) / 100000 = +0.5
    assert orderflow.total_quantity_imbalance(24400.0, "CE", s) == pytest.approx(0.5)


def test_describe_distinguishes_live_stale_and_absent():
    assert "no feed state" in orderflow.describe({})
    assert "LIVE" in orderflow.describe(_state())
    assert "STALE" in orderflow.describe(_state(written_offset=orderflow.MAX_AGE_SECONDS + 60))
