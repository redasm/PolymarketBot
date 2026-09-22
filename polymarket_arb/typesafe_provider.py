"""TypeSafe Jev（System One）打分 provider 薄封装.

Jev 不生成文本：对一个 ``state`` 并行回答若干类型化问题，返回校准概率。
接口是 ``(state, questions) -> answers``，与 ``ai_provider.LLMProvider``
（要求 provider 生成 JSON）完全无关，因此不复用那套抽象。

设计要点（见 doc/zh/ai-configuration.md 的 TypeSafe 一节）：

- 直接用 httpx 调 ``POST /v1/systemone``，不依赖 ``typesafe-sdk``：API 只有一个
  端点，自己重试才能把 429 / 529 次数和 usage 精确记进 telemetry。
- 默认 pin ``jev-1.13.0``，响应里的 ``model`` 字段一旦与 pin 不一致就告警
  （``jev-latest`` 漂移会让阈值失效）。
- 令牌桶限速（默认 10 RPS，官方限额 1,200 RPM 且"可能随时调整"）。
- 每次请求可通过 ``telemetry_sink`` 回调写一行结构化记录，累计计数在 ``stats``。

**不进热路径。** 只供 ``scripts/`` 下的旁路 worker 使用。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

LOG = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_RPS = 10.0
DEFAULT_TIMEOUT_SEC = 10.0
DEFAULT_MAX_RETRIES = 3
SYSTEM_ONE_PATH = "/v1/systemone"

# 官方文档：$0.042 / Mtok 输入，输出免费
INPUT_USD_PER_MTOK = 0.042

_RETRYABLE_STATUSES = frozenset({429, 529})

QuestionLike = Union["Noul", "Choice", "Score", dict[str, Any]]
TelemetrySink = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class TypeSafeError(RuntimeError):
    """TypeSafe provider 基础异常."""


class TypeSafeConfigurationError(TypeSafeError, ValueError):
    """配置错误：缺失 API key 或参数非法."""


class TypeSafeRequestError(TypeSafeError):
    """请求阶段错误：网络异常、HTTP 错误、响应格式异常."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        request_id: str = "",
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.attempts = attempts


class TypeSafeRateLimitError(TypeSafeRequestError):
    """重试耗尽后仍是 429 / 529."""


# ---------------------------------------------------------------------------
# 问题原语（与 API schema 一一对应）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Noul:
    """是/否问题，返回 0–1 概率."""

    instructions: Any
    criteria: Optional[dict[str, str]] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            payload["criteria"] = dict(self.criteria)
        return payload


@dataclass(frozen=True)
class Choice:
    """多选一，返回 choice + probabilities + confidence."""

    instructions: Any
    criteria: dict[str, Optional[str]]

    def __post_init__(self) -> None:
        if not self.criteria or len(self.criteria) < 2:
            raise TypeSafeConfigurationError("Choice.criteria 至少需要两个选项")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.criteria)}


@dataclass(frozen=True)
class Score:
    """有序等级打分，返回 score + probabilities + confidence."""

    instructions: Any
    criteria: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.criteria) < 2:
            raise TypeSafeConfigurationError("Score.criteria 至少需要两个等级")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.criteria)}


def _question_to_dict(name: str, question: QuestionLike) -> dict[str, Any]:
    if isinstance(question, dict):
        if question.get("type") not in ("noul", "choice", "score"):
            raise TypeSafeConfigurationError(f"question {name!r}: dict 形式必须带合法 type 字段")
        return dict(question)
    if isinstance(question, (Noul, Choice, Score)):
        return question.to_dict()
    raise TypeSafeConfigurationError(f"question {name!r}: 不支持的类型 {type(question).__name__}")


# ---------------------------------------------------------------------------
# 答案
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoulAnswer:
    noul: float


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


Answer = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer]


