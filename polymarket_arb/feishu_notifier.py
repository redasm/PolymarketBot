"""飞书应用机器人通知模块."""

from __future__ import annotations

import json
import logging
import time
from html import unescape

import requests

from polymarket_arb.config import ArbConfig

LOG = logging.getLogger(__name__)


class FeishuNotifier:
    """通过飞书应用机器人 OpenAPI 发送消息."""

    def __init__(self, config: ArbConfig):
        self._app_id = config.feishu_app_id
        self._app_secret = config.feishu_app_secret
        self._open_id = config.feishu_open_id
        self._api_base = config.feishu_api_base.rstrip("/")
        self._cooldown_sec = config.notification_cooldown_sec
        self._last_sent: dict[str, float] = {}
        self._tenant_access_token = ""
        self._tenant_access_token_expire_at = 0.0

    def send(self, message: str, *, category: str = "general", force: bool = False) -> bool:
        if not self._app_id or not self._app_secret or not self._open_id:
            return False
        if not force and self._is_in_cooldown(category):
            LOG.debug("飞书消息 [%s] 在冷却期内，跳过", category)
            return False

        try:
            token = self._get_tenant_access_token()
        except requests.RequestException as exc:
            LOG.error("飞书获取 tenant_access_token 失败: %s", exc)
            return False

        payload = _build_message_request(message, category=category)
        sent = self._send_one(self._open_id, payload, token)
        if sent:
            self._last_sent[category] = time.time()
        return sent

    def _send_one(self, open_id: str, payload: dict, token: str) -> bool:
        url = f"{self._api_base}/im/v1/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        params = {"receive_id_type": "open_id"}
        body = {
            "receive_id": open_id,
            **payload,
        }
        try:
            resp = requests.post(url, params=params, json=body, headers=headers, timeout=10)
            body_json = resp.json() if getattr(resp, "content", b"") else {}
            if resp.status_code == 200 and int(body_json.get("code", 0)) == 0:
                return True
            LOG.warning(
                "飞书发送失败: status=%s code=%s msg=%s open_id=%s",
                resp.status_code,
                body_json.get("code"),
                body_json.get("msg"),
                open_id,
            )
            return False
        except (ValueError, requests.RequestException) as exc:
            LOG.error("飞书发送异常: open_id=%s error=%s", open_id, exc)
            return False

    def _get_tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_access_token and now < self._tenant_access_token_expire_at:
            return self._tenant_access_token

        url = f"{self._api_base}/auth/v3/tenant_access_token/internal"
        resp = requests.post(
            url,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if int(body.get("code", 0)) != 0:
            raise requests.RequestException(f"tenant_access_token error: {body}")

        token = str(body.get("tenant_access_token") or "")
        expire = float(body.get("expire", 0) or 0)
        if not token or expire <= 0:
            raise requests.RequestException(f"invalid tenant_access_token payload: {body}")

        self._tenant_access_token = token
        self._tenant_access_token_expire_at = now + max(60.0, expire - 60.0)
        return token

    def _is_in_cooldown(self, category: str) -> bool:
        last = self._last_sent.get(category, 0.0)
        return (time.time() - last) < self._cooldown_sec


def _build_message_request(message: str, *, category: str) -> dict:
    title, lines = _split_title_and_lines(message, category=category)
    content_rows = []
    for line in lines:
        text = _normalize_line(line)
        if not text:
            continue
        content_rows.append([{"tag": "text", "text": text}])
    if not content_rows:
        content_rows = [[{"tag": "text", "text": " "}]]

    content = {
        "zh_cn": {
            "title": title,
            "content": content_rows,
        }
    }
    return {
        "msg_type": "post",
        "content": json.dumps(content, ensure_ascii=False),
    }


def _split_title_and_lines(message: str, *, category: str) -> tuple[str, list[str]]:
    raw_lines = [line.strip() for line in str(message).splitlines()]
    non_empty = [line for line in raw_lines if line]
    default_title = _title_for_category(category)

    timestamp_line = f"监测时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"
    if not non_empty:
        return default_title, [timestamp_line]
    if len(non_empty) == 1:
        return default_title, non_empty + [timestamp_line]

    first = non_empty[0]
    if len(first) <= 32:
        return first, non_empty[1:] + [timestamp_line]
    return default_title, non_empty + [timestamp_line]


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
