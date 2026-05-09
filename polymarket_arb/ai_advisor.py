"""AI 决策顾问：通过 LLMProvider 抽象调用 LLM，输出结构化交易决策.

职责:
  - 市场评估: 批量扫描活跃市场，识别 mispricing
  - 执行决策: 对具体机会决定是否执行、仓位大小
  - 风控调整: 根据市场状态动态建议风控参数变更

安全约束:
  - 所有决策经 RiskManager 硬校验，AI 不能绕过
  - 日成本上限追踪，超限后自动降级为只读
  - 连续亏损自动降级
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from polymarket_arb.ai_context import MarketContextBuilder
from polymarket_arb.ai_prompts import (
    EXECUTION_DECISION_PROMPT,
    EXECUTION_TOOL,
    MARKET_EVAL_TOOL,
    MARKET_EVALUATION_PROMPT,
    RISK_ADJUSTMENT_PROMPT,
    RISK_TOOL,
    SYSTEM_BASE,
)
from polymarket_arb.ai_provider import LLMProvider, create_provider
from polymarket_arb.config import ArbConfig
from polymarket_arb.models import AIDecision, MarketContext

LOG = logging.getLogger(__name__)

_VALID_ACTIONS = {"BUY_YES", "BUY_NO", "SELL_YES", "SELL_NO", "HOLD", "CLOSE"}

class AIAdvisor:
    """AI 决策顾问，通过 LLMProvider 抽象调用 LLM."""

    def __init__(self, config: ArbConfig, provider: LLMProvider | None = None) -> None:
        self._config = config
        self._provider = provider or create_provider(config)
        self._context_builder = MarketContextBuilder()
        self._temperature = config.ai_temperature

        self._daily_cost_usd = 0.0
        # Reset key tracks the UTC calendar day, not a 24h sliding window. A
        # bot started at 23:59 UTC must reset its budget at the very next
        # 00:00 UTC, not 24h later.
        self._daily_cost_reset_day = _utc_day_key(time.time())
        self._call_count = 0
        self._consecutive_losses = 0
        self._degraded = False
        self._degraded_ts = 0.0
        self._decision_history: list[dict] = []
        self._last_eval_ts = 0.0

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    @property
    def decision_history(self) -> list[dict]:
        return list(self._decision_history[-50:])

    @property
    def daily_cost(self) -> float:
        return self._daily_cost_usd

    def should_evaluate(self) -> bool:
        """检查是否到了下一次 AI 评估的时间."""
        if self._degraded:
            if self._degraded_ts and (time.time() - self._degraded_ts) >= self._config.ai_auto_recover_sec:
                self.reset_degradation(reason="auto_recover_timeout")
            else:
                return False
        return (time.time() - self._last_eval_ts) >= self._config.ai_eval_interval_sec

    async def evaluate_markets(self, context: MarketContext) -> list[AIDecision]:
        """批量评估活跃市场，返回交易建议."""
        if not self._check_budget():
            return []

        prompt_text = self._context_builder.to_prompt_text(context)
        messages = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"{MARKET_EVALUATION_PROMPT}\n\n{prompt_text}"},
        ]

        resp = await self._call_llm(messages, tools=[MARKET_EVAL_TOOL])
        self._last_eval_ts = time.time()

        decisions_raw = self._extract_decisions(resp)
        decisions: list[AIDecision] = []
        for d in decisions_raw:
            parsed = self._parse_decision_dict(d, require_market_id=True)
            if parsed is None or parsed.action == "HOLD":
                continue
            decisions.append(parsed)

        for dec in decisions:
            self._record_decision("market_eval", dec)

        return decisions

    async def evaluate_execution(
        self,
        context: MarketContext,
        opportunity_summary: str,
    ) -> AIDecision:
        """对具体机会做执行决策."""
        if not self._check_budget():
            return self._hold_decision("budget_exceeded")

        prompt_text = self._context_builder.to_prompt_text(context)
        messages = [
            {"role": "system", "content": SYSTEM_BASE},
            {
                "role": "user",
                "content": (
                    f"{EXECUTION_DECISION_PROMPT}\n\n"
                    f"## Opportunity\n{opportunity_summary}\n\n"
                    f"## Market Context\n{prompt_text}"
                ),
            },
        ]

        resp = await self._call_llm(messages, tools=[EXECUTION_TOOL])
        parsed = self._extract_single_decision(resp)

        decision = self._parse_decision_dict(parsed, require_market_id=False) or self._hold_decision("invalid_execution_schema")

        self._record_decision("execution", decision)
        return decision

    async def adjust_risk_params(self, context: MarketContext) -> dict:
        """动态调整风控参数建议."""
        if not self._config.ai_override_risk:
            return {}
        if not self._check_budget():
            return {}

        prompt_text = self._context_builder.to_prompt_text(context)
        messages = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"{RISK_ADJUSTMENT_PROMPT}\n\n{prompt_text}"},
        ]

        resp = await self._call_llm(messages, tools=[RISK_TOOL])
        parsed = self._extract_risk_adjustment(resp)
        adjustments = self._sanitize_risk_adjustments(parsed.get("adjustments", {}))

        if adjustments:
            LOG.info(
                "AI 风控建议: %s, 原因: %s",
                adjustments,
                parsed.get("reasoning", ""),
            )

        return adjustments

    def record_trade_outcome(self, pnl: float) -> None:
        """记录交易结果用于自动降级判断."""
        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= 5:
                self._degraded = True
                self._degraded_ts = time.time()
                LOG.warning("AI 已降级: 连续 %d 笔亏损", self._consecutive_losses)
        else:
            self._consecutive_losses = 0
            if self._degraded:
                self.reset_degradation(reason="successful_trade")

    def reset_degradation(self, reason: str = "manual") -> None:
        self._degraded = False
        self._degraded_ts = 0.0
        self._consecutive_losses = 0
        LOG.info("AI 降级已解除: %s", reason)

    def get_status(self) -> dict:
        """获取 AI 顾问状态摘要."""
        return {
            "enabled": self._config.ai_enabled,
            "provider": self._config.ai_provider,
            "model": self._config.ai_model,
            "degraded": self._degraded,
            "daily_cost_usd": round(self._daily_cost_usd, 4),
            "cost_limit_usd": self._config.ai_max_cost_per_day,
            "call_count": self._call_count,
            "consecutive_losses": self._consecutive_losses,
            "recent_decisions": len(self._decision_history),
        }

    async def _call_llm(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> dict:
        """统一的 LLM 调用入口，带成本追踪和错误处理."""
        try:
            resp = await self._provider.chat(
                messages,
                temperature=self._temperature,
                json_mode=(tools is None),
                tools=tools,
            )
        except Exception as e:
            LOG.error("LLM 调用失败: %s", e)
            return {}

        cost = self._provider.estimate_cost(resp.model, resp.input_tokens, resp.output_tokens)
        self._daily_cost_usd += cost
        self._call_count += 1

        LOG.debug(
            "LLM call: model=%s, in=%d, out=%d, cost=$%.4f, latency=%.0fms",
            resp.model, resp.input_tokens, resp.output_tokens, cost, resp.latency_ms,
        )

        if resp.tool_calls:
            return self._merge_tool_call_arguments(resp.tool_calls)

        if resp.content:
            try:
                return json.loads(resp.content)
            except json.JSONDecodeError:
                LOG.warning("LLM 返回非 JSON 内容: %s", resp.content[:200])
                return {}
        return {}

    def _merge_tool_call_arguments(self, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        merged_decisions: list[dict[str, Any]] = []
        merged_adjustments: dict[str, Any] = {}
        reasoning_parts: list[str] = []

        for tool_call in tool_calls:
            args = tool_call.get("arguments", {})
            if not isinstance(args, dict):
                continue

            if isinstance(args.get("decisions"), list):
                merged_decisions.extend(item for item in args["decisions"] if isinstance(item, dict))

            adjustments = args.get("adjustments")
            if isinstance(adjustments, dict):
                merged_adjustments.update(adjustments)

            reasoning = args.get("reasoning")
            if isinstance(reasoning, str):
                cleaned = reasoning.strip()
                if cleaned:
                    reasoning_parts.append(cleaned)

            for key, value in args.items():
                if key in {"decisions", "adjustments", "reasoning"}:
                    continue
                merged[key] = value

        if merged_decisions:
            merged["decisions"] = merged_decisions
        if merged_adjustments:
            merged["adjustments"] = merged_adjustments
        if reasoning_parts:
            merged["reasoning"] = " | ".join(dict.fromkeys(reasoning_parts))

        return merged

    def _check_budget(self) -> bool:
        """检查是否超出日成本上限."""
        today = _utc_day_key(time.time())
        if today != self._daily_cost_reset_day:
            LOG.info(
                "AI 日成本已跨日重置: %s -> %s, 累计 $%.4f -> 0",
                self._daily_cost_reset_day,
                today,
                self._daily_cost_usd,
            )
            self._daily_cost_usd = 0.0
            self._daily_cost_reset_day = today

        if self._daily_cost_usd >= self._config.ai_max_cost_per_day:
            LOG.warning(
                "AI 日成本已达上限: $%.4f >= $%.2f",
                self._daily_cost_usd,
                self._config.ai_max_cost_per_day,
            )
            return False
        return True

    def _extract_decisions(self, parsed: dict) -> list[dict]:
        """从 LLM 响应提取决策列表."""
        if "decisions" in parsed:
            return parsed["decisions"] if isinstance(parsed["decisions"], list) else []
        if isinstance(parsed, list):
            return parsed
        return []

    def _extract_single_decision(self, parsed: dict) -> dict:
        """从 LLM 响应提取单个决策."""
        if "action" in parsed:
            return parsed
        if "decisions" in parsed and parsed["decisions"]:
            return parsed["decisions"][0]
        return {"action": "HOLD", "reasoning": "parse_failed"}

    def _extract_risk_adjustment(self, parsed: dict) -> dict:
        """从 LLM 响应提取风控调整."""
        if "adjustments" in parsed:
            return parsed
        return {"adjustments": {}, "reasoning": "no_adjustment"}

    def _hold_decision(self, reason: str) -> AIDecision:
        return AIDecision(
            action="HOLD",
            market_id="",
            confidence=0.0,
            recommended_size_pct=0.0,
            reasoning=reason,
        )

    def _parse_decision_dict(
        self,
        raw: dict[str, Any],
        *,
        require_market_id: bool,
    ) -> AIDecision | None:
        if not isinstance(raw, dict):
            return None
        action = str(raw.get("action", "HOLD")).upper()
        if action not in _VALID_ACTIONS:
            LOG.warning("AI 返回非法 action=%r，已降级为 HOLD", raw.get("action"))
            action = "HOLD"
        market_id = str(raw.get("market_id", "")).strip()
        if require_market_id and action != "HOLD" and not market_id:
            LOG.warning("AI 返回缺少 market_id 的决策，已忽略: %s", raw)
            return None
        reasoning = str(raw.get("reasoning", "") or "").strip()[:500]
        if not reasoning:
            reasoning = "no_reasoning"
        return AIDecision(
            action=action,
            market_id=market_id,
            confidence=self._coerce_float(raw.get("confidence"), default=0.0, min_value=0.0, max_value=1.0),
            recommended_size_pct=self._coerce_float(raw.get("recommended_size_pct"), default=0.0, min_value=0.0, max_value=0.1),
            reasoning=reasoning,
            urgency=self._coerce_float(raw.get("urgency"), default=0.5, min_value=0.0, max_value=1.0),
        )

    def _sanitize_risk_adjustments(self, raw: Any) -> dict[str, float]:
        if not isinstance(raw, dict):
            return {}
        adjustments: dict[str, float] = {}
        for key in ("max_exposure_factor", "daily_loss_factor"):
            if key not in raw:
                continue
            value = self._coerce_float(raw.get(key), default=None, min_value=0.5, max_value=1.5)
            if value is not None:
                adjustments[key] = value
        return adjustments

    def _coerce_float(
        self,
        value: Any,
        *,
        default: float | None,
        min_value: float,
        max_value: float,
    ) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, parsed))

    def _record_decision(self, decision_type: str, decision: AIDecision) -> None:
        entry = {
            "type": decision_type,
            "timestamp": time.time(),
            **decision.to_dict(),
        }
        self._decision_history.append(entry)
        if len(self._decision_history) > 200:
            self._decision_history = self._decision_history[-200:]


def _utc_day_key(timestamp: float) -> str:
    """Return YYYY-MM-DD (UTC) for use as a calendar-day budget reset key."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def create_ai_advisor(config: ArbConfig) -> Optional[AIAdvisor]:
    """工厂函数：仅在 AI_ENABLED=true 时创建 AIAdvisor."""
    if not config.ai_enabled:
        return None
    provider = create_provider(config)
    return AIAdvisor(config, provider)
