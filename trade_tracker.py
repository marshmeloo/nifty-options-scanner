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
import contextlib
from pathlib import Path
from datetime import date, datetime

import config
import costs
import logic_version
import scanner
from atomic_state import atomic_write_json

log = logging.getLogger("nifty_scanner")

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

OPEN_TRADES_PATH = STATE_DIR / "open_trades.json"
JOURNAL_PATH = LOG_DIR / "trade_journal.jsonl"
# Real trades close here for good once the breakeven-arm rule fires (see
# config.BREAKEVEN_ARM_R) -- this is a SEPARATE, purely observational
# journal of what that same contract did AFTERWARD, up to the original
# target/stop/EOD. Never affects real P&L or state["trades"].
BREAKEVEN_SHADOW_JOURNAL_PATH = LOG_DIR / "breakeven_shadow_journal.jsonl"
# Execution-delay measurement (added 2026-08-15). Every price this system
# acts on is the price at the instant a signal fired, but a real order is
# punched in some seconds LATER, and fills at whatever the market is then.
# Nothing in the backtest models that gap. These probes record, for each
# entry and each exit, what the same contract was priced at one and two
# cycles after the trigger -- purely observational, never affects real
# P&L or any trading decision. See _seed_delay_probe().
DELAY_PROBE_JOURNAL_PATH = LOG_DIR / "execution_delay_journal.jsonl"


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
            data.setdefault("stop_cooldowns", {})
            data.setdefault("direction_cooldowns", {})
            data.setdefault("breakeven_shadows", [])
            data.setdefault("delay_probes", [])
            return data
        if data.get("trades"):
            data["_stale_from_previous_session"] = True
            data.setdefault("stop_cooldowns", {})
            data.setdefault("direction_cooldowns", {})
            data.setdefault("breakeven_shadows", [])
            data.setdefault("delay_probes", [])
            return data
    return {"date": date.today().isoformat(), "trades": [], "opened_today": 0,
            "stop_cooldowns": {}, "direction_cooldowns": {}, "breakeven_shadows": [],
            "delay_probes": []}


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
    already_journaled = _journaled_trade_ids()

    for trade in state["trades"]:
        # Recovery must be idempotent. It runs at startup, before the
        # market is necessarily reachable, and can legitimately be
        # attempted more than once (a failed quote fetch, a supervisor
        # restart, a second manual start later the same morning). Without
        # this guard the same trade gets a SECOND journal entry with a
        # different exit price -- which is exactly what happened on
        # 2026-07-24, when 8 trades from the 2026-07-23 session were
        # journaled once at 00:39 (no quote available) and again at 07:28
        # (fresh quote), double-counting Rs -1,976 of P&L and inflating
        # every downstream read of the journal.
        if trade.get("id") in already_journaled:
            log.info(
                f"  Skipping recovery for {trade['strike']} {trade['option_type']} "
                f"({trade.get('id')}) -- already present in the journal."
            )
            continue
        quote = quote_lookup.get((trade["strike"], trade["option_type"]))
        if quote is not None:
            exit_ltp = exit_price_for(quote)
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
        trade.update(costs.apply_to_trade(trade))
        _append_journal(trade)
        recovered.append(trade)

    # Clear the settled trades from state HERE, the same way
    # force_close_end_of_day() does. Leaving that to the caller made the
    # whole operation non-idempotent: anything that interrupted the
    # process between journaling and the caller's save_open_trades() left
    # the trades still marked OPEN on disk, so the next start recovered
    # and journaled them all over again.
    state["trades"] = []
    return recovered


def save_open_trades(state: dict):
    """
    Atomic write (see atomic_state.py) -- a process killed mid-write
    (e.g. supervisor.py force-terminating a frozen main_live.py) can
    never leave a corrupted state file.
    """
    atomic_write_json(OPEN_TRADES_PATH, state, indent=2)


# When False, closing a trade computes every field as normal but writes
# nothing to disk. Replay and tests MUST set this: they run the real
# close path over historical or synthetic data, and without it every
# replayed trade is appended to the live journal as though it had really
# happened -- silently corrupting the trade record that all performance
# analysis depends on. Use the journal_writes_disabled() context manager
# rather than setting this directly.
JOURNAL_WRITES_ENABLED = True


@contextlib.contextmanager
def journal_writes_disabled():
    """Run close/settle logic without persisting anything to the journal."""
    global JOURNAL_WRITES_ENABLED
    previous = JOURNAL_WRITES_ENABLED
    JOURNAL_WRITES_ENABLED = False
    try:
        yield
    finally:
        JOURNAL_WRITES_ENABLED = previous


