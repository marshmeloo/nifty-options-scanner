"""
Posts to Telegram so open positions, today's pre-market lean, and
intraday bias shifts are visible away from the screen, not just on the
live dashboard. Built 2026-08-16.

THREE MESSAGE TYPES
--------------------
1. Position snapshot, every CHECK_INTERVAL_SECONDS during market hours.
   Only sends when something is actually open -- a ping every 15 minutes
   on a flat day would just be noise you'd start ignoring.
2. Pre-market summary, once, right after run_forever() starts (reads
   today's premarket.py JSON output -- see brief_json_path()).
3. Bias-shift alert, whenever NIFTY's or Bank Nifty's market_bias label
   actually changes since the last check (not on every cycle it stays
   changed -- see _check_bias_shift()'s own docstring on why, including
   a real noise pattern found in recorded data before this was built).

WHAT COUNTS AS "OPEN" (message type 1)
----------------------------------------
Reuses dashboard_server.build_state() -- the exact same function that
feeds the live dashboard -- so this is never a second, possibly-drifted
implementation of "what's open." Covers, per index (NIFTY and Bank
Nifty): momentum (Anchor) open trades, iron condor (if OPEN), directional
spread (if OPEN), price-action open trades. NOT covered yet: Sentinel's
own positions -- the live dashboard itself doesn't surface those either.

START/STOP MESSAGES
--------------------
run_forever() also sends one Telegram message the moment it starts
(proves delivery is actually working right now, market open or not) and
one on a clean Ctrl+C stop. The stop message can only fire on a graceful
exit -- see run_forever()'s own docstring on why a forceful kill can't
trigger it.

SETUP
-----
Needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID -- see telegram_notifier.py's
own docstring for how to get them.

Run:
    python3 position_notifier.py                  # every 15 min, market hours only
    python3 position_notifier.py --interval 300    # more frequent, useful while testing
    python3 position_notifier.py --once             # single check, then exit (good for a manual test)
"""

import argparse
import json
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path

import dashboard_server
import premarket
import telegram_notifier
from atomic_state import atomic_write_json

log = logging.getLogger("position_notifier")

MARKET_OPEN = dtime(9, 15)
# A little past the nominal 15:30 close: options keep moving until close
# to 15:40 under SEBI's Closing Auction Session (see main_condor.py's
# EXPIRY_SETTLEMENT_CUTOFF and BACKLOG.md's CAS entry) -- a position can
# still be genuinely live in that window, worth one more check rather
# than going quiet right at 15:30.
MARKET_CLOSE = dtime(15, 40)

CHECK_INTERVAL_SECONDS = 900   # 15 minutes

BIAS_STATE_PATH = Path(__file__).parent / "state" / "position_notifier_bias.json"


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _pnl_icon(value) -> str:
    """Green/red/white circle by sign -- None (unavailable) and exactly 0
    both read as white, since neither one is a directional read."""
    if value is None or value == 0:
        return "⚪"       # ⚪
    return "\U0001f7e2" if value > 0 else "\U0001f534"   # 🟢 / 🔴


def _direction_icon(value) -> str:
    """Chart-up/chart-down/flat by sign -- used where the number is a
    trend-like read (an index's subtotal, a bias label) rather than a
    single line's own P&L."""
    if value is None or value == 0:
        return "➖"       # ➖
    return "\U0001f4c8" if value > 0 else "\U0001f4c9"   # 📈 / 📉


def _bias_icon(label) -> str:
    if not label:
        return "➖"
    if "bullish" in label:
        return "\U0001f4c8"
    if "bearish" in label:
        return "\U0001f4c9"
    return "➖"


def _momentum_lines(trades: list) -> tuple:
    """(lines, pnl subtotal) -- the subtotal is summed from the exact same
    running_pnl_inr each line displays, not read separately from anywhere
    else, so the footer total can never drift from what's shown above it."""
    lines = []
    subtotal = 0
    for t in trades:
        r = t.get("current_r")
        r_note = f"  R {r:+.2f}" if r is not None else ""
        pnl = t.get("running_pnl_inr", 0) or 0
        pnl_pct = t.get("running_pnl_pct", 0) or 0
        subtotal += pnl
        lines.append(
            f"  {_pnl_icon(pnl)} {t['strike']:.0f}{t['option_type']}  entry {t['entry']} → now {t.get('current_ltp', '?')}  "
            f"{pnl_pct:+.1f}% (₹{pnl:+,.0f}){r_note}"
        )
    return lines, subtotal


