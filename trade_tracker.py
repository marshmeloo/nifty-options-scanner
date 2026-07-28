"""
Trade tracker + journal.

Fixes the "same strike, brand-new plan every 90 seconds" problem: the
scanner correctly re-scans the whole chain every cycle to FIND setups,
but until now nothing distinguished "the same setup drifting" from
"a genuinely new trade." Every cycle silently overwrote the previous
plan for whatever still scored well, which produced noise, not signal.

This module:
  1. Enforces a daily cap on NEW trades opened (config.MAX_NEW_TRADES_PER_DAY)
     and a much higher conviction bar to open one
     (config.MIN_CONVICTION_SCORE_TO_TRACK) than the watchlist threshold.
  2. Once opened, a trade's entry/target/stop are FROZEN and tracked
     against live price until it actually closes (hits target, hits
     stop, or end of day) - never silently recalculated.
  3. Every closed trade is appended to logs/trade_journal.jsonl with a
     plain-language lesson.
  4. A simple RULE-BASED adjustment (NOT machine learning - this is a
     win-rate lookup over recent journal history, not a trained model)
     nudges a candidate's score up or down based on how its reason-tags
     have historically performed. Framed honestly: this is "keep a
     spreadsheet of what worked and lean on it a little," not a
     self-training AI. It only starts influencing anything once a tag
     has enough samples (config.MIN_TAG_SAMPLES_FOR_ADJUSTMENT).
"""

import json
import os
import logging
from pathlib import Path
from datetime import date, datetime

import config
from atomic_state import atomic_write_json

log = logging.getLogger("nifty_scanner")

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

OPEN_TRADES_PATH = STATE_DIR / "open_trades.json"
JOURNAL_PATH = LOG_DIR / "trade_journal.jsonl"


def load_open_trades() -> dict:
    """
    Daily state: which trades are currently open, how many opened today.

    IMPORTANT: if the saved state is from a PREVIOUS date and still has
    open trades (e.g. the process was killed with Ctrl+C or crashed
    before the 15:30 EOD settlement ever ran), this does NOT silently
    discard them. Doing that used to be exactly what happened on
    2026-07-23: the script was stopped around 15:20, the market-close
    transition that triggers force_close_all() was never observed, and a
    naive "if not today's date, start fresh" reload would have silently
    wiped those 8 trades the next time the script started -- no journal
    entry, no error, just gone. Instead, stale trades are returned as-is
    with a flag so the caller (main_live.py's startup) can settle and
    journal them via settle_stale_trades() BEFORE starting a fresh day.
    """
    if OPEN_TRADES_PATH.exists():
        data = json.loads(OPEN_TRADES_PATH.read_text())
        if data.get("date") == date.today().isoformat():
            return data
        if data.get("trades"):
            data["_stale_from_previous_session"] = True
            return data
    return {"date": date.today().isoformat(), "trades": [], "opened_today": 0}


