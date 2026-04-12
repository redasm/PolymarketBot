"""Telegram notifier tests."""

from __future__ import annotations

import requests

from polymarket_arb.telegram_notifier import TelegramNotifier
from tests.conftest import make_test_config


def test_telegram_notifier_respects_cooldown(monkeypatch):
    sent = {"count": 0}

    class _Resp:
        status_code = 200
        text = "ok"

    def fake_post(*args, **kwargs):
        sent["count"] += 1
        return _Resp()

    monkeypatch.setattr("polymarket_arb.telegram_notifier.requests.post", fake_post)

    notifier = TelegramNotifier(
        make_test_config(
            telegram_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_cooldown_sec=60.0,
        )
    )

    assert notifier.send("hello", category="status") is True
    assert notifier.send("again", category="status") is False
    assert sent["count"] == 1


def test_telegram_notifier_handles_request_exception(monkeypatch):
    def fake_post(*args, **kwargs):
        raise requests.RequestException("network down")

    monkeypatch.setattr("polymarket_arb.telegram_notifier.requests.post", fake_post)

    notifier = TelegramNotifier(
        make_test_config(
            telegram_enabled=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
        )
    )

    assert notifier.send("hello", category="status") is False