@dataclass
class SystemOneResponse:
    """一次 System One 调用的解析结果."""

    model: str
    answers: dict[str, Answer]
    input_tokens: int
    output_tokens: int
    latency_ms: float
    request_id: str = ""
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def nouls(self) -> dict[str, NoulAnswer]:
        return {k: v for k, v in self.answers.items() if isinstance(v, NoulAnswer)}

    @property
    def choices(self) -> dict[str, ChoiceAnswer]:
        return {k: v for k, v in self.answers.items() if isinstance(v, ChoiceAnswer)}

    @property
    def scores(self) -> dict[str, ScoreAnswer]:
        return {k: v for k, v in self.answers.items() if isinstance(v, ScoreAnswer)}

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * INPUT_USD_PER_MTOK / 1_000_000


def _bounded_unit(value: Any, *, field_name: str, question: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as e:
        raise TypeSafeRequestError(f"answer {question!r}: {field_name} 不是数值: {value!r}") from e
    if parsed != parsed:  # NaN
        raise TypeSafeRequestError(f"answer {question!r}: {field_name} 为 NaN")
    return max(0.0, min(1.0, parsed))


def _parse_probabilities(value: Any, *, question: str) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise TypeSafeRequestError(f"answer {question!r}: probabilities 缺失或为空")
    return {str(k): _bounded_unit(v, field_name=f"probabilities[{k}]", question=question) for k, v in value.items()}


def parse_answer(name: str, payload: Any) -> Answer:
    """把 API 的单个 answer 对象转成类型化答案；格式异常抛 TypeSafeRequestError."""
    if not isinstance(payload, dict):
        raise TypeSafeRequestError(f"answer {name!r}: 不是对象: {type(payload).__name__}")
    kind = payload.get("type")
    if kind == "noul":
        return NoulAnswer(noul=_bounded_unit(payload.get("noul"), field_name="noul", question=name))
    if kind == "choice":
        choice = payload.get("choice")
        if not isinstance(choice, str) or not choice:
            raise TypeSafeRequestError(f"answer {name!r}: choice 缺失")
        return ChoiceAnswer(
            choice=choice,
            probabilities=_parse_probabilities(payload.get("probabilities"), question=name),
            confidence=_bounded_unit(payload.get("confidence"), field_name="confidence", question=name),
        )
    if kind == "score":
        try:
            score = float(payload.get("score"))
        except (TypeError, ValueError) as e:
            raise TypeSafeRequestError(f"answer {name!r}: score 不是数值") from e
        legend_raw = payload.get("legend") or {}
        if not isinstance(legend_raw, dict):
            legend_raw = {}
        return ScoreAnswer(
            score=score,
            legend={str(k): str(v) for k, v in legend_raw.items()},
            probabilities=_parse_probabilities(payload.get("probabilities"), question=name),
            confidence=_bounded_unit(payload.get("confidence"), field_name="confidence", question=name),
        )
    raise TypeSafeRequestError(f"answer {name!r}: 未知 type={kind!r}")


def parse_system_one_payload(
    payload: Any,
    *,
    expected_questions: Optional[set[str]] = None,
    latency_ms: float = 0.0,
    request_id: str = "",
    attempts: int = 1,
) -> SystemOneResponse:
    """解析完整响应体；缺 question、类型不符都视为格式异常."""
    if not isinstance(payload, dict):
        raise TypeSafeRequestError(f"响应不是 JSON 对象: {type(payload).__name__}")
    answers_raw = payload.get("answers")
    if not isinstance(answers_raw, dict):
        raise TypeSafeRequestError("响应缺少 answers 对象")
    answers = {str(name): parse_answer(str(name), value) for name, value in answers_raw.items()}
    if expected_questions is not None:
        missing = sorted(expected_questions - set(answers))
        if missing:
            raise TypeSafeRequestError(f"响应缺少 question 答案: {missing}")
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return SystemOneResponse(
        model=str(payload.get("model") or ""),
        answers=answers,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        latency_ms=round(latency_ms, 1),
        request_id=request_id,
        attempts=attempts,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# 配置 / 限速 / 统计
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise TypeSafeConfigurationError(f"{name} 必须是数值，得到 {raw!r}") from e


@dataclass(frozen=True)
class TypeSafeConfig:
    """独立于 ArbConfig：只有旁路 worker 用，不往主循环配置里塞字段."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    rps: float = DEFAULT_RPS
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    max_retries: int = DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        if not self.api_key:
            raise TypeSafeConfigurationError("缺少 TYPESAFE_API_KEY")
        if self.rps <= 0:
            raise TypeSafeConfigurationError("TYPESAFE_RPS 必须 > 0")
        if self.timeout_sec <= 0:
            raise TypeSafeConfigurationError("TYPESAFE_TIMEOUT_SEC 必须 > 0")
        if self.max_retries < 0:
            raise TypeSafeConfigurationError("TYPESAFE_MAX_RETRIES 不能为负数")
        if not self.model:
            raise TypeSafeConfigurationError("TYPESAFE_MODEL 不能为空")

    @classmethod
    def from_env(cls) -> "TypeSafeConfig":
        """读 TYPESAFE_* 环境变量（调用方负责先 load_dotenv）."""
        return cls(
            api_key=os.getenv("TYPESAFE_API_KEY", "").strip(),
            base_url=(os.getenv("TYPESAFE_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/"),
            model=os.getenv("TYPESAFE_MODEL", "").strip() or DEFAULT_MODEL,
            rps=_env_float("TYPESAFE_RPS", DEFAULT_RPS),
            timeout_sec=_env_float("TYPESAFE_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC),
            max_retries=int(_env_float("TYPESAFE_MAX_RETRIES", float(DEFAULT_MAX_RETRIES))),
        )


class AsyncTokenBucket:
    """简单令牌桶：``rate`` 个/秒，桶容量 ``capacity``（默认 = rate，允许 1 秒突发）."""

    def __init__(
        self,
        rate: float,
        capacity: Optional[float] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise TypeSafeConfigurationError("token bucket rate 必须 > 0")
        self._rate = float(rate)
        self._capacity = float(capacity if capacity is not None else max(1.0, rate))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()
        self.total_wait_sec = 0.0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    async def acquire(self) -> float:
        """阻塞直到拿到一个令牌，返回等待秒数."""
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            wait = (1.0 - self._tokens) / self._rate
            # 在锁内预扣，避免并发 waiter 同时被放行
            self._tokens -= 1.0
        await self._sleep(wait)
        self.total_wait_sec += wait
        return wait


@dataclass
class TypeSafeStats:
    """进程内累计计数，worker 每轮把它落到 status 文件."""

    requests: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    rate_limited_429: int = 0
    overloaded_529: int = 0
    transport_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms_total: float = 0.0
    latency_ms_max: float = 0.0
    throttle_wait_sec: float = 0.0
    last_error: str = ""
    last_request_id: str = ""

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * INPUT_USD_PER_MTOK / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        avg = self.latency_ms_total / self.successes if self.successes else 0.0
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "rate_limited_429": self.rate_limited_429,
            "overloaded_529": self.overloaded_529,
            "transport_errors": self.transport_errors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms_avg": round(avg, 1),
            "latency_ms_max": round(self.latency_ms_max, 1),
            "throttle_wait_sec": round(self.throttle_wait_sec, 3),
            "last_error": self.last_error,
            "last_request_id": self.last_request_id,
        }


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def _retry_after_sec(headers: Any) -> Optional[float]:
    try:
        raw = headers.get("retry-after") if headers is not None else None
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return max(0.0, value)


class TypeSafeJevClient:
    """``POST /v1/systemone`` 的异步薄封装：限速 + 重试 + 解析 + 统计.

    用法::

        async with TypeSafeJevClient(TypeSafeConfig.from_env()) as client:
            resp = await client.system_one(state, {"q": Noul("...")})
            resp.nouls["q"].noul
    """

    def __init__(
        self,
        config: TypeSafeConfig,
        *,
        telemetry_sink: Optional[TelemetrySink] = None,
        transport: Any = None,
        backoff_base_sec: float = 0.5,
        backoff_max_sec: float = 8.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - 环境问题
            raise ImportError("pip install httpx>=0.27.0") from e
        self._httpx = httpx
        self._config = config
        self._telemetry_sink = telemetry_sink
        self._transport = transport
        self._backoff_base_sec = max(0.0, backoff_base_sec)
        self._backoff_max_sec = max(self._backoff_base_sec, backoff_max_sec)
        self._sleep = sleep
        self._bucket = AsyncTokenBucket(config.rps, sleep=sleep)
        self._client: Any = None
        self._model_drift_logged = False
        self.stats = TypeSafeStats()

    @property
    def config(self) -> TypeSafeConfig:
        return self._config

    async def __aenter__(self) -> "TypeSafeJevClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _ensure_client(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "base_url": self._config.base_url,
                "timeout": self._config.timeout_sec,
                "headers": {
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "polymarket-arb-typesafe/1",
                },
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = self._httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(self._backoff_max_sec, retry_after)
        base = min(self._backoff_max_sec, self._backoff_base_sec * (2 ** attempt))
        return base * (0.5 + random.random() * 0.5)

    def _emit(self, row: dict[str, Any]) -> None:
        if self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink(row)
        except Exception as e:  # sink 出错不能影响主流程
            LOG.warning("typesafe telemetry sink 写入失败: %s", e)

    async def system_one(
        self,
        state: Any,
        questions: dict[str, QuestionLike],
        *,
        model: Optional[str] = None,
        extra_body: Optional[dict[str, Any]] = None,
        tag: str = "",
    ) -> SystemOneResponse:
        """对 ``state`` 并行评估 ``questions``；失败抛 TypeSafeRequestError.

        ``tag`` 只用于 telemetry 行（例如 condition_id），不进请求体。
        """
        if not questions:
            raise TypeSafeConfigurationError("questions 不能为空")
        if state is None or (isinstance(state, (str, list, dict)) and not state):
            raise TypeSafeConfigurationError("state 不能为空")
        body: dict[str, Any] = {
            "state": state,
            "model": model or self._config.model,
            "questions": {str(name): _question_to_dict(str(name), q) for name, q in questions.items()},
        }
        if extra_body:
            body.update(extra_body)
        expected = set(body["questions"])

        client = self._ensure_client()
        self.stats.requests += 1
        started = time.monotonic()
        wait = await self._bucket.acquire()
        self.stats.throttle_wait_sec += wait

        max_attempts = self._config.max_retries + 1
        last_status: Optional[int] = None
        last_request_id = ""
        last_error = ""
        for attempt in range(max_attempts):
            if attempt > 0:
                self.stats.retries += 1
            attempt_started = time.monotonic()
            try:
                resp = await client.post(SYSTEM_ONE_PATH, json=body)
            except self._httpx.HTTPError as e:
                self.stats.transport_errors += 1
                last_error = f"{type(e).__name__}: {e}"
                last_status = None
                if attempt + 1 < max_attempts:
                    delay = self._backoff(attempt, None)
                    LOG.warning("typesafe 传输错误，%.2fs 后重试 (%d/%d): %s", delay, attempt + 1, max_attempts, last_error)
                    await self._sleep(delay)
                    continue
                break

            status = int(resp.status_code)
            last_status = status
            last_request_id = str(resp.headers.get("x-typesafe-request-id", "") or "")
            if status in _RETRYABLE_STATUSES:
                if status == 429:
                    self.stats.rate_limited_429 += 1
                else:
                    self.stats.overloaded_529 += 1
                last_error = f"HTTP {status}"
                if attempt + 1 < max_attempts:
                    delay = self._backoff(attempt, _retry_after_sec(resp.headers))
                    LOG.warning(
                        "typesafe HTTP %d，%.2fs 后重试 (%d/%d) request_id=%s",
                        status, delay, attempt + 1, max_attempts, last_request_id,
                    )
                    await self._sleep(delay)
                    continue
                break

            if status >= 400:
                try:
                    preview = resp.text.strip()[:300]
                except Exception:
                    preview = ""
                last_error = f"HTTP {status}: {preview}"
                break

            try:
                payload = resp.json()
            except ValueError as e:
                last_error = f"响应不是合法 JSON: {e}"
                break

            latency_ms = (time.monotonic() - attempt_started) * 1000
            try:
                parsed = parse_system_one_payload(
                    payload,
                    expected_questions=expected,
                    latency_ms=latency_ms,
                    request_id=last_request_id,
                    attempts=attempt + 1,
                )
            except TypeSafeRequestError as e:
                last_error = str(e)
                break

            self._record_success(parsed, tag=tag, question_count=len(expected), total_ms=(time.monotonic() - started) * 1000)
            return parsed

        # 走到这里就是失败
        self.stats.failures += 1
        self.stats.last_error = last_error
        self.stats.last_request_id = last_request_id
        total_ms = (time.monotonic() - started) * 1000
        self._emit(
            {
                "kind": "typesafe_request",
                "ts": time.time(),
                "ok": False,
                "tag": tag,
                "model": body["model"],
                "status": last_status,
                "attempts": attempt + 1,
                "latency_ms": round(total_ms, 1),
                "question_count": len(expected),
                "request_id": last_request_id,
                "error": last_error,
            }
        )
        message = (
            f"typesafe 请求失败: status={last_status}, model={body['model']}, "
            f"request_id={last_request_id or '-'}, error={last_error}"
        )
        if last_status in _RETRYABLE_STATUSES:
            raise TypeSafeRateLimitError(message, status=last_status, request_id=last_request_id, attempts=max_attempts)
        raise TypeSafeRequestError(message, status=last_status, request_id=last_request_id, attempts=max_attempts)

    def _record_success(self, parsed: SystemOneResponse, *, tag: str, question_count: int, total_ms: float) -> None:
        self.stats.successes += 1
        self.stats.input_tokens += parsed.input_tokens
        self.stats.output_tokens += parsed.output_tokens
        self.stats.latency_ms_total += parsed.latency_ms
        self.stats.latency_ms_max = max(self.stats.latency_ms_max, parsed.latency_ms)
        self.stats.last_request_id = parsed.request_id
        if parsed.model and parsed.model != self._config.model and not self._model_drift_logged:
            self._model_drift_logged = True
            LOG.warning(
                "typesafe 响应 model=%s 与配置 pin=%s 不一致；校准阈值可能失效",
                parsed.model, self._config.model,
            )
        self._emit(
            {
                "kind": "typesafe_request",
                "ts": time.time(),
                "ok": True,
                "tag": tag,
                "model": parsed.model or self._config.model,
                "status": 200,
                "attempts": parsed.attempts,
                "latency_ms": parsed.latency_ms,
                "total_ms": round(total_ms, 1),
                "question_count": question_count,
                "input_tokens": parsed.input_tokens,
                "output_tokens": parsed.output_tokens,
                "cost_usd": round(parsed.cost_usd, 6),
                "request_id": parsed.request_id,
            }
        )


def create_typesafe_client(
    config: Optional[TypeSafeConfig] = None,
    *,
    telemetry_sink: Optional[TelemetrySink] = None,
) -> TypeSafeJevClient:
    """工厂：未传 config 时从 TYPESAFE_* 环境变量读取."""
    cfg = config or TypeSafeConfig.from_env()
    LOG.info("TypeSafe Jev client: model=%s base=%s rps=%.1f", cfg.model, cfg.base_url, cfg.rps)
    return TypeSafeJevClient(cfg, telemetry_sink=telemetry_sink)