def settle_stale_trades(state: dict, snapshot=None) -> list:
    """
    Settle trades left over from an interrupted previous session (see
    load_open_trades()'s note above). Exit price preference order:
      1. A live quote from `snapshot`, if one is provided and covers
         this strike (most accurate -- reflects current reality).
      2. The trade's own last recorded `current_ltp` from before the
         interruption (the last real observed price, not a guess).
      3. Entry price, flagged as estimated, if neither is available.
    Every recovered trade is clearly tagged so it's auditable in the
    journal, not indistinguishable from a normal close.
    """
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain} if snapshot else {}
    recovered = []

    for trade in state["trades"]:
        quote = quote_lookup.get((trade["strike"], trade["option_type"]))
        if quote is not None:
            exit_ltp = quote.ltp
            estimated = False
            price_source = "fresh quote at recovery time"
        elif trade.get("current_ltp") is not None:
            exit_ltp = trade["current_ltp"]
            estimated = True
            price_source = "last live quote before the session was interrupted"
        else:
            exit_ltp = trade["entry"]
            estimated = True
            price_source = "entry price (no live quote ever recorded for this trade)"

        trade["closed_at"] = datetime.now().isoformat(timespec="seconds")
        trade["exit_ltp"] = exit_ltp
        trade["outcome"] = "RECOVERED_INTERRUPTED_SESSION"
        trade["pnl_pct"] = round((exit_ltp - trade["entry"]) / trade["entry"] * 100, 1)
        trade["pnl_inr"] = _pnl_inr(trade["entry"], exit_ltp, trade["lots"])
        trade["exit_price_estimated"] = estimated
        _update_excursion(trade, exit_ltp)
        trade.update(_excursion_summary(trade))
        trade["lesson"] = (
            _build_lesson(trade, "RECOVERED_INTERRUPTED_SESSION")
            + f" NOTE: this trade was recovered after the tracking session for {state.get('date')} was "
            f"interrupted before end-of-day settlement (e.g. a Ctrl+C or crash before 15:30). "
            f"Exit price source: {price_source}. Treat this outcome as approximate, not a confirmed exit."
        )
        _append_journal(trade)
        recovered.append(trade)

    return recovered


def save_open_trades(state: dict):
    """
    Atomic write (see atomic_state.py) -- a process killed mid-write
    (e.g. supervisor.py force-terminating a frozen main_live.py) can
    never leave a corrupted state file.
    """
    atomic_write_json(OPEN_TRADES_PATH, state, indent=2)


def _append_journal(entry: dict):
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_recent_journal(limit=None) -> list:
    limit = limit or config.JOURNAL_LOOKBACK_FOR_LEARNING
    if not JOURNAL_PATH.exists():
        return []
    lines = [l for l in JOURNAL_PATH.read_text().strip().split("\n") if l]
    return [json.loads(l) for l in lines[-limit:]]


_TAG_PHRASES = {
    "long_buildup": "long buildup",
    "short_buildup": "short buildup",
    "short_covering": "short covering",
    "long_unwinding": "long unwinding",
    "iv_rich": "iv rich",
    "iv_cheap": "iv cheap",
    "order_block": "order block",
    "fvg": "fvg",
    "sweep": "sweep",
    "resistance": "resistance",
    "support": "support",
    "trend_continuation": "trend continuation",
    "counter_trend": "counter-trend",
}


def _reason_tags(reasons: list) -> list:
    """Coarse tags pulled from reason strings, used for win-rate lookup."""
    tags = []
    joined_lower = [r.lower() for r in reasons]
    for tag, phrase in _TAG_PHRASES.items():
        if any(phrase in r for r in joined_lower):
            tags.append(tag)
    return tags


def tag_win_rates(limit=None) -> dict:
    """{tag: (wins, losses, win_rate)} from recent journal history."""
    journal = _load_recent_journal(limit)
    stats = {}
    for entry in journal:
        outcome = entry.get("outcome")
        if outcome not in ("WIN", "LOSS"):
            continue
        for tag in entry.get("reason_tags", []):
            w, l = stats.get(tag, (0, 0))
            w += 1 if outcome == "WIN" else 0
            l += 1 if outcome == "LOSS" else 0
            stats[tag] = (w, l)
    return {
        tag: (w, l, round(w / (w + l), 2))
        for tag, (w, l) in stats.items()
        if (w + l) >= config.MIN_TAG_SAMPLES_FOR_ADJUSTMENT
    }