def _append_journal(entry: dict, path: Path = None):
    if not JOURNAL_WRITES_ENABLED:
        return
    with open(path or JOURNAL_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _journaled_trade_ids() -> set:
    """Every trade id already written to the journal, for idempotency checks."""
    if not JOURNAL_PATH.exists():
        return set()
    ids = set()
    for line in JOURNAL_PATH.read_text().strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("id"):
            ids.add(entry["id"])
    return ids


def _load_recent_journal(limit=None) -> list:
    limit = limit or config.JOURNAL_LOOKBACK_FOR_LEARNING
    if not JOURNAL_PATH.exists():
        return []
    lines = [l for l in JOURNAL_PATH.read_text().strip().split("\n") if l]
    return [json.loads(l) for l in lines[-limit:]]


# Phrases matched against a reason string to derive a coarse tag.
#
# THESE MUST BE ANCHORED TO THE ACTUAL LABEL scanner.py EMITS, not to a
# bare word. "support" alone matched the phrase "supportS this direction"
# (and "supportS this contract"), which scanner._score_levels attaches to
# EVERY favourable level of any kind and scanner emits on every momentum
# read. Result, measured on the 2026-08-31 session: all 27 trades carried
# a "support" tag and NOT ONE came from a real support level -- 27 of 27
# false positives. Genuine level mentions are rare (14 "Support level"
# and 27 "Resistance level" across the entire journal history), so the
# tag was ~100% noise wherever it appeared.
#
# The labels come from scanner._KIND_LABELS and read
# "Support level at this strike (...)", so matching "support level" is
# both precise and stable.
_TAG_PHRASES = {
    "long_buildup": "long buildup",
    "short_buildup": "short buildup",
    "short_covering": "short covering",
    "long_unwinding": "long unwinding",
    "iv_rich": "iv rich",
    "iv_cheap": "iv cheap",
    "order_block": "order block",
    "fvg": "fvg",
    "sweep": "liquidity sweep",
    "resistance": "resistance level",
    "support": "support level",
    "trend_continuation": "trend continuation",
    "counter_trend": "counter-trend",
}

# scanner._score_levels marks a level that points the WRONG way for this
# contract with this phrase. Such a reason is evidence against the trade,
# so tagging it as though it were confluence inverts the meaning of the
# tag for every consumer downstream.
_AGAINST_MARKER = "argues against"


def _reason_tags(reasons: list) -> list:
    """
    Coarse tags pulled from reason strings, used for win-rate lookup.

    Reasons that ARGUE AGAINST the contract are skipped: these tags feed
    tag_win_rates()/apply_learned_adjustment(), which ask "what did the
    winning trades have in common". A bearish level sitting next to a CE
    is not a reason that CE was taken -- counting it would teach the
    opposite of the truth.
    """
    tags = []
    usable = [r.lower() for r in reasons if _AGAINST_MARKER not in r.lower()]
    for tag, phrase in _TAG_PHRASES.items():
        if any(phrase in r for r in usable):
            tags.append(tag)
    return tags


def base_win_rate(limit=None) -> tuple:
    """
    (base_win_rate, total_decided_trades) across the journal -- the
    reference every tag is measured against.

    This is the number the old design was missing. It compared each tag
    to an absolute 0.4 threshold, but the journal's own base rate was
    22%, so every tag fell below 0.4 and every tag got penalised -- even
    the ones performing ABOVE this strategy's own average. A tag is only
    informative relative to how the system does overall.
    """
    decided = [e for e in _load_recent_journal(limit) if e.get("outcome") in ("WIN", "LOSS")]
    if not decided:
        return 0.0, 0
    wins = sum(1 for e in decided if e["outcome"] == "WIN")
    return wins / len(decided), len(decided)


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


def shrunk_tag_rate(wins: int, losses: int, base: float) -> float:
    """
    Empirical-Bayes estimate of a tag's true win rate:
        (wins + k*base) / (n + k)
    where k = config.TAG_PRIOR_STRENGTH, i.e. the tag starts out with k
    "virtual" trades sitting exactly at the base rate and has to earn
    its way away from that with real evidence.

    This is what makes small samples safe. At n=3, the observed rate can
    be anything from 0% to 100% by luck; shrinkage pulls it to within a
    rounding error of base, so the resulting adjustment is ~0 rather
    than a confident -0.5.
    """
    n = wins + losses
    k = config.TAG_PRIOR_STRENGTH
    if n + k == 0:
        return base
    return (wins + k * base) / (n + k)


def apply_learned_adjustment(score: float, reasons: list) -> tuple:
    """
    Nudges a score based on how a candidate's reason-tags have performed
    RELATIVE TO THE SYSTEM'S OWN BASE WIN RATE. Explicitly rule-based:
    a lookup over past outcomes with a shrinkage prior, not a trained
    model. Returns (adjusted_score, notes_explaining_why).

    Three guards, each fixing a specific failure of the previous design
    (see the config.py block for the measured evidence):
      - No adjustment at all below MIN_TRADES_FOR_ANY_ADJUSTMENT total
        decided trades. With single digits there is nothing to learn.
      - Each tag is shrunk toward the base rate, so a thin sample can't
        produce a confident penalty.
      - The total is capped at +/-MAX_TOTAL_TAG_ADJUSTMENT, because tags
        co-occur heavily and uncapped summing double-counts one
        underlying market condition -- which is what made the old
        penalty grow with the raw score.

    A fourth, blunter guard sits in front of all three: config.
    LEARNED_TAG_ADJUSTMENT_ENABLED. Turned off 2026-08-18 -- even with
    the two thresholds above satisfied on the live journal, that was
    still judged too small a sample to trust for per-tag differentiation
    (see config.py's comment for the numbers). When False, this returns
    immediately with the score untouched, before even reading the
    journal.
    """
    if not config.LEARNED_TAG_ADJUSTMENT_ENABLED:
        return round(score, 2), ["Learned tag adjustment disabled (config.LEARNED_TAG_ADJUSTMENT_ENABLED=False)."]

    base, total_trades = base_win_rate()
    if total_trades < config.MIN_TRADES_FOR_ANY_ADJUSTMENT:
        return round(score, 2), [
            f"No learned adjustment applied -- only {total_trades} decided trade(s) on record, "
            f"need {config.MIN_TRADES_FOR_ANY_ADJUSTMENT} before tag win rates mean anything."
        ]

    rates = tag_win_rates()
    notes = []
    raw_delta = 0.0
    for tag in _reason_tags(reasons):
        if tag not in rates:
            continue
        w, l, observed = rates[tag]
        shrunk = shrunk_tag_rate(w, l, base)
        delta = (shrunk - base) * config.TAG_ADJUSTMENT_SCALE
        if abs(delta) < 0.05:
            continue  # not meaningfully different from average; stay quiet
        raw_delta += delta
        direction = "above" if delta > 0 else "below"
        notes.append(
            f"'{tag}' {observed:.0%} vs {base:.0%} base ({w}W/{l}L) -- "
            f"{direction} average, {delta:+.2f}"
        )

    cap = config.MAX_TOTAL_TAG_ADJUSTMENT
    total_delta = max(-cap, min(cap, raw_delta))
    if total_delta != raw_delta:
        notes.append(f"Total tag adjustment capped at {total_delta:+.2f} (uncapped {raw_delta:+.2f})")

    return round(score + total_delta, 2), notes


def rr_milestone_stats(limit=None) -> dict:
    """
    Across closed journal entries: how many trades ever reached each
    configured R milestone, and what the actual exits looked like.

    This is the dataset for answering "what should DEFAULT_TARGET_RR be?"
    with evidence instead of intuition. `reached` counts trades whose
    BEST excursion touched that multiple at any point while open -- i.e.
    the trades a target set there would have converted into winners.

    Entries from before R-tracking existed simply have no max_r_reached
    and are excluded from `sample`, rather than silently counted as
    zero -- an unmeasured trade is not a failed one.
    """
    entries = [
        e for e in _load_recent_journal(limit)
        if e.get("max_r_reached") is not None and e.get("outcome") not in (None, "OPEN")
    ]
    if not entries:
        return {"sample": 0, "milestones": [], "median_max_r": None}

    max_rs = sorted(e["max_r_reached"] for e in entries)
    median_max_r = max_rs[len(max_rs) // 2]

    milestones = []
    for m in config.RR_MILESTONES:
        reached = sum(1 for e in entries if e["max_r_reached"] >= m)

        # Simulated expectancy if the target had been set here: a trade
        # whose peak reached this level would have exited AT it; every
        # other trade keeps the R it actually finished at.
        #
        # This matters because hit-rate alone is actively misleading --
        # a low target hits far more often but wins less each time, and
        # can easily be WORSE overall. Expectancy is the number that
        # decides it. Still a simulation: it assumes a fill exactly at
        # target and that reaching the level early wouldn't have changed
        # anything afterwards.
        #
        # NET of costs wherever the trade recorded them. Costs are close
        # to a fixed R-charge per round trip, so they penalise LOW targets
        # disproportionately -- the charge is amortised over a smaller
        # win. Comparing targets on gross numbers therefore flatters the
        # low ones and would push the choice in the wrong direction.
        simulated = []
        for e in entries:
            cost_r = e.get("cost_r") or 0.0
            if e["max_r_reached"] >= m:
                simulated.append(m - cost_r)
            else:
                simulated.append(e.get("r_at_exit_net", e.get("r_at_exit") or 0.0))
        milestones.append({
            "r": m,
            "reached": reached,
            "pct": round(reached / len(entries) * 100, 1),
            "expectancy_r": round(sum(simulated) / len(simulated), 3),
        })

    return {
        "sample": len(entries),
        "median_max_r": median_max_r,
        "best_max_r": max_rs[-1],
        "milestones": milestones,
        "current_target_r": config.DEFAULT_TARGET_RR,
    }


def summarize_rr_milestones(limit=None) -> str:
    """Human-readable version of rr_milestone_stats(), for session startup."""
    stats = rr_milestone_stats(limit)
    if not stats["sample"]:
        return ("R-multiple history: none yet (tracking starts with trades opened "
                "from now on -- earlier journal entries predate it).")

    lines = [
        f"R-multiple history ({stats['sample']} closed trades, "
        f"current target {stats['current_target_r']:g}R):"
    ]
    best_exp = max(m["expectancy_r"] for m in stats["milestones"])
    for m in stats["milestones"]:
        marker = "  <-- current" if m["r"] == stats["current_target_r"] else ""
        if m["expectancy_r"] == best_exp and best_exp > 0:
            marker += "  [best expectancy]"
        lines.append(
            f"  {m['r']:>4g}R reached by {m['reached']:>3} ({m['pct']:>5.1f}%), "
            f"simulated expectancy {m['expectancy_r']:+.3f}R{marker}"
        )
    lines.append(f"  median best excursion: {stats['median_max_r']:+.2f}R, best ever: {stats['best_max_r']:+.2f}R")
    if best_exp <= 0:
        lines.append(
            "  NOTE: every simulated target has negative expectancy on this sample -- "
            "that points at entry quality, not target placement."
        )
    return "\n".join(lines)


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


def risk_unit(trade: dict) -> float:
    """
    1R for this trade: the distance from entry to stop. Returns None if
    that distance is zero or missing, so callers can skip R math rather
    than divide by zero.
    """
    entry, stop = trade.get("entry"), trade.get("stop")
    if entry is None or stop is None:
        return None
    unit = entry - stop
    return unit if unit > 0 else None


def r_multiple(trade: dict, price: float) -> float:
    """How many R above entry `price` sits. Negative below entry."""
    unit = risk_unit(trade)
    if unit is None or price is None:
        return None
    return round((price - trade["entry"]) / unit, 2)


def _update_excursion(trade: dict, current_ltp: float, timestamp=None):
    """
    Track the running best/worst LTP seen this trade, regardless of
    whether this cycle closes it -- see open_new_trade()'s comment on
    why this exists. Called every single cycle a trade is evaluated,
    including the cycle that ends up closing it.

    Also records which R-multiple milestones the trade has touched and
    WHEN it first touched each. That timing is the part intuition can't
    reconstruct afterwards: "reached 1.2R at 11:04, then gave it all back
    and stopped out at 14:05" is a completely different lesson from
    "never got going." See config.RR_MILESTONES.
    """
    trade["max_ltp_seen"] = max(trade.get("max_ltp_seen", trade["entry"]), current_ltp)
    trade["min_ltp_seen"] = min(trade.get("min_ltp_seen", trade["entry"]), current_ltp)

    current_r = r_multiple(trade, current_ltp)
    if current_r is None:
        return

    prior_max_r = trade.get("max_r_seen")
    if prior_max_r is None or current_r > prior_max_r:
        trade["max_r_seen"] = current_r

    hits = trade.setdefault("rr_milestones_hit", {})
    stamp = (timestamp.isoformat() if hasattr(timestamp, "isoformat")
             else (timestamp or datetime.now().isoformat(timespec="seconds")))
    for milestone in config.RR_MILESTONES:
        key = str(milestone)
        if key not in hits and current_r >= milestone:
            hits[key] = stamp


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

    summary = {
        "max_ltp_seen": max_ltp,
        "min_ltp_seen": min_ltp,
        "max_favorable_pct": max_favorable_pct,
        "max_favorable_inr": _pnl_inr(entry, max_ltp, trade["lots"]),
        "max_adverse_pct": max_adverse_pct,
        "max_adverse_inr": _pnl_inr(entry, min_ltp, trade["lots"]),
        "capture_efficiency_pct": capture_efficiency_pct,
    }

    # R-multiple view of the same excursion. `max_r_reached` is the
    # headline: the best R this trade was EVER worth, whether or not the
    # exit captured it. `rr_would_have_won_at` lists every configured
    # target that this trade did in fact reach -- i.e. the targets under
    # which it would have been a winner instead of the outcome it got.
    unit = risk_unit(trade)
    if unit is not None:
        max_r = round((max_ltp - entry) / unit, 2)
        summary["max_r_reached"] = max_r
        summary["min_r_reached"] = round((min_ltp - entry) / unit, 2)
        summary["rr_milestones_hit"] = trade.get("rr_milestones_hit", {})
        summary["rr_would_have_won_at"] = [m for m in config.RR_MILESTONES if max_r >= m]
        exit_ltp = trade.get("exit_ltp")
        if exit_ltp is not None:
            summary["r_at_exit"] = round((exit_ltp - entry) / unit, 2)

    return summary


def _build_lesson(trade: dict, outcome: str) -> str:
    tags = trade.get("reason_tags", [])
    tag_text = ", ".join(tags) if tags else "no tagged reasons"
    base = ""
    if outcome == "WIN":
        base = f"Hit target ({trade['pnl_pct']:+.1f}%). Contributing signals: {tag_text}."
    elif outcome == "LOSS":
        base = f"Hit stop ({trade['pnl_pct']:+.1f}%). Re-examine reliance on: {tag_text}."
    elif outcome == "BREAKEVEN_STOP":
        base = (
            f"Armed breakeven stop after reaching {trade.get('max_r_seen', 0):+.2f}R, "
            f"closed back near entry ({trade['pnl_pct']:+.1f}%). Signals: {tag_text}. "
            f"Now watching this contract to see if it would have hit target anyway."
        )
    elif outcome == "REVERSAL_EXIT":
        base = (
            f"Closed early ({trade['pnl_pct']:+.1f}%) -- a fully-qualified opposite-direction "
            f"signal arrived while this was open (see config.REVERSAL_EXIT_ENABLED), read as "
            f"evidence the market had turned rather than held to its original stop/target/EOD."
        )
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

    # The R view: which targets this trade WOULD have hit. Stated plainly
    # because it's the single most actionable line for deciding
    # DEFAULT_TARGET_RR later, and it's invisible in a pass/fail outcome.
    max_r = trade.get("max_r_reached")
    would_have_won = trade.get("rr_would_have_won_at")
    if max_r is not None:
        if would_have_won and outcome != "WIN":
            targets = ", ".join(f"{m:g}R" for m in would_have_won)
            base += (
                f" Peaked at {max_r:+.2f}R (aiming for {config.DEFAULT_TARGET_RR:g}R) -- "
                f"would have hit target at: {targets}."
            )
        elif not would_have_won:
            base += f" Never got beyond {max_r:+.2f}R, so no configured target was ever in reach."
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
        "stop_basis": getattr(plan, "stop_basis", ""),
        "lots": plan.lots,
        "score_at_entry": setup.score,
        # Which logic version opened this trade. Without it, journal
        # statistics silently average across incompatible versions of the
        # system -- see logic_version.py.
        "logic_version": logic_version.compute(),
        # Named, human-stable strategy identity -- separate from the hash
        # above. See config.STRATEGY_NAME's own comment and
        # STRATEGY_VERSIONS.md: this stays constant across trades so a
        # later comparison against a challenger version has a clean label
        # to group by, without needing to decode a config hash first.
        "strategy_name": getattr(config, "STRATEGY_NAME", None),
        "strategy_version": getattr(config, "STRATEGY_VERSION", None),
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
        # R-multiple milestones touched while open -> {"1.0": timestamp}.
        # Evidence for choosing a target later; see config.RR_MILESTONES.
        "max_r_seen": 0.0,
        "rr_milestones_hit": {},
    }


def exit_price_for(quote) -> float:
    """
    What closing a LONG position in this contract would actually realise:
    the bid, not the last traded price. Falls back to LTP when the source
    publishes no book. Used for marking open trades AND for target/stop
    checks -- a stop that triggers on an unreachable LTP is not a stop.
    """
    if getattr(config, "USE_BID_ASK_FILLS", False) and getattr(quote, "has_book", False):
        return quote.sell_price
    return quote.ltp


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

        current_ltp = exit_price_for(quote)
        _update_excursion(trade, current_ltp, snapshot.timestamp)

        # Breakeven-arm: once this trade has EVER been up config.BREAKEVEN_ARM_R
        # (max_r_seen already reflects this cycle, _update_excursion ran just
        # above), a return to entry price closes it here instead of letting it
        # keep falling toward the original stop. Checked AFTER target (an
        # outright win is strictly better and takes priority) and BEFORE the
        # original stop (which a real winning trade's price must cross entry
        # to even approach, so this fires first for every armed trade in
        # practice -- the original stop remains the fallback for trades that
        # never got going at all). See config.BREAKEVEN_ARM_R's own comment.
        armed = (
            getattr(config, "BREAKEVEN_ARM_R", None) is not None
            and trade.get("max_r_seen") is not None
            and trade["max_r_seen"] >= config.BREAKEVEN_ARM_R
        )

        outcome = None
        if current_ltp >= trade["target"]:
            outcome = "WIN"
        elif armed and current_ltp <= trade["entry"]:
            outcome = "BREAKEVEN_STOP"
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
            trade.update(costs.apply_to_trade(trade))
            _append_journal(trade)
            if outcome == "LOSS":
                _record_stop_cooldown(state, trade)
                _record_direction_cooldown(state, trade)
            elif outcome == "BREAKEVEN_STOP":
                _seed_breakeven_shadow(state, trade, snapshot.timestamp)
            # Exit-side delay probe. Deliberately seeded for EVERY outcome,
            # not just stops: a target exit is delayed too, and only
            # sampling the adverse ones would overstate the measured cost.
            _seed_delay_probe(state, trade, "exit", current_ltp, snapshot.timestamp)
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
            exit_ltp = exit_price_for(quote)
            trade["exit_price_estimated"] = False
            _update_excursion(trade, exit_ltp, snapshot.timestamp)  # in case this final tick is a new high/low not yet captured

        trade["closed_at"] = snapshot.timestamp.isoformat()
        trade["exit_ltp"] = exit_ltp
        trade["outcome"] = "EOD_CLOSE"
        trade["pnl_pct"] = round((exit_ltp - trade["entry"]) / trade["entry"] * 100, 1)
        trade["pnl_inr"] = _pnl_inr(trade["entry"], exit_ltp, trade["lots"])
        trade.update(_excursion_summary(trade))
        trade["lesson"] = _build_lesson(trade, "EOD_CLOSE")
        if trade["exit_price_estimated"]:
            trade["lesson"] += " NOTE: exit price could not be confirmed at close -- this P&L is an estimate, not a confirmed outcome."
        trade.update(costs.apply_to_trade(trade))
        _append_journal(trade)
        closed.append(trade)
    state["trades"] = []
    return closed


def _seed_breakeven_shadow(state: dict, trade: dict, timestamp) -> None:
    """
    Called the instant a real trade closes via the breakeven-arm rule
    (see update_open_trades). Starts a lightweight, purely observational
    record of the SAME contract so there's an ongoing answer to "did this
    one actually go on to hit target after being stopped out early" --
    never touches state["trades"], real P&L, or the real journal again.
    """
    unit = risk_unit(trade)
    r_at_close = round((trade["exit_ltp"] - trade["entry"]) / unit, 2) if unit else None
    shadow = {
        "id": f"{trade['id']}_shadow",
        "strike": trade["strike"],
        "option_type": trade["option_type"],
        "entry": trade["entry"],
        "target": trade["target"],
        "stop": trade["stop"],
        "lots": trade["lots"],
        "reason_tags": trade.get("reason_tags", []),
        "breakeven_closed_at": trade["closed_at"],
        "breakeven_exit_ltp": trade["exit_ltp"],
        "breakeven_pnl_inr": trade["pnl_inr"],
        "status": "WATCHING",
        "max_ltp_seen_after_breakeven": trade["exit_ltp"],
        "min_ltp_seen_after_breakeven": trade["exit_ltp"],
        "max_r_seen_after_breakeven": r_at_close,
    }
    state.setdefault("breakeven_shadows", []).append(shadow)


def update_breakeven_shadows(state: dict, snapshot) -> list:
    """
    Call every cycle, same as update_open_trades. Walks every contract a
    breakeven-close is still watching forward against its ORIGINAL
    target/stop (config.DEFAULT_TARGET_RR's 2R, not the breakeven exit) --
    purely to answer, with real forward data, whether BREAKEVEN_ARM_R is
    actually the right threshold. Resolved shadows are journaled to
    BREAKEVEN_SHADOW_JOURNAL_PATH, a file the real P&L/win-rate numbers
    never read from.
    """
    resolved = []
    still_watching = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}

    for shadow in state.get("breakeven_shadows", []):
        quote = quote_lookup.get((shadow["strike"], shadow["option_type"]))
        if quote is None:
            still_watching.append(shadow)
            continue

        current_ltp = exit_price_for(quote)
        unit = shadow["entry"] - shadow["stop"]
        current_r = round((current_ltp - shadow["entry"]) / unit, 2) if unit else None

        shadow["max_ltp_seen_after_breakeven"] = max(shadow.get("max_ltp_seen_after_breakeven", current_ltp), current_ltp)
        shadow["min_ltp_seen_after_breakeven"] = min(shadow.get("min_ltp_seen_after_breakeven", current_ltp), current_ltp)
        if current_r is not None:
            prior = shadow.get("max_r_seen_after_breakeven")
            if prior is None or current_r > prior:
                shadow["max_r_seen_after_breakeven"] = current_r

        result = None
        if current_ltp >= shadow["target"]:
            result = "WOULD_HAVE_HIT_TARGET"
        elif current_ltp <= shadow["stop"]:
            result = "WOULD_HAVE_HIT_STOP"

        if result:
            shadow["resolved_at"] = snapshot.timestamp.isoformat()
            shadow["resolved_ltp"] = current_ltp
            shadow["resolution"] = result
            shadow["status"] = "RESOLVED"
            _append_journal(shadow, path=BREAKEVEN_SHADOW_JOURNAL_PATH)
            resolved.append(shadow)
        else:
            shadow["current_ltp_after_breakeven"] = current_ltp
            still_watching.append(shadow)

    state["breakeven_shadows"] = still_watching
    return resolved


