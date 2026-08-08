"""
Read side of the order-flow feed: turns the book orderflow_feed.py
maintains into signals a strategy can use.

STALENESS IS THE WHOLE DESIGN
-----------------------------
orderflow_feed.py runs as a separate process. If it dies, crashes, or was
never started, its state file does not vanish -- it just stops being
updated, and every number in it silently becomes a description of some
earlier moment. A reader that checks only "does the file exist" would
happily trade on a book from three hours ago.

So every accessor here is age-gated, and the failure mode is always
"return None / unavailable", never "return a plausible default". A
missing book is missing information, not a balanced book -- the same
distinction behind the 2026-07-30 condor incident, where a missing quote
scored as a valid mark would have invented exits that never happened.

WHAT THE SIGNALS MEAN
---------------------
This is ORDER BOOK data (resting orders), not a trade tape with aggressor
classification -- see orderflow_feed.py's docstring. So:

  - `spread_pct` and real bid/ask: what a fill would actually cross.
    This is the most immediately valuable output, since every backtest
    number in this project is currently priced at LTP and therefore
    optimistic.
  - `book_imbalance`: resting buy size vs resting sell size, in
    [-1, +1]. Positive means more size bid than offered. It measures
    intent to trade at a price, NOT executed aggression, and resting
    orders can be pulled -- treat it as context, not a trigger.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("nifty_scanner")

STATE_PATH = Path(__file__).parent / "state" / "orderflow.json"

# A book older than this is not describing the current market. Set
# against the feed's own 2s write cadence with generous headroom for a
# brief reconnect, but far below the horizon on which option prices
# meaningfully move.
MAX_AGE_SECONDS = 30.0

# Per-contract staleness. A single illiquid strike can go quiet while the
# feed as a whole is perfectly healthy, so this is checked separately and
# is more forgiving than the feed-level bound.
MAX_CONTRACT_AGE_SECONDS = 120.0


def load_state(path: Path = None) -> dict:
    """Raw state file, or {} when absent/unreadable."""
    path = path or STATE_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def feed_age_seconds(state: dict = None, now: float = None) -> float:
    """Seconds since the feed last wrote, or inf if it never has."""
    state = state if state is not None else load_state()
    written = state.get("written_at")
    if not written:
        return float("inf")
    return (now or time.time()) - written


def is_fresh(state: dict = None, now: float = None, max_age: float = None) -> bool:
    state = state if state is not None else load_state()
    return feed_age_seconds(state, now) <= (max_age or MAX_AGE_SECONDS)


def _contract_key(contract: dict) -> tuple:
    return (float(contract["strike"]), contract["option_type"])


def book_for(strike: float, option_type: str, state: dict = None,
             now: float = None, max_age: float = None) -> dict:
    """
    The latest book for one contract, or None if the feed is stale, the
    contract is not subscribed, or its own last update is too old.

    Three separate "unavailable" causes, all collapsing to None on
    purpose: a caller should skip the contract in every one of them, and
    distinguishing them at the call site would invite treating some as
    "close enough".
    """
    state = state if state is not None else load_state()
    if not is_fresh(state, now, max_age):
        return None

    now = now or time.time()
    want = (float(strike), option_type)
    for contract in state.get("contracts", []):
        if _contract_key(contract) != want:
            continue
        updated = contract.get("updated_at")
        if not updated or (now - updated) > MAX_CONTRACT_AGE_SECONDS:
            return None
        return contract
    return None


def best_bid_ask(strike: float, option_type: str, state: dict = None,
                 now: float = None) -> tuple:
    """
    (bid, ask) from the top of book, or (None, None) if unavailable.

    Zero prices are treated as unavailable rather than real: an empty
    side of the book is published as 0.0, and a 0.0 "bid" flowing into a
    fill calculation would price an exit at nothing.
    """
    contract = book_for(strike, option_type, state, now)
    if not contract:
        return (None, None)
    depth = contract.get("depth") or []
    if not depth:
        return (None, None)
    top = depth[0]
    bid = top.get("bid_price") or None
    ask = top.get("ask_price") or None
    return (bid, ask)


def top_of_book(strike: float, option_type: str, state: dict = None,
                now: float = None) -> dict:
    """
    {bid, ask, bid_qty, ask_qty} in one call -- for a caller (like
    dhan_source.py's live snapshot builder) that wants all four instead
    of calling best_bid_ask() separately, avoiding a second state load.
    All four are None (never a guessed default) if the feed is stale,
    the contract isn't subscribed, or the book's top level is empty.
    """
    contract = book_for(strike, option_type, state, now)
    if not contract:
        return {"bid": None, "ask": None, "bid_qty": None, "ask_qty": None}
    depth = contract.get("depth") or []
    if not depth:
        return {"bid": None, "ask": None, "bid_qty": None, "ask_qty": None}
    top = depth[0]
    return {
        "bid": top.get("bid_price") or None,
        "ask": top.get("ask_price") or None,
        "bid_qty": top.get("bid_qty") or None,
        "ask_qty": top.get("ask_qty") or None,
    }


def spread_pct(strike: float, option_type: str, state: dict = None,
               now: float = None) -> float:
    """
    Bid-ask spread as a percentage of the mid, or None if unavailable.

    This is what config.MAX_SPREAD_PCT screens on, and until now it could
    only be approximated from the REST chain's own bid/ask when present.
    """
    bid, ask = best_bid_ask(strike, option_type, state, now)
    if not bid or not ask or ask <= bid:
        return None
    mid = (bid + ask) / 2
    return round((ask - bid) / mid * 100, 3) if mid else None


def book_imbalance(strike: float, option_type: str, levels: int = 5,
                   state: dict = None, now: float = None) -> float:
    """
    Resting buy size vs resting sell size across the top `levels`, as a
    ratio in [-1, +1]. Positive = more size bid than offered.

    None (not 0.0) when unavailable: zero is a real, meaningful reading
    -- a perfectly balanced book -- and must not double as "no idea".
    """
    contract = book_for(strike, option_type, state, now)
    if not contract:
        return None
    depth = (contract.get("depth") or [])[:levels]
    if not depth:
        return None
    bid_size = sum(lv.get("bid_qty") or 0 for lv in depth)
    ask_size = sum(lv.get("ask_qty") or 0 for lv in depth)
    total = bid_size + ask_size
    if not total:
        return None
    return round((bid_size - ask_size) / total, 4)


def total_quantity_imbalance(strike: float, option_type: str, state: dict = None,
                             now: float = None) -> float:
    """
    Exchange-wide total buy vs total sell quantity for this contract, in
    [-1, +1]. Broader than book_imbalance (which sees only the top 5
    levels) but correspondingly coarser.
    """
    contract = book_for(strike, option_type, state, now)
    if not contract:
        return None
    buy = contract.get("total_buy_qty") or 0
    sell = contract.get("total_sell_qty") or 0
    total = buy + sell
    if not total:
        return None
    return round((buy - sell) / total, 4)


def describe(state: dict = None, now: float = None) -> str:
    """One-line health summary, for logs and the dashboard."""
    state = state if state is not None else load_state()
    if not state:
        return "Order flow: no feed state on disk (orderflow_feed.py not started?)"

    age = feed_age_seconds(state, now)
    if age == float("inf"):
        return "Order flow: state file present but never written"

    status = "LIVE" if age <= MAX_AGE_SECONDS else f"STALE ({age:.0f}s old)"
    return (f"Order flow: {status}  |  {state.get('receiving', 0)}/"
            f"{state.get('subscribed', 0)} contracts receiving  |  "
            f"{state.get('packets_seen', 0):,} packets")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(describe())