def apply_learned_adjustment(score: float, reasons: list) -> tuple:
    """
    Nudges a score based on historical win rate of its reason-tags.
    Explicitly rule-based: a lookup over past outcomes, not a trained
    model. Returns (adjusted_score, notes_explaining_why).
    """
    rates = tag_win_rates()
    notes = []
    adjusted = score
    for tag in _reason_tags(reasons):
        if tag in rates:
            w, l, rate = rates[tag]
            if rate < config.WEAK_TAG_WIN_RATE:
                adjusted -= 0.5
                notes.append(f"'{tag}' historically weak ({w}W/{l}L, {rate:.0%}) — score reduced")
            elif rate > config.STRONG_TAG_WIN_RATE:
                adjusted += 0.25
                notes.append(f"'{tag}' historically strong ({w}W/{l}L, {rate:.0%}) — score boosted")
    return round(adjusted, 2), notes


def summarize_recent_lessons(limit=None) -> str:
    """One-line-per-tag summary of what's worked/not, for session startup."""
    rates = tag_win_rates(limit)
    if not rates:
        return "No trade history yet — nothing learned so far, starting neutral."
    lines = []
    for tag, (w, l, rate) in sorted(rates.items(), key=lambda kv: kv[1][2]):
        flag = "weak" if rate < config.WEAK_TAG_WIN_RATE else ("strong" if rate > config.STRONG_TAG_WIN_RATE else "neutral")
        lines.append(f"  {tag}: {w}W/{l}L ({rate:.0%}) [{flag}]")
    return "Recent signal performance:\n" + "\n".join(lines)


def _update_excursion(trade: dict, current_ltp: float):
    """
    Track the running best/worst LTP seen this trade, regardless of
    whether this cycle closes it -- see open_new_trade()'s comment on
    why this exists. Called every single cycle a trade is evaluated,
    including the cycle that ends up closing it.
    """
    trade["max_ltp_seen"] = max(trade.get("max_ltp_seen", trade["entry"]), current_ltp)
    trade["min_ltp_seen"] = min(trade.get("min_ltp_seen", trade["entry"]), current_ltp)


def _excursion_summary(trade: dict) -> dict:
    """
    Derived MFE/MAE stats computed at close time. `capture_efficiency_pct`
    is the number that actually answers "did we give back a big move
    before closing": of the best favorable move this trade ever had,
    what fraction did the ACTUAL exit capture? 100%+ means it closed at
    or beyond its peak (e.g. target hit exactly at the high). A low or
    negative number means a real pullback ate most or all of an earlier
    favorable move before the trade closed.
    """
    entry = trade["entry"]
    max_ltp = trade.get("max_ltp_seen", entry)
    min_ltp = trade.get("min_ltp_seen", entry)
    max_favorable_pct = round((max_ltp - entry) / entry * 100, 1)
    max_adverse_pct = round((min_ltp - entry) / entry * 100, 1)
    pnl_pct = trade.get("pnl_pct")

    capture_efficiency_pct = None
    if pnl_pct is not None and max_favorable_pct > 0:
        capture_efficiency_pct = round(pnl_pct / max_favorable_pct * 100, 1)

    return {
        "max_ltp_seen": max_ltp,
        "min_ltp_seen": min_ltp,
        "max_favorable_pct": max_favorable_pct,
        "max_favorable_inr": _pnl_inr(entry, max_ltp, trade["lots"]),
        "max_adverse_pct": max_adverse_pct,
        "max_adverse_inr": _pnl_inr(entry, min_ltp, trade["lots"]),
        "capture_efficiency_pct": capture_efficiency_pct,
    }