def force_close_breakeven_shadows(state: dict, snapshot) -> list:
    """EOD finalization for any breakeven shadows still being watched when
    the market closes -- same reasoning as force_close_end_of_day, but
    resolution is "still open at close", not a real trade outcome."""
    closed = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}
    for shadow in state.get("breakeven_shadows", []):
        quote = quote_lookup.get((shadow["strike"], shadow["option_type"]))
        if quote is not None:
            current_ltp = exit_price_for(quote)
            unit = shadow["entry"] - shadow["stop"]
            if unit:
                current_r = round((current_ltp - shadow["entry"]) / unit, 2)
                prior = shadow.get("max_r_seen_after_breakeven")
                if prior is None or current_r > prior:
                    shadow["max_r_seen_after_breakeven"] = current_r
            shadow["resolved_ltp"] = current_ltp
        else:
            shadow["resolved_ltp"] = None
        shadow["resolved_at"] = snapshot.timestamp.isoformat()
        shadow["resolution"] = "EOD_STILL_WATCHING"
        shadow["status"] = "RESOLVED"
        _append_journal(shadow, path=BREAKEVEN_SHADOW_JOURNAL_PATH)
        closed.append(shadow)
    state["breakeven_shadows"] = []
    return closed


def _seed_delay_probe(state: dict, trade: dict, leg: str, signal_price: float, timestamp) -> None:
    """
    Start measuring what an order-entry delay would have cost on this
    leg. `leg` is "entry" (we would be BUYING) or "exit" (SELLING).

    Records nothing but observations -- no trading decision reads these,
    and the trade's own entry/exit/P&L are untouched. See
    config.DELAY_PROBE_SAMPLES for why this exists.
    """
    if not getattr(config, "DELAY_PROBE_SAMPLES", 0):
        return
    state.setdefault("delay_probes", []).append({
        "trade_id": trade.get("id"),
        "strike": trade["strike"],
        "option_type": trade["option_type"],
        "leg": leg,
        "lots": trade.get("lots", 1),
        "signal_at": timestamp.isoformat(),
        "signal_price": signal_price,
        "samples": [],
    })


