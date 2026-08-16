"""
Minimal Telegram Bot API client -- posts a plain-text message to one chat.

SETUP (one-time, on your own Telegram account)
------------------------------------------------
1. Create a bot: message @BotFather on Telegram, send /newbot, follow the
   prompts. It gives you a token that looks like
   "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ" -- that's TELEGRAM_BOT_TOKEN.
2. Get your chat id: send your new bot ANY message first (it can't message
   you until you've messaged it at least once), then open this URL in a
   browser, with your own token in place of <TOKEN>:
       https://api.telegram.org/bot<TOKEN>/getUpdates
   Find "chat":{"id": ...} in the response -- that number is
   TELEGRAM_CHAT_ID. (Or message @userinfobot to get your own numeric id
   directly, if you'd rather DM yourself than set up a group.)
3. Set both as environment variables, same pattern as DHAN_ACCESS_TOKEN:
       set TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
       set TELEGRAM_CHAT_ID=987654321

NEVER PLACES ORDERS, same as every other file in this project -- this
sends a text message to Telegram's API and does nothing else. It cannot
read replies, accept commands, or act on anything sent back to the bot.
"""

import os

import requests

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 15


def _credentials() -> tuple:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables first "
            "-- see this module's own docstring for how to get them."
        )
    return token, chat_id


def send_message(text: str, parse_mode: str = None) -> dict:
    """
    Posts `text` to the configured chat. Raises on failure (missing
    credentials, a network error, Telegram rejecting the request) rather
    than swallowing it -- the caller decides whether that's fatal or just
    worth logging and retrying next cycle; silently eating a failed send
    here would mean believing you're being notified when you are not.
    """
    token, chat_id = _credentials()
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    resp = requests.post(
        f"{API_BASE}/bot{token}/sendMessage", json=payload, timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()
