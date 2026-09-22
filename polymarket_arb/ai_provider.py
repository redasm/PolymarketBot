"""LLM Provider 抽象层：统一接口支持多 provider 自由切换.

通过 AI_PROVIDER 环境变量选择 provider，通过 AI_API_BASE 支持自定义端点。
DeepSeek / Gemini / Together 等 OpenAI 兼容 API 直接复用 OpenAIProvider。

按需导入：只有实际使用的 provider 需要安装对应 SDK。
"""

from __future__ import annotations

import asyncio
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
_DEFAULT_MAX_OUTPUT_TOKENS = 4096


class LLMProviderError(RuntimeError):
    """LLM provider 基础异常."""


class LLMConfigurationError(LLMProviderError, ValueError):
    """配置错误：缺失 API key 或 provider 配置非法."""


class LLMRequestError(LLMProviderError):
    """请求阶段错误：网络异常、HTTP 错误、响应格式异常."""


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

        return _parse_openai_compatible_response(resp, model=self._model, latency_ms=round(latency, 1))


def _parse_openai_compatible_response(resp: Any, *, model: str, latency_ms: float) -> LLMResponse:
    if isinstance(resp, str):
        try:
            payload = json.loads(resp)
        except json.JSONDecodeError as e:
            raise LLMRequestError(
                f"OpenAI 兼容响应格式异常: 返回了字符串而非 completion 对象, preview={resp[:200]!r}"
            ) from e
        return _parse_openai_compatible_dict(payload, fallback_model=model, latency_ms=latency_ms)

    if isinstance(resp, dict):
        return _parse_openai_compatible_dict(resp, fallback_model=model, latency_ms=latency_ms)

    if hasattr(resp, "choices"):
        msg = resp.choices[0].message
        content = _coerce_openai_message_content(getattr(msg, "content", ""))
        tool_calls_parsed: list[dict] = []
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, AttributeError, TypeError):
                    args = {}
                tool_calls_parsed.append({"name": tc.function.name, "arguments": args})

        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content=content,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            model=getattr(resp, "model", None) or model,
            latency_ms=latency_ms,
            tool_calls=tool_calls_parsed,
        )

    raise LLMRequestError(
        f"OpenAI 兼容响应格式异常: type={type(resp).__name__}, missing choices field"
    )


def _parse_openai_compatible_dict(payload: dict[str, Any], *, fallback_model: str, latency_ms: float) -> LLMResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMRequestError("OpenAI 兼容响应缺少 choices 列表")

    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        raise LLMRequestError("OpenAI 兼容响应中的 message 字段格式异常")

    tool_calls_parsed: list[dict] = []
    for tc in message.get("tool_calls", []) or []:
        if not isinstance(tc, dict):
            continue
        function = tc.get("function", {})
        if not isinstance(function, dict):
            function = {}
        raw_args = function.get("arguments", {})
        if isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed_args = {}
        elif isinstance(raw_args, dict):
            parsed_args = raw_args
        else:
            parsed_args = {}
        tool_calls_parsed.append({"name": function.get("name", ""), "arguments": parsed_args})

    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}

    return LLMResponse(
        content=_coerce_openai_message_content(message.get("content", "")),
        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage.get("completion_tokens", 0) or 0),
        model=str(payload.get("model") or fallback_model),
        latency_ms=latency_ms,
        tool_calls=tool_calls_parsed,
    )