def _delay_sample(probe: dict, price: float, timestamp) -> dict:
    """
    One post-signal observation. `adverse_inr` is signed so that
    NEGATIVE always means "this delay cost money", for both legs:
    buying later at a higher price is bad, selling later at a lower
    price is bad. Reading a single sign convention wrongly is exactly
    how a slippage study ends up concluding delay is free.
    """
    signal_price = probe["signal_price"]
    delta = round(price - signal_price, 2)
    qty = probe.get("lots", 1) * getattr(config, "NIFTY_LOT_SIZE", 65)
    if probe["leg"] == "entry":
        adverse_inr = round(-delta * qty, 2)   # paying MORE later = worse
    else:
        adverse_inr = round(delta * qty, 2)    # receiving LESS later = worse
    signal_ts = datetime.fromisoformat(probe["signal_at"])
    return {
        "at": timestamp.isoformat(),
        "delay_sec": round((timestamp - signal_ts).total_seconds(), 1),
        "price": price,
        "delta_vs_signal": delta,
        "adverse_inr": adverse_inr,
    }


def update_delay_probes(state: dict, snapshot) -> list:
    """
    Call every cycle. Samples each open probe's contract once per cycle
    until it has config.DELAY_PROBE_SAMPLES observations, then journals
    and retires it. Returns the probes completed this cycle.
    """
    wanted = getattr(config, "DELAY_PROBE_SAMPLES", 0)
    if not wanted:
        state["delay_probes"] = []
        return []

    completed = []
    still_open = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}

    for probe in state.get("delay_probes", []):
        quote = quote_lookup.get((probe["strike"], probe["option_type"]))
        if quote is None:
            still_open.append(probe)   # missing quote: keep waiting, same as trades/shadows
            continue

        probe["samples"].append(_delay_sample(probe, exit_price_for(quote), snapshot.timestamp))
        if len(probe["samples"]) >= wanted:
            probe["completed_at"] = snapshot.timestamp.isoformat()
            probe["complete"] = True
            _append_journal(probe, path=DELAY_PROBE_JOURNAL_PATH)
            completed.append(probe)
        else:
            still_open.append(probe)

    state["delay_probes"] = still_open
    return completed


