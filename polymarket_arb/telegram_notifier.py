"""Telegram 通知模块：套利发现、交易执行、错误告警等通知."""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from polymarket_arb.config import ArbConfig

LOG = logging.getLogger(__name__)


class TelegramNotifier:
    """Telegram Bot 消息推送."""

    def __init__(self, config: ArbConfig):
        self._enabled = config.telegram_enabled
        self._token = config.telegram_bot_token
        self._chat_id = config.telegram_chat_id
        self._notify_arb = config.notify_on_arb_found
        self._notify_trade = config.notify_on_trade
        self._notify_error = config.notify_on_error
        self._cooldown_sec = config.telegram_cooldown_sec
        self._last_sent: dict[str, float] = {}

    def send(self, message: str, *, category: str = "general", force: bool = False) -> bool:
        """发送 Telegram 消息.

        Args:
            message: 消息文本
            category: 消息类别（用于冷却去重）
            force: 是否跳过冷却期
        """
        if not self._enabled:
            return False

        if not self._token or not self._chat_id:
            LOG.warning("Telegram 未配置 bot_token 或 chat_id")
            return False

        if not force and self._is_in_cooldown(category):
            LOG.debug("Telegram 消息 [%s] 在冷却期内，跳过", category)
            return False

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        correlation_id = f"tg-{category}-{int(time.time() * 1000)}"

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                self._last_sent[category] = time.time()
                return True
            LOG.warning("[cid=%s] Telegram 发送失败: HTTP %d - %s", correlation_id, resp.status_code, resp.text[:200])
            if "migrate_to_chat_id" in resp.text:
                LOG.error("群组已升级为超级群，请更新 TELEGRAM_CHAT_ID")
            return False
        except requests.RequestException as e:
            LOG.error("[cid=%s] Telegram 发送异常: %s", correlation_id, e)
            return False

    def notify_arb_found(self, message: str) -> bool:
        if not self._notify_arb:
            return False
        return self.send(message, category="arb_found")

    def notify_trade(self, message: str) -> bool:
        if not self._notify_trade:
            return False
        return self.send(message, category="trade", force=True)

    def notify_error(self, message: str) -> bool:
        if not self._notify_error:
            return False
        return self.send(f"⚠️ 错误告警\n{message}", category="error")

    def notify_status(self, message: str) -> bool:
        return self.send(message, category="status")

    def _is_in_cooldown(self, category: str) -> bool:
        last = self._last_sent.get(category, 0.0)
        return (time.time() - last) < self._cooldown_sec