def _condor_line(position: dict) -> tuple:
    """(line or None, mtm) -- mtm is 0 when unavailable, not None, so it's
    safe to add straight into a running total without a null check at
    every call site."""
    if not position or position.get("status") != "OPEN":
        return None, 0
    plan = position.get("plan") or {}
    mtm = position.get("current_mtm_pnl_inr")
    mtm_note = f"₹{mtm:+,.0f}" if mtm is not None else "MTM unavailable"
    line = (f"  {_pnl_icon(mtm)} Condor: short {plan.get('short_ce_strike')}CE/{plan.get('short_pe_strike')}PE, "
            f"hedge {plan.get('hedge_ce_strike')}CE/{plan.get('hedge_pe_strike')}PE  {mtm_note}")
    return line, (mtm or 0)


def _spread_line(position: dict) -> tuple:
    if not position or position.get("status") != "OPEN":
        return None, 0
    plan = position.get("plan") or {}
    mtm = position.get("current_mtm_pnl_inr")
    mtm_note = f"₹{mtm:+,.0f}" if mtm is not None else "MTM unavailable"
    direction = "Bull put" if plan.get("direction") == "PE" else "Bear call"
    line = f"  {_pnl_icon(mtm)} Directional spread: {direction} {plan.get('short_strike')}/{plan.get('hedge_strike')}  {mtm_note}"
    return line, (mtm or 0)


def _index_section(label: str, momentum_trades: list, condor_position: dict,
                   spread_position: dict, price_action_trades: list) -> tuple:
    """(lines, pnl subtotal) for one index across every category."""
    body = []
    subtotal = 0

    momentum_lines, momentum_pnl = _momentum_lines(momentum_trades)
    if momentum_lines:
        body.append("  Momentum:")
        body.extend(momentum_lines)
        subtotal += momentum_pnl

    condor_line, condor_pnl = _condor_line(condor_position)
    if condor_line:
        body.append(condor_line)
        subtotal += condor_pnl

    spread_line, spread_pnl = _spread_line(spread_position)
    if spread_line:
        body.append(spread_line)
        subtotal += spread_pnl

    pa_lines, pa_pnl = _momentum_lines(price_action_trades)   # same trade shape as momentum's
    if pa_lines:
        body.append("  Price action:")
        body.extend(pa_lines)
        subtotal += pa_pnl

    if not body:
        return [], 0
    return [f"{_direction_icon(subtotal)} {label}"] + body, subtotal


def _position_lines_and_total(state: dict) -> tuple:
    """(lines across both indices, combined pnl total) -- shared by
    build_snapshot_message() and build_full_message() so there's exactly
    one place that assembles "what's open," not two that could drift."""
    bn = state.get("banknifty") or {}
    nifty_lines, nifty_pnl = _index_section(
        "NIFTY", state.get("open_trades") or [], state.get("condor_position"),
        state.get("directional_spread_position"), state.get("price_action_trades") or [],
    )
    bn_lines, bn_pnl = _index_section(
        "Bank Nifty", bn.get("open_trades") or [], bn.get("condor_position"),
        bn.get("directional_spread_position"), bn.get("price_action_trades") or [],
    )
    return nifty_lines + bn_lines, nifty_pnl + bn_pnl


def build_snapshot_message(state: dict = None):
    """Positions only, no radar section. Returns the message text, or None
    if nothing is currently open. Kept as its own function (build_full_message()
    below is what check_once() actually sends) since "what's open" is a
    useful thing to be able to ask for on its own."""
    state = state if state is not None else dashboard_server.build_state()
    sections, total_pnl = _position_lines_and_total(state)
    if not sections:
        return None

    # Summed from every line actually shown above (momentum + condor MTM +
    # spread MTM + price action, both indices) -- NOT state["totals"],
    # which only ever covers momentum's own open_trades and would silently
    # under-report whenever condor/spread/price-action also have something
    # open (caught by testing this against real dev data: condor and
    # spread positions were open with real non-zero MTM, but totals showed
    # Rs 0 since no momentum trade happened to be open at the same time).
    header = f"\U0001f4ca Live Positions — {datetime.now().strftime('%H:%M:%S')}"
    footer = f"{_pnl_icon(total_pnl)} Total open P&L: ₹{total_pnl:+,.0f}"
    return "\n".join([header, ""] + sections + ["", footer])