def force_close_delay_probes(state: dict, snapshot) -> list:
    """
    EOD finalisation. Journals every probe still collecting, flagged
    incomplete -- a probe seeded near the close legitimately never gets
    its full sample count, and silently dropping those would bias the
    measurement toward mid-session conditions only.
    """
    closed = []
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}
    for probe in state.get("delay_probes", []):
        quote = quote_lookup.get((probe["strike"], probe["option_type"]))
        if quote is not None:
            probe["samples"].append(_delay_sample(probe, exit_price_for(quote), snapshot.timestamp))
        probe["completed_at"] = snapshot.timestamp.isoformat()
        probe["complete"] = len(probe["samples"]) >= getattr(config, "DELAY_PROBE_SAMPLES", 0)
        probe["ended_at_market_close"] = True
        _append_journal(probe, path=DELAY_PROBE_JOURNAL_PATH)
        closed.append(probe)
    state["delay_probes"] = []
    return closed


def compute_risk_state(state: dict) -> tuple:
    """
    Live (open_exposure_pct, daily_loss_pct) for risk_checker.check().

    These two numbers gate MAX_TOTAL_EXPOSURE_PCT and the
    MAX_DAILY_LOSS_PCT circuit breaker. Until now main_live.py passed
    hardcoded 0.0 for both and never updated them, which meant BOTH gates
    were dead code -- the daily-loss breaker could never trip no matter
    how much the day lost. Everything needed to compute them was already
    on disk (today's journal entries + the open-trades state), it just
    was never read back.

      - open_exposure_pct: capital at risk across open trades, expressed
        the same way plan.risk_pct_of_capital is (stop distance x lot
        size x lots, as a % of TOTAL_CAPITAL) so the two are additive.
      - daily_loss_pct: today's REALIZED loss (from the journal) plus
        current UNREALIZED loss (from open trades), as a positive % of
        capital. Returns 0.0 when the day is net flat or up -- the
        breaker only cares about losses.
    """
    capital = config.TOTAL_CAPITAL
    lot_size = getattr(config, "NIFTY_LOT_SIZE", 65)

    open_exposure_inr = sum(
        (t["entry"] - t["stop"]) * lot_size * t["lots"] for t in state.get("trades", [])
    )

    unrealized_inr = sum(t.get("running_pnl_inr", 0.0) or 0.0 for t in state.get("trades", []))

    # Deduplicate by trade id. The journal is append-only and a
    # pre-2026-07-28 recovery bug left genuine duplicate entries in it
    # (same id, two different exit prices). Summing blind would
    # double-count those into the daily-loss breaker and trip it early.
    today = date.today().isoformat()
    realized_by_id = {}
    for entry in _load_recent_journal(limit=500):
        closed_at = entry.get("closed_at") or ""
        if closed_at.startswith(today):
            key = entry.get("id") or f"{closed_at}_{entry.get('strike')}_{entry.get('option_type')}"
            realized_by_id[key] = entry.get("pnl_inr", 0.0) or 0.0
    realized_inr = sum(realized_by_id.values())

    net_inr = realized_inr + unrealized_inr
    daily_loss_pct = round(-net_inr / capital * 100, 3) if net_inr < 0 else 0.0

    return round(open_exposure_inr / capital * 100, 3), daily_loss_pct