def _build_lesson(trade: dict, outcome: str) -> str:
    tags = trade.get("reason_tags", [])
    tag_text = ", ".join(tags) if tags else "no tagged reasons"
    base = ""
    if outcome == "WIN":
        base = f"Hit target ({trade['pnl_pct']:+.1f}%). Contributing signals: {tag_text}."
    elif outcome == "LOSS":
        base = f"Hit stop ({trade['pnl_pct']:+.1f}%). Re-examine reliance on: {tag_text}."
    else:  # EOD_CLOSE, RECOVERED_INTERRUPTED_SESSION, etc.
        base = f"Closed at end of day, neither target nor stop hit ({trade['pnl_pct']:+.1f}%). Signals: {tag_text}."

    # Append the excursion read whenever there's something worth flagging:
    # a real gap between the best move this trade had and what actually
    # got captured at exit. Skip it when the trade barely moved either
    # way, or when it captured close to its full favorable move already
    # -- no point noting "captured 98%" as if it were a lesson.
    max_fav = trade.get("max_favorable_pct")
    capture_eff = trade.get("capture_efficiency_pct")
    if max_fav is not None and max_fav > 1.0 and capture_eff is not None and capture_eff < 85:
        base += (
            f" Reached as high as {trade['max_ltp_seen']} ({max_fav:+.1f}% from entry) before closing at "
            f"{trade.get('exit_ltp')} ({trade['pnl_pct']:+.1f}%) -- only captured {capture_eff:.0f}% of "
            f"that favorable move. Worth reviewing whether the target/stop or an exit rule should adapt "
            f"once a trade has moved this far in its favor."
        )
    return base


def open_new_trade(setup, plan, snapshot) -> dict:
    """Locks in a new tracked trade — entry/target/stop frozen from here on."""
    return {
        "id": f"{setup.strike}_{setup.option_type}_{snapshot.timestamp.strftime('%Y%m%d%H%M%S')}",
        "strike": setup.strike,
        "option_type": setup.option_type,
        "expiry": setup.expiry,
        "opened_at": snapshot.timestamp.isoformat(),
        "entry": plan.entry,
        "target": plan.target,
        "stop": plan.stop,
        "lots": plan.lots,
        "score_at_entry": setup.score,
        "reasons_at_entry": list(setup.reasons),
        "reason_tags": _reason_tags(setup.reasons),
        "status": "OPEN",
        # Max favorable / adverse excursion tracking (MFE/MAE) -- the
        # highest and lowest LTP seen at ANY point while the trade is
        # open, updated every cycle regardless of whether that cycle
        # closes the trade. This is what lets you later answer "how
        # often do we give back a big favorable move before exit" --
        # something the target/stop check alone can never tell you,
        # since it only ever looks at the current tick.
        "max_ltp_seen": plan.entry,
        "min_ltp_seen": plan.entry,
    }


def _pnl_inr(entry: float, exit_or_current: float, lots: int) -> float:
    """
    P&L in rupees for a long option position: (price move) * lot size *
    number of lots. Percentage alone doesn't tell you what a move is
    actually worth -- a 10% move on a 10-rupee option and a 10% move on a
    150-rupee option are very different amounts of real money.
    """
    lot_size = getattr(config, "NIFTY_LOT_SIZE", 65)
    return round((exit_or_current - entry) * lot_size * lots, 2)


def update_open_trades(state: dict, snapshot) -> list:
    """
    Checks each open trade's CURRENT premium against its FROZEN
    target/stop. Closes and journals any that hit either. Returns the
    list of trades that closed this cycle.
    """
    closed_this_cycle = []
    still_open = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}

    for trade in state["trades"]:
        quote = quote_lookup.get((trade["strike"], trade["option_type"]))
        if quote is None:
            # Out of this cycle's strike/premium filter range — can't
            # evaluate right now, keep tracking, don't lose it silently.
            still_open.append(trade)
            continue

        current_ltp = quote.ltp
        _update_excursion(trade, current_ltp)

        outcome = None
        if current_ltp >= trade["target"]:
            outcome = "WIN"
        elif current_ltp <= trade["stop"]:
            outcome = "LOSS"

        if outcome:
            trade["closed_at"] = snapshot.timestamp.isoformat()
            trade["exit_ltp"] = current_ltp
            trade["outcome"] = outcome
            trade["pnl_pct"] = round((current_ltp - trade["entry"]) / trade["entry"] * 100, 1)
            trade["pnl_inr"] = _pnl_inr(trade["entry"], current_ltp, trade["lots"])
            trade.update(_excursion_summary(trade))
            trade["lesson"] = _build_lesson(trade, outcome)
            _append_journal(trade)
            closed_this_cycle.append(trade)
        else:
            trade["current_ltp"] = current_ltp
            trade["running_pnl_pct"] = round((current_ltp - trade["entry"]) / trade["entry"] * 100, 1)
            trade["running_pnl_inr"] = _pnl_inr(trade["entry"], current_ltp, trade["lots"])
            still_open.append(trade)

    state["trades"] = still_open
    return closed_this_cycle


