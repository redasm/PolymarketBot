"""LLM Provider 抽象层：统一接口支持多 provider 自由切换.

通过 AI_PROVIDER 环境变量选择 provider，通过 AI_API_BASE 支持自定义端点。
DeepSeek / Gemini / Together 等 OpenAI 兼容 API 直接复用 OpenAIProvider。

按需导入：只有实际使用的 provider 需要安装对应 SDK。
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from polymarket_arb.config import ArbConfig

LOG = logging.getLogger(__name__)

# provider -> 默认 api_base 映射（OpenAI 兼容系列）
_OPENAI_COMPAT_DEFAULTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

# provider -> 默认模型名
_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "deepseek": "deepseek-chat",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-sonnet-4-20250514",
    "ollama": "qwen2.5:7b",
}

# 粗略定价 (USD per 1M tokens): (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "deepseek-chat": (0.27, 1.1),
    "gemini-2.0-flash": (0.1, 0.4),
    "claude-sonnet-4-20250514": (3.0, 15.0),
}


@dataclass
class LLMResponse:
    """LLM 调用的统一返回值."""

    content: str
    input_tokens: int
    output_tokens: int
    model: str
    latency_ms: float
    tool_calls: list[dict] = field(default_factory=list)


class LLMProvider(ABC):
    """LLM 调用的统一抽象接口."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """发送 chat completion 请求."""

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """估算单次调用成本 (USD)."""
        pricing = _PRICING.get(model, (5.0, 15.0))
        return (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000


class OpenAIProvider(LLMProvider):
    """OpenAI / Azure OpenAI / 任何 OpenAI 兼容 API."""

    def __init__(self, api_key: str, api_base: str, model: str) -> None:
        self._model = model
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError("pip install openai>=1.30.0") from e

        kwargs: dict[str, Any] = {"api_key": api_key}
        if api_base:
            kwargs["base_url"] = api_base
        self._client = AsyncOpenAI(**kwargs)

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        start = time.monotonic()
        resp = await self._client.chat.completions.create(**kwargs)
        latency = (time.monotonic() - start) * 1000

        msg = resp.choices[0].message
        content = msg.content or ""
        tool_calls_parsed: list[dict] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, AttributeError):
                    args = {}
                tool_calls_parsed.append({"name": tc.function.name, "arguments": args})

        usage = resp.usage
        return LLMResponse(
            content=content,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=resp.model or self._model,
            latency_ms=round(latency, 1),
            tool_calls=tool_calls_parsed,
        )


class AnthropicProvider(LLMProvider):
    """Anthropic Claude 系列."""

    def __init__(self, api_key: str, model: str) -> None:
        self._model = model
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise ImportError("pip install anthropic>=0.30.0") from e
        self._client = AsyncAnthropic(api_key=api_key)

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        system_msg = ""
        chat_messages: list[dict] = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat_messages.append(m)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": chat_messages,
            "max_tokens": 2048,
            "temperature": temperature,
        }
        if system_msg:
            kwargs["system"] = system_msg

        anthropic_tools: list[dict] | None = None
        if tools:
            anthropic_tools = []
            for t in tools:
                func = t.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {}),
                })
            kwargs["tools"] = anthropic_tools

        start = time.monotonic()
        resp = await self._client.messages.create(**kwargs)
        latency = (time.monotonic() - start) * 1000

        content = ""
        tool_calls_parsed: list[dict] = []
        for block in resp.content:
            if block.type == "text":
                content = block.text
            elif block.type == "tool_use":
                tool_calls_parsed.append({
                    "name": block.name,
                    "arguments": block.input if isinstance(block.input, dict) else {},
                })

        return LLMResponse(
            content=content,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=self._model,
            latency_ms=round(latency, 1),
            tool_calls=tool_calls_parsed,
        )


class OllamaProvider(LLMProvider):
    """本地 Ollama 部署的开源模型."""

    def __init__(self, api_base: str, model: str) -> None:
        self._model = model
        self._base = api_base.rstrip("/")
        try:
            import httpx
            self._httpx = httpx
        except ImportError as e:
            raise ImportError("pip install httpx>=0.27.0") from e

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"

        start = time.monotonic()
        async with self._httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self._base}/api/chat", json=payload)
            resp.raise_for_status()
        latency = (time.monotonic() - start) * 1000

        data = resp.json()
        content = data.get("message", {}).get("content", "")
        input_tokens = data.get("prompt_eval_count", 0)
        output_tokens = data.get("eval_count", 0)

        return LLMResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=self._model,
            latency_ms=round(latency, 1),
        )

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return 0.0


def create_provider(config: ArbConfig) -> LLMProvider:
    """工厂函数：根据 AI_PROVIDER 配置创建对应 provider."""
    provider_name = config.ai_provider.lower().strip()
    api_key = config.ai_api_key
    api_base = config.ai_api_base
    model = config.ai_model or _DEFAULT_MODELS.get(provider_name, "gpt-4o")

    if provider_name in ("openai", "deepseek", "gemini"):
        if not api_base:
            api_base = _OPENAI_COMPAT_DEFAULTS.get(provider_name, "")
        assert api_key, f"AI_API_KEY is required for provider={provider_name}"
        LOG.info("LLM provider: %s (model=%s, base=%s)", provider_name, model, api_base)
        return OpenAIProvider(api_key=api_key, api_base=api_base, model=model)

    if provider_name == "anthropic":
        assert api_key, "AI_API_KEY is required for provider=anthropic"
        LOG.info("LLM provider: anthropic (model=%s)", model)
        return AnthropicProvider(api_key=api_key, model=model)

    if provider_name == "ollama":
        if not api_base:
            api_base = "http://localhost:11434"
        LOG.info("LLM provider: ollama (model=%s, base=%s)", model, api_base)
        return OllamaProvider(api_base=api_base, model=model)

    raise ValueError(
        f"Unknown AI_PROVIDER={provider_name!r}. "
        f"Supported: openai, deepseek, gemini, anthropic, ollama"
    )