def expiry_day_rules(expiry: str, now: datetime) -> tuple:
    """
    Returns (conviction_bar, blocked_reason_or_None) for a contract
    expiring on `expiry`, evaluated at `now`.

    Only applies when the contract expires TODAY. See the config comment
    on EXPIRY_DAY_* for why same-day expiry was originally treated
    differently, and on EXPIRY_DAY_RULES_ENABLED for why it's currently
    OFF: research/expiry_day_rule_study.py (2026-08-26) found the
    population this rule blocks outperforms the system average, not
    underperforms it.
    """
    bar = config.MIN_CONVICTION_SCORE_TO_TRACK
    if not getattr(config, "EXPIRY_DAY_RULES_ENABLED", True):
        return bar, None
    if expiry != now.date().isoformat():
        return bar, None

    bar += config.EXPIRY_DAY_EXTRA_CONVICTION

    cutoff_h, cutoff_m = (int(x) for x in config.EXPIRY_DAY_NO_NEW_TRADES_AFTER.split(":"))
    if (now.hour, now.minute) >= (cutoff_h, cutoff_m):
        return bar, (
            f"expiry day and past {config.EXPIRY_DAY_NO_NEW_TRADES_AFTER} -- "
            f"buying same-day-expiry premium this late is mostly paying theta"
        )
    return bar, None


