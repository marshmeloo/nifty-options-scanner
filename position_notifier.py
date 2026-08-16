"""
Posts a snapshot of every currently open position (both indices, every
strategy) to Telegram on a fixed interval during market hours -- built
2026-08-16 so open positions are visible away from the screen, not just
on the live dashboard.

WHAT COUNTS AS "OPEN"
----------------------
Reuses dashboard_server.build_state() -- the exact same function that
feeds the live dashboard -- so this is never a second, possibly-drifted
implementation of "what's open." Covers, per index (NIFTY and Bank
Nifty):
  - Momentum (Anchor) open trades
  - Iron condor position (if status == OPEN)
  - Directional spread position (if status == OPEN)
  - Price-action open trades

NOT covered yet: Sentinel's own positions (main_live_sentinel.py /
main_live_banknifty_sentinel.py). The live dashboard itself doesn't
surface those either (only the /pnl historical dashboard does), so this
matches what checking the live dashboard right now would show you.

ONLY SENDS WHEN SOMETHING IS ACTUALLY OPEN
--------------------------------------------
A ping every 15 minutes on a flat day would just be noise you'd start
ignoring, and defeats the point of a notification -- something you can
trust means "look at this." If nothing is open, this stays silent.

SETUP
-----
Needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID -- see telegram_notifier.py's
own docstring for how to get them. Not yet wired into
automation/start_trading.ps1 -- run it by hand first and confirm a real
message actually lands in your chat before adding it there.

Run:
    python3 position_notifier.py                  # every 15 min, market hours only
    python3 position_notifier.py --interval 300    # more frequent, useful while testing
    python3 position_notifier.py --once             # single check, then exit (good for a manual test)
"""

import argparse
import logging
import time
from datetime import datetime, time as dtime

import dashboard_server
import telegram_notifier

log = logging.getLogger("position_notifier")

MARKET_OPEN = dtime(9, 15)
# A little past the nominal 15:30 close: options keep moving until close
# to 15:40 under SEBI's Closing Auction Session (see main_condor.py's
# EXPIRY_SETTLEMENT_CUTOFF and BACKLOG.md's CAS entry) -- a position can
# still be genuinely live in that window, worth one more check rather
# than going quiet right at 15:30.
MARKET_CLOSE = dtime(15, 40)

CHECK_INTERVAL_SECONDS = 900   # 15 minutes


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


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
            f"  {t['strike']:.0f}{t['option_type']}  entry {t['entry']} -> now {t.get('current_ltp', '?')}  "
            f"{pnl_pct:+.1f}% (Rs {pnl:+,.0f}){r_note}"
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
    mtm_note = f"Rs {mtm:+,.0f}" if mtm is not None else "MTM unavailable"
    line = (f"  Condor: short {plan.get('short_ce_strike')}CE/{plan.get('short_pe_strike')}PE, "
            f"hedge {plan.get('hedge_ce_strike')}CE/{plan.get('hedge_pe_strike')}PE  {mtm_note}")
    return line, (mtm or 0)


def _spread_line(position: dict) -> tuple:
    if not position or position.get("status") != "OPEN":
        return None, 0
    plan = position.get("plan") or {}
    mtm = position.get("current_mtm_pnl_inr")
    mtm_note = f"Rs {mtm:+,.0f}" if mtm is not None else "MTM unavailable"
    direction = "Bull put" if plan.get("direction") == "PE" else "Bear call"
    line = f"  Directional spread: {direction} {plan.get('short_strike')}/{plan.get('hedge_strike')}  {mtm_note}"
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
    return [label] + body, subtotal


def build_snapshot_message(state: dict = None):
    """Returns the message text, or None if nothing is currently open."""
    state = state if state is not None else dashboard_server.build_state()
    bn = state.get("banknifty") or {}

    nifty_lines, nifty_pnl = _index_section(
        "NIFTY", state.get("open_trades") or [], state.get("condor_position"),
        state.get("directional_spread_position"), state.get("price_action_trades") or [],
    )
    bn_lines, bn_pnl = _index_section(
        "Bank Nifty", bn.get("open_trades") or [], bn.get("condor_position"),
        bn.get("directional_spread_position"), bn.get("price_action_trades") or [],
    )

    sections = nifty_lines + bn_lines
    if not sections:
        return None

    # Summed from every line actually shown above (momentum + condor MTM +
    # spread MTM + price action, both indices) -- NOT state["totals"],
    # which only ever covers momentum's own open_trades and would silently
    # under-report whenever condor/spread/price-action also have something
    # open (caught by testing this against real dev data: condor and
    # spread positions were open with real non-zero MTM, but totals showed
    # Rs 0 since no momentum trade happened to be open at the same time).
    total_pnl = nifty_pnl + bn_pnl
    header = f"Live positions -- {datetime.now().strftime('%H:%M:%S')}"
    footer = f"Total open P&L: Rs {total_pnl:+,.0f}"
    return "\n".join([header, ""] + sections + ["", footer])


def check_once():
    if not market_is_open():
        return
    message = build_snapshot_message()
    if message is None:
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] nothing open, skipping")
        return
    try:
        telegram_notifier.send_message(message)
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] snapshot sent")
    except Exception as e:
        log.info(f"[{datetime.now().strftime('%H:%M:%S')}] send failed, will retry next cycle: {e}")


def run_forever(interval_seconds: int = CHECK_INTERVAL_SECONDS):
    log.info(f"position_notifier started -- checking every {interval_seconds}s during market hours.")
    while True:
        check_once()
        time.sleep(interval_seconds)


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