# --- radar (top-scored candidate, whether or not it opened) --------------------

def _radar_line(label: str, candidates: list):
    """
    The scanner's #1-ranked candidate this cycle -- candidates[0], not a
    re-sort here: main_live.py already sorts results by raw score
    descending before truncating to top_n and logging (see decision_log.log_cycle()),
    so the first entry in the list IS the highest-scored one. Returns None
    if this cycle produced no candidates at all (rare during market hours,
    but possible right at the very start of a session).
    """
    if not candidates:
        return None
    c = candidates[0]
    raw = c.get("raw_score")
    adjusted = c.get("adjusted_score")
    score_note = f"{raw}" if raw is not None else "?"
    if adjusted is not None and adjusted != raw:
        score_note += f" → {adjusted}"
    bar = c.get("conviction_bar")
    bar_note = f" (bar {bar})" if bar is not None else ""
    decision = c.get("final_decision") or "?"
    strike = c.get("strike")
    strike_note = f"{strike:.0f}" if strike is not None else "?"
    return f"  {label}: {strike_note}{c.get('option_type', '')} — score {score_note}{bar_note} — {decision}"


def _radar_section(state: dict) -> list:
    lines = []
    nifty_line = _radar_line("NIFTY", (state.get("latest_cycle") or {}).get("candidates") or [])
    if nifty_line:
        lines.append(nifty_line)
    bn_cycle = (state.get("banknifty") or {}).get("latest_cycle") or {}
    bn_line = _radar_line("Bank Nifty", bn_cycle.get("candidates") or [])
    if bn_line:
        lines.append(bn_line)
    if not lines:
        return []
    return ["\U0001f4e1 On the radar"] + lines


def build_full_message(state: dict = None):
    """
    Position snapshot + "On the radar" (each index's #1-scored candidate
    this cycle, always shown -- not gated by any score threshold, per
    explicit request) combined into one message.

    UNLIKE build_snapshot_message() ALONE, this sends even when nothing is
    open, as long as the scanner produced at least one candidate this
    cycle -- which is true almost every cycle during market hours. This is
    a deliberate change from the original silent-when-nothing-open design:
    radar visibility is most useful exactly when nothing's open yet, and
    showing the #1 candidate unconditionally (no score floor) means this
    now fires on essentially every 15-minute check during market hours,
    not only when a position exists.
    """
    state = state if state is not None else dashboard_server.build_state()
    position_lines, total_pnl = _position_lines_and_total(state)
    radar_lines = _radar_section(state)

    if not position_lines and not radar_lines:
        return None

    header = f"\U0001f4ca Live Positions — {datetime.now().strftime('%H:%M:%S')}"
    blocks = [header]
    if position_lines:
        blocks.append("\n".join(position_lines))
    if radar_lines:
        blocks.append("\n".join(radar_lines))
    blocks.append(
        f"{_pnl_icon(total_pnl)} Total open P&L: ₹{total_pnl:+,.0f}" if position_lines
        else "(no open positions right now)"
    )
    return "\n\n".join(blocks)


# --- pre-market summary -------------------------------------------------------

