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
from pathlib import Path
from datetime import date

import config

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

OPEN_TRADES_PATH = STATE_DIR / "open_trades.json"
JOURNAL_PATH = LOG_DIR / "trade_journal.jsonl"


def load_open_trades() -> dict:
    """Daily state: which trades are currently open, how many opened today."""
    if OPEN_TRADES_PATH.exists():
        data = json.loads(OPEN_TRADES_PATH.read_text())
        if data.get("date") == date.today().isoformat():
            return data
    return {"date": date.today().isoformat(), "trades": [], "opened_today": 0}


def save_open_trades(state: dict):
    OPEN_TRADES_PATH.write_text(json.dumps(state, indent=2))


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


def _build_lesson(trade: dict, outcome: str) -> str:
    tags = trade.get("reason_tags", [])
    tag_text = ", ".join(tags) if tags else "no tagged reasons"
    if outcome == "WIN":
        return f"Hit target ({trade['pnl_pct']:+.1f}%). Contributing signals: {tag_text}."
    elif outcome == "LOSS":
        return f"Hit stop ({trade['pnl_pct']:+.1f}%). Re-examine reliance on: {tag_text}."
    else:  # EOD_CLOSE
        return f"Closed at end of day, neither target nor stop hit ({trade['pnl_pct']:+.1f}%). Signals: {tag_text}."


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
    }


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
            trade["lesson"] = _build_lesson(trade, outcome)
            _append_journal(trade)
            closed_this_cycle.append(trade)
        else:
            trade["current_ltp"] = current_ltp
            trade["running_pnl_pct"] = round((current_ltp - trade["entry"]) / trade["entry"] * 100, 1)
            still_open.append(trade)

    state["trades"] = still_open
    return closed_this_cycle


def force_close_end_of_day(state: dict, snapshot) -> list:
    """Call once when market close is detected. Journals remaining open trades as EOD_CLOSE."""
    closed = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}
    for trade in state["trades"]:
        quote = quote_lookup.get((trade["strike"], trade["option_type"]))
        exit_ltp = quote.ltp if quote else trade["entry"]
        trade["closed_at"] = snapshot.timestamp.isoformat()
        trade["exit_ltp"] = exit_ltp
        trade["outcome"] = "EOD_CLOSE"
        trade["pnl_pct"] = round((exit_ltp - trade["entry"]) / trade["entry"] * 100, 1)
        trade["lesson"] = _build_lesson(trade, "EOD_CLOSE")
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