def force_close_end_of_day(state: dict, snapshot) -> list:
    """Call once when market close is detected. Journals remaining open trades as EOD_CLOSE."""
    closed = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}
    for trade in state["trades"]:
        quote = quote_lookup.get((trade["strike"], trade["option_type"]))

        if quote is None:
            # No quote for this strike in the closing snapshot -- can
            # happen if it's drifted outside STRIKE_RANGE_POINTS, or the
            # feed already went quiet right at the close. Falling back to
            # entry price is the only numeric option here, but doing that
            # SILENTLY used to make a trade that actually moved (see the
            # 2026-07-22 24000 PE incident: quote unavailable all day,
            # force-closed at entry showing a misleading flat 0.0%, when
            # it had actually traded up to 192 intraday) look like a
            # harmless flat close in the journal. Flag it clearly instead.
            log.info(
                f"  WARNING: no closing quote available for {trade['strike']} {trade['option_type']} -- "
                f"exit price defaulted to entry ({trade['entry']}). True EOD P&L is UNKNOWN, not necessarily flat."
            )
            exit_ltp = trade["entry"]
            trade["exit_price_estimated"] = True
        else:
            exit_ltp = quote.ltp
            trade["exit_price_estimated"] = False
            _update_excursion(trade, exit_ltp)  # in case this final tick is a new high/low not yet captured

        trade["closed_at"] = snapshot.timestamp.isoformat()
        trade["exit_ltp"] = exit_ltp
        trade["outcome"] = "EOD_CLOSE"
        trade["pnl_pct"] = round((exit_ltp - trade["entry"]) / trade["entry"] * 100, 1)
        trade["pnl_inr"] = _pnl_inr(trade["entry"], exit_ltp, trade["lots"])
        trade.update(_excursion_summary(trade))
        trade["lesson"] = _build_lesson(trade, "EOD_CLOSE")
        if trade["exit_price_estimated"]:
            trade["lesson"] += " NOTE: exit price could not be confirmed at close -- this P&L is an estimate, not a confirmed outcome."
        _append_journal(trade)
        closed.append(trade)
    state["trades"] = []
    return closed


def try_open_new_trade(setups_with_plans, state, snapshot):
    """
    setups_with_plans: list of (Setup, TradePlan, RiskVerdict), best-first.
    Opens AT MOST ONE new trade per cycle, only if: daily cap not reached,
    conviction clears the raised bar (after the learned adjustment), and
    there isn't already an open trade on the same strike+type.
    Returns the newly opened trade dict, or None.
    """
    if state["opened_today"] >= config.MAX_NEW_TRADES_PER_DAY:
        return None

    open_keys = {(t["strike"], t["option_type"]) for t in state["trades"]}

    for setup, plan, verdict in setups_with_plans:
        if verdict.decision != "APPROVED":
            continue
        if (setup.strike, setup.option_type) in open_keys:
            continue

        adjusted_score, learn_notes = apply_learned_adjustment(setup.score, setup.reasons)
        if adjusted_score < config.MIN_CONVICTION_SCORE_TO_TRACK:
            continue

        trade = open_new_trade(setup, plan, snapshot)
        trade["adjusted_score_at_entry"] = adjusted_score
        trade["learned_adjustment_notes"] = learn_notes
        state["trades"].append(trade)
        state["opened_today"] += 1
        return trade

    return None
