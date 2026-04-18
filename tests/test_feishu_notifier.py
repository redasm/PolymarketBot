from __future__ import annotations

import base64
import hashlib
import hmac

from polymarket_arb.feishu_notifier import FeishuNotifier
from tests.conftest import make_test_config


def test_feishu_notifier_sends_post_payload(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"
        content = b'{"code":0,"msg":"ok"}'

        def json(self):
            return {"code": 0, "msg": "ok"}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("polymarket_arb.feishu_notifier.requests.post", fake_post)

    notifier = FeishuNotifier(
        make_test_config(
            notification_provider="feishu",
            feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        )
    )

    assert notifier.send("✅ 成交成功\n事件: BTC", category="trade_success", force=True) is True
    assert captured["url"].endswith("/abc")
    assert captured["json"]["msg_type"] == "post"
    zh_cn = captured["json"]["content"]["post"]["zh_cn"]
    assert zh_cn["title"] == "✅ 成交成功"
    assert zh_cn["content"][0][0]["text"] == "事件: BTC"


def test_feishu_notifier_adds_signature_when_secret_configured(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"
        content = b'{"code":0,"msg":"ok"}'

        def json(self):
            return {"code": 0, "msg": "ok"}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("polymarket_arb.feishu_notifier.requests.post", fake_post)
    monkeypatch.setattr("polymarket_arb.feishu_notifier.time.time", lambda: 1710000000.0)

    secret = "sign-secret"
    notifier = FeishuNotifier(
        make_test_config(
            notification_provider="feishu",
            feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            feishu_sign_secret=secret,
        )
    )

    assert notifier.send("🚨 严重错误\n详情: network down", category="fatal_error:network", force=True) is True
    assert captured["json"]["timestamp"] == "1710000000"

    expected = base64.b64encode(
        hmac.new(
            b"1710000000\nsign-secret",
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    assert captured["json"]["sign"] == expected
