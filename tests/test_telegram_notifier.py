"""
telegram_notifier.py -- minimal Telegram Bot API client, added 2026-08-16
for position_notifier.py's live-position snapshots.

Run: python -m pytest tests/test_telegram_notifier.py -q
"""
import pytest

import telegram_notifier as tn


def test_credentials_raises_when_token_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    with pytest.raises(RuntimeError):
        tn._credentials()


def test_credentials_raises_when_chat_id_missing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError):
        tn._credentials()


def test_credentials_returns_both_when_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert tn._credentials() == ("abc:def", "123")


def test_send_message_posts_to_the_right_url_with_chat_and_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(tn.requests, "post", fake_post)

    result = tn.send_message("hello world")

    assert captured["url"] == "https://api.telegram.org/botabc:def/sendMessage"
    assert captured["json"]["chat_id"] == "123"
    assert captured["json"]["text"] == "hello world"
    assert captured["json"]["disable_web_page_preview"] is True
    assert "parse_mode" not in captured["json"]
    assert result == {"ok": True}


def test_send_message_includes_parse_mode_when_given(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(tn.requests, "post",
                        lambda url, json=None, timeout=None: (captured.update(json=json), FakeResp())[1])

    tn.send_message("hi", parse_mode="HTML")

    assert captured["json"]["parse_mode"] == "HTML"


def test_send_message_raises_on_http_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    class FakeResp:
        def raise_for_status(self):
            raise tn.requests.HTTPError("401 Unauthorized")

    monkeypatch.setattr(tn.requests, "post", lambda *a, **kw: FakeResp())

    with pytest.raises(tn.requests.HTTPError):
        tn.send_message("hello")


def test_send_message_requires_credentials_before_any_network_call(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def _boom(*a, **kw):
        raise AssertionError("should not reach the network without credentials")
    monkeypatch.setattr(tn.requests, "post", _boom)

    with pytest.raises(RuntimeError):
        tn.send_message("hello")
