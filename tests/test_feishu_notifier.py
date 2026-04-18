from __future__ import annotations

import json

from polymarket_arb.feishu_notifier import FeishuNotifier
from tests.conftest import make_test_config


def test_feishu_notifier_fetches_token_and_sends_post_message(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, *, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {"code": 0, "msg": "ok"}
            self.text = json.dumps(self._payload, ensure_ascii=False)
            self.content = self.text.encode("utf-8")

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return _Resp(payload={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        if url.endswith("/im/v1/messages"):
            return _Resp(payload={"code": 0, "data": {"message_id": "om_xxx"}})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("polymarket_arb.feishu_notifier.requests.post", fake_post)

    notifier = FeishuNotifier(
        make_test_config(
            feishu_app_id="cli_xxx",
            feishu_app_secret="secret_xxx",
            feishu_open_id="ou_xxx_123",
            feishu_api_base="https://open.feishu.cn/open-apis",
        )
    )

    assert notifier.send("✅ 成交成功\n事件: BTC", category="trade_success", force=True) is True
    assert len(calls) == 2

    token_call = calls[0]
    assert token_call[0].endswith("/auth/v3/tenant_access_token/internal")
    assert token_call[1]["json"]["app_id"] == "cli_xxx"
    assert token_call[1]["json"]["app_secret"] == "secret_xxx"

    message_call = calls[1]
    assert message_call[0].endswith("/im/v1/messages")
    assert message_call[1]["params"]["receive_id_type"] == "open_id"
    assert message_call[1]["headers"]["Authorization"] == "Bearer tenant-token"
    assert message_call[1]["json"]["receive_id"] == "ou_xxx_123"
    assert message_call[1]["json"]["msg_type"] == "post"

    content = json.loads(message_call[1]["json"]["content"])
    zh_cn = content["zh_cn"]
    assert zh_cn["title"] == "✅ 成交成功"
    assert zh_cn["content"][0][0]["text"] == "事件: BTC"


def test_feishu_notifier_reuses_cached_token(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(self._payload, ensure_ascii=False)
            self.content = self.text.encode("utf-8")

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    current_time = {"value": 1_710_000_000.0}

    def fake_time():
        return current_time["value"]

    def fake_post(url, **kwargs):
        calls.append(url)
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return _Resp({"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        if url.endswith("/im/v1/messages"):
            return _Resp({"code": 0, "data": {"message_id": "om_xxx"}})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("polymarket_arb.feishu_notifier.time.time", fake_time)
    monkeypatch.setattr("polymarket_arb.feishu_notifier.requests.post", fake_post)

    notifier = FeishuNotifier(
        make_test_config(
            feishu_app_id="cli_xxx",
            feishu_app_secret="secret_xxx",
            feishu_open_id="ou_xxx_123",
        )
    )

    assert notifier.send("hello 1", category="c1", force=True) is True
    current_time["value"] += 30
    assert notifier.send("hello 2", category="c2", force=True) is True

    assert calls.count("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal") == 1
    assert calls.count("https://open.feishu.cn/open-apis/im/v1/messages") == 2