def build_premarket_message(brief: dict):
    """Condensed version of premarket.py's full brief -- the real thing runs
    to a dozen sections, too much for a phone glance. Returns None only if
    the brief itself is empty (should not normally happen)."""
    if not brief:
        return None

    bias = brief.get("bias") or "unavailable"
    lines = ["\U0001f305 Pre-Market Brief", "", f"{_bias_icon(bias)} Overall lean: {bias}"]

    expiry = brief.get("expiry") or {}
    if "error" not in expiry and expiry.get("expiry"):
        note = f"\U0001f4c5 Expiry: {expiry['expiry']} ({expiry.get('days_to_expiry')} day(s) away)"
        if expiry.get("is_expiry_day"):
            note += " — TODAY IS EXPIRY DAY"
        lines.append(note)

    prev = brief.get("previous_session") or {}
    if prev.get("close") is not None:
        pos_pct = prev.get("close_position_pct")
        pos_note = f", {pos_pct:.1f}% of day's range" if pos_pct is not None else ""
        lines.append(f"Previous session: closed {prev['close']}{pos_note}")

    bn_divergence = (brief.get("banknifty") or {}).get("divergence") or {}
    if bn_divergence.get("read"):
        lines.append(f"\U0001f3e6 Bank Nifty: {bn_divergence['read']}")

    smart_money = brief.get("smart_money") or {}
    if smart_money.get("lean") and smart_money["lean"] != "unavailable":
        lines.append(f"\U0001f9e0 Smart money: {smart_money['lean']}")

    chain_ctx = brief.get("chain_context") or {}
    if "error" not in chain_ctx and chain_ctx.get("pcr") is not None:
        pcr_line = f"⚖️ PCR {chain_ctx['pcr']}"
        if chain_ctx.get("max_pain_strike") is not None:
            pcr_line += f" | Max pain {chain_ctx['max_pain_strike']}"
        lines.append(pcr_line)

    event = brief.get("event_today")
    news_risk = (brief.get("news") or {}).get("risk") or {}
    news_elevated = news_risk.get("level") == "elevated"
    if event:
        lines.append(f"⚠️ Event flagged: {event} — expect elevated volatility/whipsaw")
    if news_elevated:
        cats = ", ".join(news_risk.get("categories_hit") or [])
        lines.append(f"⚠️ News risk: elevated ({cats})")
    if not event and not news_elevated:
        lines.append("✅ No event/news risk flagged today")

    return "\n".join(lines)


def _send_premarket_summary():
    """Best-effort -- if today's JSON brief doesn't exist yet (premarket.py
    hasn't been run, or hasn't finished), logs and moves on rather than
    blocking or erroring. In the intended automated flow
    (start_trading.ps1 runs premarket.py to completion BEFORE starting
    this process), the file is already there by the time this runs."""
    path = premarket.brief_json_path()
    if not path.exists():
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] no premarket brief found at {path.name}, skipping")
        return
    try:
        brief = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] premarket brief unreadable, skipping: {e}")
        return
    message = build_premarket_message(brief)
    if message is None:
        return
    try:
        telegram_notifier.send_message(message)
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] premarket summary sent")
    except Exception as e:
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] premarket summary send failed: {e}")


# --- bias-shift alerts ---------------------------------------------------------