def _coerce_openai_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(part for part in text_parts if part)
    return str(content or "")


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
        system_parts: list[str] = []
        chat_messages: list[dict] = []
        for m in messages:
            if m["role"] == "system":
                sanitized = _sanitize_system_prompt(m.get("content", ""))
                if sanitized:
                    system_parts.append(sanitized)
            else:
                chat_messages.append(m)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": chat_messages,
            "max_tokens": _DEFAULT_MAX_OUTPUT_TOKENS,
            "temperature": temperature,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        if json_mode:
            json_instruction = "Return valid JSON only. Do not include markdown fences or commentary."
            system_parts.append(json_instruction)
            kwargs["system"] = "\n\n".join(system_parts)

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

    def __init__(self, api_base: str, model: str, *, max_retries: int = 2, retry_delay_sec: float = 0.25) -> None:
        self._model = model
        self._base = api_base.rstrip("/")
        self._max_retries = max(0, int(max_retries))
        self._retry_delay_sec = max(0.0, float(retry_delay_sec))
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
        request_error_cls = getattr(self._httpx, "RequestError", ())
        http_status_error_cls = getattr(self._httpx, "HTTPStatusError", ())
        data: dict[str, Any] | None = None
        correlation_id = f"ollama-{int(time.time() * 1000)}"
        for attempt in range(self._max_retries + 1):
            try:
                async with self._httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(f"{self._base}/api/chat", json=payload)
                    resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                should_retry = False
                if request_error_cls and isinstance(e, request_error_cls):
                    should_retry = attempt < self._max_retries
                    if should_retry:
                        LOG.warning(
                            "[cid=%s] Ollama 请求失败，准备重试 (%d/%d): %s",
                            correlation_id,
                            attempt + 1,
                            self._max_retries + 1,
                            e,
                        )
                        await asyncio.sleep(self._retry_delay_sec)
                        continue
                    raise LLMRequestError(
                        f"Ollama 请求失败: base={self._base}, model={self._model}, error={e}"
                    ) from e
                if http_status_error_cls and isinstance(e, http_status_error_cls):
                    response = getattr(e, "response", None)
                    status_code = getattr(response, "status_code", "unknown")
                    if isinstance(status_code, int) and status_code >= 500 and attempt < self._max_retries:
                        LOG.warning(
                            "[cid=%s] Ollama HTTP %s，准备重试 (%d/%d)",
                            correlation_id,
                            status_code,
                            attempt + 1,
                            self._max_retries + 1,
                        )
                        await asyncio.sleep(self._retry_delay_sec)
                        continue
                    detail = ""
                    if response is not None:
                        try:
                            body_preview = response.text.strip()
                        except Exception:
                            body_preview = ""
                        if body_preview:
                            detail = f", body={body_preview[:200]!r}"
                    raise LLMRequestError(
                        f"Ollama HTTP 请求失败: status={status_code}, base={self._base}, model={self._model}{detail}"
                    ) from e
                if isinstance(e, ValueError):
                    raise LLMRequestError(
                        f"Ollama 返回了无法解析的响应: base={self._base}, model={self._model}"
                    ) from e
                raise

        if data is None:
            raise LLMRequestError(f"Ollama 请求失败: base={self._base}, model={self._model}, error=unknown")

        latency = (time.monotonic() - start) * 1000
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


def _require_api_key(provider_name: str) -> str:
    if provider_name in ("openai", "deepseek", "gemini"):
        return "AI_API_KEY 或 OPENAI_API_KEY"
    return "AI_API_KEY"


def _sanitize_system_prompt(content: Any, *, max_chars: int = 8_000) -> str:
    text = str(content or "")
    cleaned_chars: list[str] = []
    for ch in text:
        if ch in "\n\t" or ord(ch) >= 32:
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in cleaned.split("\n")]
    compact_lines: list[str] = []
    previous_blank = False
    for line in lines:
        if line:
            compact_lines.append(line)
            previous_blank = False
            continue
        if not previous_blank:
            compact_lines.append("")
        previous_blank = True
    return "\n".join(compact_lines).strip()[:max_chars]


def create_provider(config: ArbConfig) -> LLMProvider:
    """工厂函数：根据 AI_PROVIDER 配置创建对应 provider."""
    provider_name = config.ai_provider.lower().strip()
    api_key = config.ai_api_key
    api_base = config.ai_api_base
    model = config.ai_model or _DEFAULT_MODELS.get(provider_name, "gpt-4o")

    if provider_name in ("openai", "deepseek", "gemini"):
        if not api_base:
            api_base = _OPENAI_COMPAT_DEFAULTS.get(provider_name, "")
        if not api_key:
            raise LLMConfigurationError(
                f"缺少 API Key: provider={provider_name}，请设置 {_require_api_key(provider_name)}"
            )
        LOG.info("LLM provider: %s (model=%s, base=%s)", provider_name, model, api_base)
        return OpenAIProvider(api_key=api_key, api_base=api_base, model=model)

    if provider_name == "anthropic":
        if not api_key:
            raise LLMConfigurationError(
                f"缺少 API Key: provider={provider_name}，请设置 {_require_api_key(provider_name)}"
            )
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
