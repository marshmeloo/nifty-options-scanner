"""
Tracks OPEN positions for the pure price-action strategy: a single long
CE/PE per trade, opened on a price_structure.generate_signal() break and
closed on target, stop, or -- if neither triggers first -- the option's
own real expiry (settled at LTP, falling back to intrinsic value if the
strike no longer appears in the chain).

Deliberately its own file rather than reusing trade_tracker.py: that
module's learned-tag-adjustment and stop-cooldown machinery exists to
learn from a MOMENTUM sample large enough to be statistically meaningful
(config.MIN_TAG_SAMPLES_FOR_ADJUSTMENT), and this strategy's entire
2-year backtest found only 19-24 distinct trades total -- reusing that
machinery here would either silently do nothing (sample floor never
reached) or, worse, risk pooling price-action's structural reasons into
momentum's own learned tag statistics if the two ever shared a journal.
Kept minimal: open/mark/close and R-multiple milestone tracking, nothing
more, until there is a real sample to learn from.

NOTHING HERE PLACES A REAL BROKER ORDER. "Opening a position" only ever
means writing to this module's own state file -- same guarantee as
trade_tracker.py, condor_tracker.py, and directional_spread_tracker.py.

State supports MULTIPLE open trades (a list, keyed loosely by `pair`)
rather than a single slot, since ACTIVE_PAIRS can run daily_hourly and
intraday simultaneously -- each pair gets at most one open trade of its
own (see has_open_trade), but the two pairs don't block each other.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import config_price_action as pcfg
import costs
import logic_version
from atomic_state import atomic_write_json

STATE_DIR = Path(__file__).parent / "state"
LOG_DIR = Path(__file__).parent / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

STATE_PATH = Path(pcfg.STATE_PATH)
JOURNAL_PATH = Path(pcfg.JOURNAL_PATH)


def _logic_version() -> str:
    """
    Same shape as logic_version.compute() (config-hash + git SHA), but
    over config_price_action's OWN constants rather than momentum's
    curated DECISION_RELEVANT_SETTINGS list -- this strategy's config
    file is small enough that every uppercase constant in it is
    decision-relevant, so no separate curated list is needed.
    """
    fp = {k: v for k, v in vars(pcfg).items() if k.isupper()}
    digest = hashlib.sha256(json.dumps(fp, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:8]
    return f"{digest}-{logic_version.git_sha()}"


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"trades": []}


def save_state(state: dict):
    atomic_write_json(STATE_PATH, state, default=str, indent=2)


def _append_journal(trade: dict):
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(trade, default=str) + "\n")


def has_open_trade(state: dict, pair: str) -> bool:
    return any(t["pair"] == pair and t["status"] == "OPEN" for t in state.get("trades", []))


def open_trades(state: dict) -> list:
    return [t for t in state.get("trades", []) if t["status"] == "OPEN"]


def tracked_strikes(state: dict) -> set:
    """
    Strikes every OPEN trade needs to keep seeing in the chain fetch,
    regardless of how far they drift from spot -- same reasoning and
    same name-shape as main_condor.py's own _tracked_strikes, so a
    strike doesn't silently vanish from the snapshot mid-hold (the
    2026-07-30 condor MTM incident this project has already hit once).
    """
    return {(t["strike"], t["option_type"]) for t in open_trades(state)}


def risk_unit(trade: dict):
    entry, stop = trade.get("entry"), trade.get("stop")
    if entry is None or stop is None:
        return None
    unit = entry - stop
    return unit if unit > 0 else None


def r_multiple(trade: dict, price: float):
    unit = risk_unit(trade)
    if unit is None or price is None:
        return None
    return round((price - trade["entry"]) / unit, 2)


def _update_excursion(trade: dict, current_ltp: float, timestamp=None):
    trade["max_ltp_seen"] = max(trade.get("max_ltp_seen", trade["entry"]), current_ltp)
    trade["min_ltp_seen"] = min(trade.get("min_ltp_seen", trade["entry"]), current_ltp)

    current_r = r_multiple(trade, current_ltp)
    if current_r is None:
        return

    prior = trade.get("max_r_seen")
    if prior is None or current_r > prior:
        trade["max_r_seen"] = current_r

    hits = trade.setdefault("rr_milestones_hit", {})
    stamp = (timestamp.isoformat() if hasattr(timestamp, "isoformat")
             else (timestamp or datetime.now().isoformat(timespec="seconds")))
    for milestone in pcfg.RR_MILESTONES:
        key = str(milestone)
        if key not in hits and current_r >= milestone:
            hits[key] = stamp


def _pnl_inr(entry: float, exit_or_current: float, lots: int) -> float:
    return round((exit_or_current - entry) * pcfg.NIFTY_LOT_SIZE * lots, 2)


def _exit_price_for(quote) -> float:
    """What closing this LONG position would actually realise: the bid,
    or LTP if the source publishes no book. See config_price_action's
    USE_BID_ASK_FILLS docstring."""
    if pcfg.USE_BID_ASK_FILLS and getattr(quote, "has_book", False):
        return quote.sell_price
    return quote.ltp


def open_position(state: dict, setup, plan, pair: str, snapshot) -> dict:
    """Locks in a new tracked trade -- entry/target/stop frozen from here on."""
    trade = {
        "id": f"{pair}_{setup.strike}_{setup.option_type}_{snapshot.timestamp.strftime('%Y%m%d%H%M%S')}",
        "pair": pair,
        "strike": setup.strike,
        "option_type": setup.option_type,
        "expiry": setup.expiry,
        "opened_at": snapshot.timestamp.isoformat(),
        "entry": plan.entry,
        "entry_basis": plan.entry_basis,
        "target": plan.target,
        "stop": plan.stop,
        "stop_basis": plan.stop_basis,
        "lots": plan.lots,
        "logic_version": _logic_version(),
        "reasons_at_entry": list(setup.reasons),
        "status": "OPEN",
        "max_ltp_seen": plan.entry,
        "min_ltp_seen": plan.entry,
        "max_r_seen": 0.0,
        "rr_milestones_hit": {},
    }
    state.setdefault("trades", []).append(trade)
    save_state(state)
    return trade


def _close(trade: dict, exit_price: float, outcome: str, timestamp, reason: str = "") -> dict:
    trade["closed_at"] = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
    # costs.apply_to_trade reads "exit_ltp" -- same field name
    # trade_tracker.py's momentum trades use, kept consistent here too.
    trade["exit_ltp"] = round(exit_price, 2)
    trade["outcome"] = outcome
    trade["exit_reason"] = reason or outcome
    trade["pnl_pct"] = round((exit_price - trade["entry"]) / trade["entry"] * 100, 1)
    trade["pnl_inr"] = _pnl_inr(trade["entry"], exit_price, trade["lots"])
    trade["status"] = "CLOSED"
    trade.update(costs.apply_to_trade(trade))
    _append_journal(trade)
    return trade


def update_positions(state: dict, snapshot) -> list:
    """
    Checks each OPEN trade's current premium against its frozen target/
    stop, and settles any past their own contract's real expiry. Closes
    and journals whatever qualifies this cycle; returns that list.

    A cycle where this trade's strike has no quote is SKIPPED for the
    target/stop check rather than treated as a zero -- missing
    information is not a zero, same principle as condor/directional-
    spread's own trackers. Only at real settlement (past expiry) does a
    missing quote fall back to intrinsic value, since by then there is
    no "next cycle" left to wait for a quote to reappear.
    """
    closed_this_cycle = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}
    today = snapshot.timestamp.date()

    for trade in state.get("trades", []):
        if trade["status"] != "OPEN":
            continue

        quote = quote_lookup.get((trade["strike"], trade["option_type"]))
        try:
            expiry_date = datetime.strptime(trade["expiry"], "%Y-%m-%d").date()
        except ValueError:
            expiry_date = None
        past_expiry = expiry_date is not None and today > expiry_date

        if quote is not None:
            current_ltp = _exit_price_for(quote)
            _update_excursion(trade, current_ltp, snapshot.timestamp)

            outcome = None
            if current_ltp >= trade["target"]:
                outcome = "WIN"
            elif current_ltp <= trade["stop"]:
                outcome = "LOSS"
            if outcome:
                closed_this_cycle.append(_close(trade, current_ltp, outcome, snapshot.timestamp))
                continue

            trade["current_ltp"] = current_ltp
            trade["running_pnl_pct"] = round((current_ltp - trade["entry"]) / trade["entry"] * 100, 1)
            trade["running_pnl_inr"] = _pnl_inr(trade["entry"], current_ltp, trade["lots"])

            if past_expiry:
                closed_this_cycle.append(_close(trade, current_ltp, "EXPIRY_CLOSE", snapshot.timestamp))
            continue

        if past_expiry:
            # Strike no longer quoted AND past its own expiry -- settle
            # at intrinsic value rather than leaving it open forever.
            intrinsic = (max(snapshot.spot - trade["strike"], 0.0) if trade["option_type"] == "CE"
                        else max(trade["strike"] - snapshot.spot, 0.0))
            outcome = "EXPIRY_CLOSE" if intrinsic > 0 else "LOSS"
            closed_this_cycle.append(_close(trade, intrinsic, outcome, snapshot.timestamp,
                                            reason="expiry_settlement_no_quote"))
            continue
        # Not past expiry and no quote this cycle -- keep tracking, don't lose it silently.

    # Trades transition to CLOSED in place above (already journaled) --
    # only OPEN ones need to persist in the working state file at all.
    state["trades"] = [t for t in state.get("trades", []) if t["status"] == "OPEN"]
    save_state(state)
    return closed_this_cycle