def _load_bias_state() -> dict:
    if BIAS_STATE_PATH.exists():
        try:
            return json.loads(BIAS_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _check_bias_shift(label: str, index_key: str, bias: dict, bias_state: dict):
    """
    Returns an alert message if `bias`'s label differs from what was
    recorded at the LAST check for this index, else None. Always updates
    bias_state in place when a label is available -- including the first
    time an index is ever seen, which deliberately does NOT alert (no real
    "shift" from nothing, just no prior memory yet, e.g. right after a
    restart or on a fresh install).

    WHY "differs from the last CHECK", not "differs from the last ALERT":
    checked real recorded decision_log data (2026-08-16) and found NIFTY's
    bias flipping bearish -> neutral/range -> bearish twice within 90
    seconds, from PCR oscillating right at the bearish threshold. This
    runs on the same CHECK_INTERVAL_SECONDS cadence as position snapshots
    (default 15 min), not continuously, so most flicker like that is never
    even sampled -- and because each check only compares against the
    single immediately-prior check, a shift that reverses itself inside
    one interval produces at most one message, not an escalating stream.
    """
    current_label = (bias or {}).get("label")
    if not current_label:
        return None
    now_str = datetime.now().strftime("%H:%M")
    prev = bias_state.get(index_key)
    if prev is None:
        bias_state[index_key] = {"label": current_label, "since": now_str}
        return None
    if prev.get("label") == current_label:
        return None

    old_icon, new_icon = _bias_icon(prev.get("label")), _bias_icon(current_label)
    score = bias.get("score")
    score_note = f"  (score {score:+.2f})" if score is not None else ""
    reasons = bias.get("reasons") or []
    reasons_line = " · ".join(reasons) if reasons else "no strong signal"
    message = (
        f"\U0001f504 Bias Shift — {label} — {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"{old_icon} {prev.get('label')} → {new_icon} {current_label}{score_note}\n"
        f"{reasons_line}\n\n"
        f"(was {prev.get('label')} since {prev.get('since', '?')})"
    )
    bias_state[index_key] = {"label": current_label, "since": now_str}
    return message


def _check_and_alert_bias_shifts(state: dict):
    bias_state = _load_bias_state()
    checks = (
        ("NIFTY", "NIFTY", (state.get("latest_cycle") or {}).get("market_bias")),
        ("Bank Nifty", "BANKNIFTY", ((state.get("banknifty") or {}).get("latest_cycle") or {}).get("market_bias")),
    )
    for label, key, bias in checks:
        message = _check_bias_shift(label, key, bias, bias_state)
        if message:
            try:
                telegram_notifier.send_message(message)
                log.info(f"[{datetime.now().strftime('%H:%M:%S')}] bias shift alert sent for {label}")
            except Exception as e:
                log.info(f"[{datetime.now().strftime('%H:%M:%S')}] bias shift alert FAILED for {label}: {e}")
    atomic_write_json(BIAS_STATE_PATH, bias_state)


# --- lifecycle -------------------------------------------------------------------

def _send_lifecycle_message(text: str):
    """
    Best-effort start/stop notice -- logs success or failure either way, so
    the TERMINAL alone tells you whether Telegram delivery actually works,
    without needing to go check the chat itself. Sent unconditionally
    (unlike check_once()'s snapshots, which are market-hours-gated only):
    the whole point is to prove connectivity right now, market open or not.
    """
    try:
        telegram_notifier.send_message(text)
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] lifecycle message sent -- Telegram delivery is working.")
    except Exception as e:
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] lifecycle message FAILED -- "
                 f"Telegram delivery is NOT working, check TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID: {e}")


def check_once():
    if not market_is_open():
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] market closed, skipping")
        return
    state = dashboard_server.build_state()

    message = build_full_message(state)
    if message is None:
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] nothing to report, skipping")
    else:
        try:
            telegram_notifier.send_message(message)
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] snapshot sent")
        except Exception as e:
            log.info(f"[{datetime.now().strftime('%H:%M:%S')}] send failed, will retry next cycle: {e}")

    _check_and_alert_bias_shifts(state)


def run_forever(interval_seconds: int = CHECK_INTERVAL_SECONDS):
    """
    Sends a start message immediately (proves Telegram delivery works right
    now, whether or not the market's open), then today's pre-market summary
    if it's available yet, then a stop message on a clean Ctrl+C exit.
    NOTE on the stop message: it can only fire on a graceful stop where
    Python code actually gets to run -- Ctrl+C (KeyboardInterrupt)
    qualifies. A forceful kill does not: Windows' Stop-Process -Force (what
    automation/start_trading.ps1's own stop_trading.ps1 uses) calls
    TerminateProcess, which ends the process with zero code execution -- no
    exception, no signal handler, nothing to hook. If this is ever added to
    that automation, expect the start message but not a stop message when
    it's cycled that way.
    """
    log.info(f"position_notifier started -- checking every {interval_seconds}s during market hours.")
    _send_lifecycle_message(
        f"\U0001f7e2 position_notifier started — checking positions every {interval_seconds}s during "
        "market hours (stays silent when nothing is open). This message confirms Telegram "
        "delivery is working right now."
    )
    _send_premarket_summary()
    try:
        while True:
            check_once()
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log.info("position_notifier stopping (Ctrl+C).")
        _send_lifecycle_message(
            "\U0001f534 position_notifier stopped (Ctrl+C) — no more position snapshots until it's restarted."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL_SECONDS,
                        help=f"seconds between checks (default {CHECK_INTERVAL_SECONDS})")
    parser.add_argument("--once", action="store_true", help="single check, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.once:
        check_once()
    else:
        run_forever(args.interval)
