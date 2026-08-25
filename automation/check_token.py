"""
Morning token-validity check. Run once, at 09:00 IST, by the "Morning
Token Check" scheduled task (wakes the PC -- see
fix_scheduler_and_add_recorder.ps1). NEVER PLACES ORDERS: the only Dhan
call this makes is a single-instrument LTP read, and the only other
thing it does is send a Telegram message.

WHY THIS EXISTS
----------------
automation/start_trading.ps1 already refuses to start anything if
DHAN_ACCESS_TOKEN/DHAN_CLIENT_ID are unset -- but "unset" and "set to
yesterday's dead token" are different failure modes, and the second one
is silent: every process starts fine, then every live API call 401s.
That exact class of bug has now hit this project three times (the
stock-spread recorder twice, in the middle of live measurement). For
the LIVE trading system this is worse: a stale token means Anchor and
Sentinel sit there doing nothing all session, with no crash and no
obvious signal unless someone reads the logs.

This checks the token BEFORE market open and pages Telegram if it is
still bad, repeating every 5 minutes until either it starts working
(you ran update_token.ps1) or the check window ends -- so there is a
20-minute runway to fix it before Trading Start's 09:13 trigger and
before the market itself opens at 09:15, instead of finding out at
09:41 like on 2026-08-20.

Telegram, not a phone call: this project's telegram_notifier.py can
only POST a text message via the Bot API -- there is no integration
here that can place an actual phone call (that would need a separate
service like Twilio). Repeating the message every 5 minutes until
resolved is the practical stand-in.

Run: python automation/check_token.py
"""

import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import dhan_source
import telegram_notifier
from research.stock_spread_recorder import refresh_token_from_registry

CHECK_INTERVAL_SECONDS = 5 * 60
STOP_BY = "09:25"  # give up (and stop paging) after this -- Trading Start
                    # already fired at 09:13 and the market opened at 09:15;
                    # by 09:25 the token is either fixed or this is no
                    # longer a "you still have time" situation.


def token_is_valid() -> bool:
    """One cheap probe: NIFTY spot LTP. Any successful response means the
    token authenticated; a 401 means it didn't. Anything else (network
    blip, Dhan's own 5xx) is treated as NOT a token problem -- don't page
    for something this check can't diagnose."""
    try:
        prices = dhan_source.get_ltp_batch([dhan_source.NIFTY_UNDERLYING_SCRIP], exchange_segment="IDX_I")
        return dhan_source.NIFTY_UNDERLYING_SCRIP in prices
    except Exception as e:
        if "401" in str(e):
            return False
        print(f"  probe failed for a non-token reason, not paging: {type(e).__name__}: {e}")
        return True  # unknown -- don't cry wolf


def main():
    stop_hh, stop_mm = (int(x) for x in STOP_BY.split(":"))
    stop_at = datetime.now().replace(hour=stop_hh, minute=stop_mm, second=0, microsecond=0)

    while True:
        refresh_token_from_registry()
        now = datetime.now()
        if token_is_valid():
            print(f"{now:%H:%M:%S}  token OK")
            return
        print(f"{now:%H:%M:%S}  token INVALID (401) -- paging Telegram")
        try:
            telegram_notifier.send_message(
                f"⚠️ Dhan token expired/invalid as of {now:%H:%M}. "
                f"Live trading has NOT authenticated. Run update_token.ps1 now.\n"
                f"Will keep checking every 5 min until {STOP_BY} or until fixed."
            )
        except Exception as e:
            print(f"  (Telegram send also failed: {e})")

        if now >= stop_at:
            print(f"reached {STOP_BY} cutoff, stopping checks -- token still bad")
            return
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