def cluster_cap_blocks(state: dict, strike: float, option_type: str, now: datetime) -> bool:
    """
    True if `strike`/`option_type` should be rejected because a
    same-direction position is ALREADY OPEN nearby and was opened
    recently -- see config.CLUSTER_CAP_ENABLED's own comment for the
    real sessions this is for. Live counterpart of
    shadow.correlated_cluster_blocked(), same rule, applied to
    state["trades"] instead of a backtest's `positions` list.

    OFF unless config.CLUSTER_CAP_ENABLED is True -- Anchor v1.0's real
    config leaves this False, so this always returns False for Anchor's
    live process regardless of what any other process's config sets.

    The band comparison is INCLUSIVE (`<=`), fixed 2026-08-17. It was
    `<`, which silently never fired for Bank Nifty: its strikes are
    100pts apart and the scanner's typical burst skips every other one,
    landing candidates EXACTLY 200pts apart -- the exact configured band
    -- and `200 < 200` is False. Confirmed against that day's real
    session, where Sentinel opened 5 strikes of a 9-strike correlated
    PE cluster it was specifically built to block (57100/300/500/700/900,
    all within 3 minutes, all losses). An exact-boundary strike is the
    typical case for Bank Nifty, not an edge case, so the boundary
    belongs inside the band.
    """
    if not getattr(config, "CLUSTER_CAP_ENABLED", False):
        return False
    band = getattr(config, "CLUSTER_CAP_ADJACENCY_POINTS", None)
    window = getattr(config, "CLUSTER_CAP_WINDOW_MINUTES", None)
    if band is None or window is None:
        return False
    for t in state.get("trades", []):
        if t["option_type"] != option_type:
            continue
        opened_at = t.get("opened_at")
        if not opened_at:
            continue
        age_minutes = (now - datetime.fromisoformat(opened_at)).total_seconds() / 60
        if 0 <= age_minutes <= window and abs(t["strike"] - strike) <= band:
            return True
    return False


def opposite_direction_blocks(state: dict, option_type: str) -> bool:
    """
    True if a position in the OPPOSITE option_type is already open --
    blocks a new PE while any CE is open, or vice versa. See
    config.OPPOSITE_DIRECTION_GATE_ENABLED's own comment for the
    2026-08-27 incident and the 6-year backtest behind this.

    Deliberately simpler than cluster_cap_blocks(): no adjacency band,
    no time window -- ANY opposite-direction position currently open
    blocks a new entry, for its full open lifetime. The measured
    population was "same-day opposite-direction overlap" full stop,
    not "opposite-direction AND nearby AND recent"; narrowing this the
    way CLUSTER_CAP_WINDOW_MINUTES narrows the same-direction cap would
    be inventing a parameter nothing here was tested against.

    OFF only if config.OPPOSITE_DIRECTION_GATE_ENABLED is explicitly
    False -- unlike CLUSTER_CAP_ENABLED, this defaults True and applies
    to Anchor and Sentinel alike (see that config comment for why this
    one is not a Sentinel-only differentiator).
    """
    if not getattr(config, "OPPOSITE_DIRECTION_GATE_ENABLED", True):
        return False
    return any(t["option_type"] != option_type for t in state.get("trades", []))


def _cooldown_key(strike, option_type) -> str:
    return f"{strike}_{option_type}"


def is_repeat_of_stopped_plan(state: dict, strike, option_type, entry: float) -> bool:
    """
    True if a trade on this strike+type was already stopped out today at
    an entry price within config.REENTRY_PRICE_TOLERANCE_PCT of `entry`.

    Same entry price means the same stop and the same target (both are
    derived from entry), i.e. the identical failed plan -- not a new read
    on the same strike. A materially different premium produces different
    geometry and is allowed through. See the config comment for why this
    replaced a time-based cooldown.
    """
    prior_entries = state.get("stop_cooldowns", {}).get(_cooldown_key(strike, option_type), [])
    tolerance = config.REENTRY_PRICE_TOLERANCE_PCT
    for prior in prior_entries:
        prior_entry = prior["entry"]
        if prior_entry and abs(entry - prior_entry) / prior_entry * 100 <= tolerance:
            return True
    return False


def _record_stop_cooldown(state: dict, trade: dict):
    """Arm the re-entry gate for this strike+type at the price that failed."""
    state.setdefault("stop_cooldowns", {})
    key = _cooldown_key(trade["strike"], trade["option_type"])
    state["stop_cooldowns"].setdefault(key, []).append(
        {"entry": trade["entry"], "closed_at": trade["closed_at"]}
    )


