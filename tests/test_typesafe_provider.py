"""TypeSafe Jev provider 行为测试（httpx.MockTransport，不发真实请求）."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from polymarket_arb.typesafe_provider import (
    AsyncTokenBucket,
    Choice,
    Noul,
    Score,
    TypeSafeConfig,
    TypeSafeConfigurationError,
    TypeSafeJevClient,
    TypeSafeRateLimitError,
    TypeSafeRequestError,
    parse_system_one_payload,
)

_OK_PAYLOAD = {
    "model": "jev-1.13.0",
    "answers": {
        "is_urgent": {"type": "noul", "noul": 0.92},
        "department": {
            "type": "choice",
            "choice": "technical",
            "probabilities": {"billing": 0.08, "technical": 0.85, "sales": 0.07},
            "confidence": 0.82,
        },
        "frustration": {
            "type": "score",
            "score": 1.6,
            "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
            "probabilities": {"0": 0.05, "1": 0.3, "2": 0.65},
            "confidence": 0.78,
        },
    },
    "usage": {"input_tokens": 312, "output_tokens": 48},
}

_QUESTIONS = {
    "is_urgent": Noul("Does this convey urgency?", criteria={"true": "time-sensitive", "false": "no urgency"}),
    "department": Choice("Which team?", criteria={"billing": None, "technical": None, "sales": None}),
    "frustration": Score("How frustrated?", criteria=("Calm", "Frustrated", "Very angry")),
}


def _config(**overrides) -> TypeSafeConfig:
    base = dict(api_key="k", model="jev-1.13.0", rps=1000.0, timeout_sec=5.0, max_retries=3)
    base.update(overrides)
    return TypeSafeConfig(**base)


def _client(handler, *, sink=None, **cfg) -> TypeSafeJevClient:
    async def _no_sleep(_: float) -> None:
        return None

    return TypeSafeJevClient(
        _config(**cfg),
        transport=httpx.MockTransport(handler),
        telemetry_sink=sink,
        sleep=_no_sleep,
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_config_from_env_requires_api_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(TypeSafeConfigurationError):
        TypeSafeConfig.from_env()


def test_config_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "abc")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-9.9.9")
    monkeypatch.setenv("TYPESAFE_RPS", "2.5")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://example.test/")
    cfg = TypeSafeConfig.from_env()
    assert cfg.model == "jev-9.9.9"
    assert cfg.rps == 2.5
    assert cfg.base_url == "https://example.test"


def test_config_defaults_pin_model(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "abc")
    for name in ("TYPESAFE_MODEL", "TYPESAFE_RPS", "TYPESAFE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    cfg = TypeSafeConfig.from_env()
    assert cfg.model == "jev-1.13.0"
    assert cfg.rps == 10.0


def test_choice_requires_two_options():
    with pytest.raises(TypeSafeConfigurationError):
        Choice("x", criteria={"only": None})


# ---------------------------------------------------------------------------
# 请求 / 响应
# ---------------------------------------------------------------------------


def test_system_one_sends_schema_and_parses_answers():
    captured: dict = {}
    sink_rows: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_OK_PAYLOAD, headers={"x-typesafe-request-id": "req-1"})

    client = _client(handler, sink=sink_rows.append)
    resp = _run(client.system_one({"text": "I was charged twice"}, _QUESTIONS, tag="cid-1"))
    _run(client.aclose())

    assert captured["url"].endswith("/v1/systemone")
    assert captured["auth"] == "Bearer k"
    body = captured["body"]
    assert body["model"] == "jev-1.13.0"
    assert body["state"] == {"text": "I was charged twice"}
    assert body["questions"]["is_urgent"] == {
        "type": "noul",
        "instructions": "Does this convey urgency?",
        "criteria": {"true": "time-sensitive", "false": "no urgency"},
    }
    assert body["questions"]["department"]["type"] == "choice"
    assert body["questions"]["frustration"]["criteria"] == ["Calm", "Frustrated", "Very angry"]

    assert resp.nouls["is_urgent"].noul == pytest.approx(0.92)
    assert resp.choices["department"].choice == "technical"
    assert resp.choices["department"].confidence == pytest.approx(0.82)
    assert resp.scores["frustration"].score == pytest.approx(1.6)
    assert resp.input_tokens == 312 and resp.output_tokens == 48
    assert resp.request_id == "req-1"
    assert resp.attempts == 1
    assert resp.cost_usd == pytest.approx(312 * 0.042 / 1_000_000)

    stats = client.stats.to_dict()
    assert stats["requests"] == 1 and stats["successes"] == 1 and stats["failures"] == 0
    assert stats["input_tokens"] == 312
    assert len(sink_rows) == 1
    assert sink_rows[0]["kind"] == "typesafe_request"
    assert sink_rows[0]["ok"] is True
    assert sink_rows[0]["tag"] == "cid-1"
    assert sink_rows[0]["question_count"] == 3


def test_retries_on_429_then_succeeds_and_counts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "rate limited"}, headers={"retry-after": "0"})
        return httpx.Response(200, json=_OK_PAYLOAD)

    client = _client(handler, max_retries=3)
    resp = _run(client.system_one("s", {"is_urgent": Noul("q"), "department": _QUESTIONS["department"], "frustration": _QUESTIONS["frustration"]}))
    _run(client.aclose())

    assert resp.attempts == 3
    assert client.stats.rate_limited_429 == 2
    assert client.stats.retries == 2
    assert client.stats.successes == 1
    assert client.stats.failures == 0


def test_rate_limit_exhausted_raises_rate_limit_error_and_emits_failure():
    sink_rows: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": "overloaded"})

    client = _client(handler, max_retries=2, sink=sink_rows.append)
    with pytest.raises(TypeSafeRateLimitError) as exc:
        _run(client.system_one("s", {"q": Noul("q")}))
    _run(client.aclose())

    assert exc.value.status == 529
    assert client.stats.overloaded_529 == 3
    assert client.stats.failures == 1
    assert client.stats.successes == 0
    assert sink_rows[-1]["ok"] is False
    assert sink_rows[-1]["status"] == 529
    assert sink_rows[-1]["attempts"] == 3


def test_non_retryable_4xx_fails_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"detail": "bad question"})

    client = _client(handler, max_retries=3)
    with pytest.raises(TypeSafeRequestError) as exc:
        _run(client.system_one("s", {"q": Noul("q")}))
    _run(client.aclose())

    assert calls["n"] == 1
    assert exc.value.status == 422
    assert "bad question" in str(exc.value)
    assert client.stats.retries == 0


def test_transport_error_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    client = _client(handler, max_retries=1)
    with pytest.raises(TypeSafeRequestError) as exc:
        _run(client.system_one("s", {"q": Noul("q")}))
    _run(client.aclose())

    assert calls["n"] == 2
    assert exc.value.status is None
    assert client.stats.transport_errors == 2


def test_missing_question_in_answers_is_request_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"other": {"type": "noul", "noul": 0.5}}, "usage": {}})

    client = _client(handler)
    with pytest.raises(TypeSafeRequestError, match="缺少 question"):
        _run(client.system_one("s", {"q": Noul("q")}))
    _run(client.aclose())
    assert client.stats.failures == 1


def test_model_drift_logs_warning_once(caplog):
    payload = dict(_OK_PAYLOAD, model="jev-1.14.0")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client(handler)
    with caplog.at_level("WARNING", logger="polymarket_arb.typesafe_provider"):
        _run(client.system_one("s", _QUESTIONS))
        _run(client.system_one("s", _QUESTIONS))
    _run(client.aclose())
    drift = [r for r in caplog.records if "pin=" in r.getMessage()]
    assert len(drift) == 1


def test_empty_questions_or_state_rejected():
    client = _client(lambda r: httpx.Response(200, json=_OK_PAYLOAD))
    with pytest.raises(TypeSafeConfigurationError):
        _run(client.system_one("s", {}))
    with pytest.raises(TypeSafeConfigurationError):
        _run(client.system_one("", {"q": Noul("q")}))
    _run(client.aclose())


def test_parse_payload_clamps_and_validates():
    parsed = parse_system_one_payload(
        {"model": "m", "answers": {"a": {"type": "noul", "noul": 1.4}}, "usage": {"input_tokens": "7"}},
        expected_questions={"a"},
    )
    assert parsed.nouls["a"].noul == 1.0
    assert parsed.input_tokens == 7
    with pytest.raises(TypeSafeRequestError):
        parse_system_one_payload({"answers": {"a": {"type": "choice", "choice": "x"}}})
    with pytest.raises(TypeSafeRequestError):
        parse_system_one_payload({"answers": {"a": {"type": "mystery"}}})
    with pytest.raises(TypeSafeRequestError):
        parse_system_one_payload({"nope": 1})


# ---------------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------------


def test_token_bucket_paces_requests():
    clock = {"t": 0.0}
    waits: list[float] = []

    async def _fake_sleep(sec: float) -> None:
        waits.append(sec)
        clock["t"] += sec

    async def _scenario():
        bucket = AsyncTokenBucket(rate=2.0, capacity=2.0, clock=lambda: clock["t"], sleep=_fake_sleep)
        assert await bucket.acquire() == 0.0
        assert await bucket.acquire() == 0.0
        third = await bucket.acquire()  # 桶空，需等 0.5s
        assert third == pytest.approx(0.5)
        clock["t"] += 10.0  # 长时间空闲后重新装满，但不超过容量
        assert await bucket.acquire() == 0.0
        assert await bucket.acquire() == 0.0
        assert (await bucket.acquire()) > 0.0
        assert bucket.total_wait_sec == pytest.approx(sum(waits))

    _run(_scenario())
    assert len(waits) == 2


def test_token_bucket_rejects_non_positive_rate():
    with pytest.raises(TypeSafeConfigurationError):
        AsyncTokenBucket(rate=0.0)
