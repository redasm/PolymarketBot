"""飞书机器人通知模块."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from html import unescape

import requests

from polymarket_arb.config import ArbConfig

LOG = logging.getLogger(__name__)


class FeishuNotifier:
    """通过飞书自定义机器人 webhook 发送消息."""

    def __init__(self, config: ArbConfig):
        self._enabled = bool(config.feishu_webhook_url)
        self._webhook_url = config.feishu_webhook_url
        self._sign_secret = config.feishu_sign_secret
        self._cooldown_sec = config.telegram_cooldown_sec
        self._last_sent: dict[str, float] = {}

    def send(self, message: str, *, category: str = "general", force: bool = False) -> bool:
        if not self._enabled:
            return False
        if not self._webhook_url:
            LOG.warning("飞书机器人未配置 webhook_url")
            return False
        if not force and self._is_in_cooldown(category):
            LOG.debug("飞书消息 [%s] 在冷却期内，跳过", category)
            return False

        payload = _build_post_payload(message, category=category)
        if self._sign_secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _build_sign(timestamp, self._sign_secret)

        try:
            resp = requests.post(self._webhook_url, json=payload, timeout=10)
            if resp.status_code == 200:
                body = resp.json() if getattr(resp, "content", b"") else {}
                if int(body.get("code", 0)) == 0:
                    self._last_sent[category] = time.time()
                    return True
                LOG.warning("飞书发送失败: code=%s msg=%s", body.get("code"), body.get("msg"))
                return False
            LOG.warning("飞书发送失败: HTTP %d - %s", resp.status_code, resp.text[:200])
            return False
        except (ValueError, requests.RequestException) as exc:
            LOG.error("飞书发送异常: %s", exc)
            return False

    def _is_in_cooldown(self, category: str) -> bool:
        last = self._last_sent.get(category, 0.0)
        return (time.time() - last) < self._cooldown_sec


def _build_sign(timestamp: str, secret: str) -> str:
    # Feishu spec: key = timestamp + "\n" + secret, message = empty
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _build_post_payload(message: str, *, category: str) -> dict:
    title, lines = _split_title_and_lines(message, category=category)
    content_rows = []
    for line in lines:
        text = _normalize_line(line)
        if not text:
            continue
        content_rows.append([{"tag": "text", "text": text}])

    if not content_rows:
        content_rows = [[{"tag": "text", "text": " "}]]

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content_rows,
                }
            }
        },
    }


def _split_title_and_lines(message: str, *, category: str) -> tuple[str, list[str]]:
    raw_lines = [line.strip() for line in str(message).splitlines()]
    non_empty = [line for line in raw_lines if line]
    default_title = _title_for_category(category)

    if not non_empty:
        return default_title, []

    if len(non_empty) == 1:
        return default_title, non_empty

    first = non_empty[0]
    if len(first) <= 32:
        return first, non_empty[1:] + [f"监测时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"]
    return default_title, non_empty + [f"监测时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"]


def _title_for_category(category: str) -> str:
    mapping = {
        "startup": "机器人启动",
        "shutdown": "机器人停止",
        "trade_success": "成交成功",
        "trade_failure": "成交失败",
        "pnl_profit": "盈利提醒",
        "pnl_loss": "亏损提醒",
        "daily_summary": "每日汇总",
    }
    if category.startswith("fatal_error"):
        return "严重错误"
    return mapping.get(category, "机器人通知")


def _normalize_line(line: str) -> str:
    return unescape(str(line).strip())