def is_direction_chase(state: dict, option_type: str, now: datetime) -> bool:
    """
    True if a trade in the SAME DIRECTION (option_type) stopped out
    within the last config.DIRECTION_CHASE_COOLDOWN_MINUTES -- checked
    regardless of strike.

    A different pathology from is_repeat_of_stopped_plan's per-strike
    price check, which is blind to the same losing read repeated across
    DIFFERENT strikes as each attempt stops out and the option chain
    shifts underneath it -- see config.py's own comment on
    DIRECTION_CHASE_COOLDOWN_MINUTES for the 2026-08-10 incident (8 CE
    trades, 8 different strikes, one after another, Rs -5,153) that
    prompted this and the measurement behind the design.

    Loss-gated only, matching REENTRY_PRICE_TOLERANCE_PCT's own stated
    philosophy: a WIN isn't evidence the directional read was wrong,
    only a LOSS is -- a working read is allowed to continue.
    """
    entries = state.get("direction_cooldowns", {}).get(option_type, [])
    for e in entries:
        closed_at = e.get("closed_at")
        if not closed_at:
            continue
        gap_minutes = (now - datetime.fromisoformat(closed_at)).total_seconds() / 60
        if 0 <= gap_minutes <= config.DIRECTION_CHASE_COOLDOWN_MINUTES:
            return True
    return False


def _record_direction_cooldown(state: dict, trade: dict):
    """Arm the direction-chase gate for this option_type at the time it failed."""
    state.setdefault("direction_cooldowns", {})
    state["direction_cooldowns"].setdefault(trade["option_type"], []).append(
        {"closed_at": trade["closed_at"]}
    )


def _reversal_exit_opposite_positions(state: dict, new_option_type: str, snapshot) -> list:
    """
    Closes every currently-open position on the OPPOSITE side of
    new_option_type, right now, at this cycle's quote -- see
    config.REVERSAL_EXIT_ENABLED's own comment for the evidence behind
    this (research/reversal_exit_study.py + the real backtest in
    research/reversal_exit_backtest_approx.py's successor). Reuses the
    exact same closing/journalling fields update_open_trades() writes
    for every other exit, tagged "REVERSAL_EXIT" so it is distinguishable
    in the journal from a real stop/target/breakeven/EOD close.

    Deliberately does NOT arm the direction-chase cooldown
    (_record_direction_cooldown) the way a LOSS does -- that gate exists
    to stop chasing the SAME losing directional read; this is the
    opposite situation, exiting BECAUSE the read has flipped, and the
    new (opposite) direction is already being suppressed by
    opposite_direction_blocks() itself, not chased.
    """
    quote_lookup = {(q.strike, q.option_type): q for q in snapshot.chain}
    closed, still_open = [], []
    for trade in state["trades"]:
        if trade["option_type"] == new_option_type:
            still_open.append(trade)
            continue
        quote = quote_lookup.get((trade["strike"], trade["option_type"]))
        if quote is None:
            # No quote for this strike this cycle -- can't price an exit,
            # leave it open and try again next cycle rather than guess.
            still_open.append(trade)
            continue

        current_ltp = exit_price_for(quote)
        _update_excursion(trade, current_ltp, snapshot.timestamp)
        trade["closed_at"] = snapshot.timestamp.isoformat()
        trade["exit_ltp"] = current_ltp
        trade["outcome"] = "REVERSAL_EXIT"
        trade["pnl_pct"] = round((current_ltp - trade["entry"]) / trade["entry"] * 100, 1)
        trade["pnl_inr"] = _pnl_inr(trade["entry"], current_ltp, trade["lots"])
        trade.update(_excursion_summary(trade))
        trade["lesson"] = _build_lesson(trade, "REVERSAL_EXIT")
        trade.update(costs.apply_to_trade(trade))
        _append_journal(trade)
        _seed_delay_probe(state, trade, "exit", current_ltp, snapshot.timestamp)
        closed.append(trade)

    state["trades"] = still_open
    return closed


def try_open_new_trade(setups_with_plans, state, snapshot, bias_label=None, bias_score=None):
    """
    setups_with_plans: list of (Setup, TradePlan, RiskVerdict), best-first.
    Opens AT MOST ONE new trade per cycle, only if: daily cap not reached,
    conviction clears the raised bar (after the learned adjustment),
    there isn't already an open trade on the same strike+type, it isn't
    a repeat of a plan that already stopped out today at the same price
    (see is_repeat_of_stopped_plan), it isn't chasing the same
    losing directional read across a different strike (see
    is_direction_chase), and there isn't already an open position in
    the OPPOSITE direction (see opposite_direction_blocks).
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
        if is_repeat_of_stopped_plan(state, setup.strike, setup.option_type, plan.entry):
            continue
        if is_direction_chase(state, setup.option_type, snapshot.timestamp):
            continue
        if cluster_cap_blocks(state, setup.strike, setup.option_type, snapshot.timestamp):
            continue

        conviction_bar, blocked = expiry_day_rules(setup.expiry, snapshot.timestamp)
        if blocked:
            continue

        # Top-down bias gate. Applied AFTER the raw score so the score
        # stays a pure read of the contract's own evidence, and the bias
        # adjustment shows up as its own auditable step.
        bias_blocked, bias_penalty, bias_note = scanner.apply_bias_gate(
            setup, bias_label, bias_score
        )
        if bias_blocked:
            continue

        adjusted_score, learn_notes = apply_learned_adjustment(setup.score, setup.reasons)
        adjusted_score = round(adjusted_score - bias_penalty, 2)
        if bias_note:
            learn_notes = list(learn_notes) + [bias_note]
        if adjusted_score < conviction_bar:
            continue

        # Opposite-direction gate, checked HERE -- after every other gate
        # -- rather than earlier (matches shadow.py's run_policy(), moved
        # 2026-08-27 for the same reason: only a setup that ALSO cleared
        # everything else is a genuine, fully-qualified signal, worth
        # treating as evidence for REVERSAL_EXIT_ENABLED below rather than
        # noise that would have been rejected anyway regardless of
        # direction.
        if opposite_direction_blocks(state, setup.option_type):
            if getattr(config, "REVERSAL_EXIT_ENABLED", False):
                _reversal_exit_opposite_positions(state, setup.option_type, snapshot)
            continue

        trade = open_new_trade(setup, plan, snapshot)
        trade["adjusted_score_at_entry"] = adjusted_score
        trade["learned_adjustment_notes"] = learn_notes
        state["trades"].append(trade)
        state["opened_today"] += 1
        _seed_delay_probe(state, trade, "entry", plan.entry, snapshot.timestamp)
        return trade

    return None
