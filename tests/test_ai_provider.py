"""AI provider behavior tests."""

from __future__ import annotations

import types
import sys
import asyncio

import pytest

from polymarket_arb.ai_provider import (
    AnthropicProvider,
    LLMConfigurationError,
    LLMRequestError,
    OpenAIProvider,
    OllamaProvider,
    create_provider,
)
from tests.conftest import make_test_config


def test_anthropic_provider_json_mode_adds_instruction(monkeypatch):
    captured = {}

    class _Resp:
        def __init__(self):
            self.content = [types.SimpleNamespace(type="text", text='{"ok": true}')]
            self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)

    class _Client:
        def __init__(self, api_key):
            self.messages = types.SimpleNamespace(create=self.create)

        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    fake_module = types.ModuleType("anthropic")
    fake_module.AsyncAnthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    provider = AnthropicProvider(api_key="x", model="claude-test")
    response = asyncio.run(
        provider.chat(
            [{"role": "system", "content": "base system"}, {"role": "user", "content": "hi"}],
            json_mode=True,
        )
    )

    assert response.content == '{"ok": true}'
    assert captured["max_tokens"] == 4096
    assert "Return valid JSON only" in captured["system"]


def test_anthropic_provider_sanitizes_system_prompt(monkeypatch):
    captured = {}

    class _Resp:
        def __init__(self):
            self.content = [types.SimpleNamespace(type="text", text='{"ok": true}')]
            self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)

    class _Client:
        def __init__(self, api_key):
            self.messages = types.SimpleNamespace(create=self.create)

        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    fake_module = types.ModuleType("anthropic")
    fake_module.AsyncAnthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    provider = AnthropicProvider(api_key="x", model="claude-test")
    asyncio.run(
        provider.chat(
            [
                {"role": "system", "content": "base system\x00\r\n\r\n  with noise\t"},
                {"role": "system", "content": "\x07extra block"},
                {"role": "user", "content": "hi"},
            ],
            json_mode=True,
        )
    )

    assert "\x00" not in captured["system"]
    assert "\x07" not in captured["system"]
    assert "\r" not in captured["system"]
    assert "base system" in captured["system"]
    assert "extra block" in captured["system"]
    assert "Return valid JSON only" in captured["system"]


def test_create_provider_raises_friendly_error_when_api_key_missing():
    config = make_test_config(ai_provider="openai", ai_api_key="", ai_api_base="", ai_model="gpt-4o")

    with pytest.raises(LLMConfigurationError, match="provider=openai"):
        create_provider(config)


def test_ollama_provider_wraps_http_status_errors():
    class _HTTPStatusError(Exception):
        def __init__(self, response):
            super().__init__("status error")
            self.response = response

    class _Resp:
        status_code = 503
        text = "model not ready"

        def raise_for_status(self):
            raise _HTTPStatusError(self)

    class _Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            return _Resp()

    class _HttpxModule:
        HTTPStatusError = _HTTPStatusError
        RequestError = RuntimeError
        AsyncClient = _Client

    provider = OllamaProvider(api_base="http://localhost:11434", model="qwen-test")
    provider._httpx = _HttpxModule

    with pytest.raises(LLMRequestError, match="status=503"):
        asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))


def test_ollama_provider_retries_request_timeout_then_succeeds():
    attempts = {"count": 0}

    class _RequestError(Exception):
        pass

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "message": {"content": "ok"},
                "prompt_eval_count": 11,
                "eval_count": 7,
            }

    class _Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise _RequestError("timeout")
            return _Resp()

    class _HttpxModule:
        RequestError = _RequestError
        HTTPStatusError = RuntimeError
        AsyncClient = _Client

    provider = OllamaProvider(api_base="http://localhost:11434", model="qwen-test", max_retries=1, retry_delay_sec=0.0)
    provider._httpx = _HttpxModule

    response = asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))

    assert response.content == "ok"
    assert attempts["count"] == 2


def test_ollama_provider_raises_after_retry_exhausted():
    attempts = {"count": 0}

    class _RequestError(Exception):
        pass

    class _Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            attempts["count"] += 1
            raise _RequestError("timeout")

    class _HttpxModule:
        RequestError = _RequestError
        HTTPStatusError = RuntimeError
        AsyncClient = _Client

    provider = OllamaProvider(api_base="http://localhost:11434", model="qwen-test", max_retries=1, retry_delay_sec=0.0)
    provider._httpx = _HttpxModule

    with pytest.raises(LLMRequestError, match="Ollama 请求失败"):
        asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))

    assert attempts["count"] == 2


def test_openai_provider_accepts_stringified_json_response(monkeypatch):
    class _Completions:
        async def create(self, **kwargs):
            return '{"choices":[{"message":{"content":"hello"}}],"usage":{"prompt_tokens":3,"completion_tokens":2},"model":"compat-model"}'

    class _Client:
        def __init__(self, **kwargs):
            self.chat = types.SimpleNamespace(completions=_Completions())

    fake_module = types.ModuleType("openai")
    fake_module.AsyncOpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    provider = OpenAIProvider(api_key="x", api_base="https://example.com", model="compat-model")
    response = asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))

    assert response.content == "hello"
    assert response.input_tokens == 3
    assert response.output_tokens == 2
