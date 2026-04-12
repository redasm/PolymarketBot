"""AI advisor schema validation tests."""

from __future__ import annotations

import asyncio

from polymarket_arb.ai_advisor import AIAdvisor
from polymarket_arb.ai_provider import LLMProvider, LLMResponse
from polymarket_arb.models import MarketContext
from tests.conftest import make_test_config


class _StubProvider(LLMProvider):
    def __init__(self, payload: dict | None = None, *, tool_calls: list[dict] | None = None):
        self.payload = payload or {}
        self.tool_calls = tool_calls or [{"name": "stub", "arguments": self.payload}]

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content="",
            input_tokens=100,
            output_tokens=50,
            model="gpt-4o-mini",
            latency_ms=10.0,
            tool_calls=self.tool_calls,
        )


def test_ai_advisor_skips_invalid_market_decisions_and_clamps_values():
    advisor = AIAdvisor(
        make_test_config(ai_enabled=True),
        provider=_StubProvider(
            {
                "decisions": [
                    {
                        "action": "buy_yes",
                        "market_id": "market-1",
                        "confidence": 1.8,
                        "recommended_size_pct": 0.4,
                        "reasoning": "Strong edge",
                        "urgency": 2,
                    },
                    {
                        "action": "BUY_NO",
                        "confidence": 0.7,
                        "recommended_size_pct": 0.05,
                        "reasoning": "Missing market id should be ignored",
                    },
                    {
                        "action": "INVALID",
                        "market_id": "market-2",
                        "confidence": 0.5,
                        "recommended_size_pct": 0.01,
                        "reasoning": "bad action",
                    },
                ]
            }
        ),
    )

    decisions = asyncio.run(advisor.evaluate_markets(MarketContext(timestamp=0)))

    assert len(decisions) == 1
    assert decisions[0].action == "BUY_YES"
    assert decisions[0].confidence == 1.0
    assert decisions[0].recommended_size_pct == 0.1
    assert decisions[0].urgency == 1.0


def test_ai_advisor_returns_hold_for_invalid_execution_schema():
    advisor = AIAdvisor(
        make_test_config(ai_enabled=True),
        provider=_StubProvider(
            {
                "action": "BUY_YES",
                "confidence": "not-a-number",
                "recommended_size_pct": "oops",
                "reasoning": "",
            }
        ),
    )

    decision = asyncio.run(advisor.evaluate_execution(MarketContext(timestamp=0), "opportunity"))

    assert decision.action == "BUY_YES"
    assert decision.confidence == 0.0
    assert decision.recommended_size_pct == 0.0
    assert decision.reasoning == "no_reasoning"


def test_ai_advisor_sanitizes_risk_adjustments():
    advisor = AIAdvisor(
        make_test_config(ai_enabled=True, ai_override_risk=True),
        provider=_StubProvider(
            {
                "adjustments": {
                    "max_exposure_factor": 9,
                    "daily_loss_factor": "0.25",
                    "ignored_field": 100,
                },
                "reasoning": "stress regime",
            }
        ),
    )

    adjustments = asyncio.run(advisor.adjust_risk_params(MarketContext(timestamp=0)))

    assert adjustments == {
        "max_exposure_factor": 1.5,
        "daily_loss_factor": 0.5,
    }


def test_ai_advisor_auto_recovers_after_timeout(monkeypatch):
    advisor = AIAdvisor(make_test_config(ai_enabled=True), provider=_StubProvider({"decisions": []}))
    advisor._degraded = True
    advisor._degraded_ts = 1.0
    monkeypatch.setattr("polymarket_arb.ai_advisor.time.time", lambda: 1.0 + 1900.0)

    should_eval = advisor.should_evaluate()

    assert should_eval is True
    assert advisor.is_degraded is False


def test_ai_advisor_merges_multiple_tool_calls_for_market_evaluation():
    advisor = AIAdvisor(
        make_test_config(ai_enabled=True),
        provider=_StubProvider(
            tool_calls=[
                {
                    "name": "submit_decisions",
                    "arguments": {
                        "decisions": [
                            {
                                "action": "BUY_YES",
                                "market_id": "market-1",
                                "confidence": 0.8,
                                "recommended_size_pct": 0.04,
                                "reasoning": "first",
                            }
                        ]
                    },
                },
                {
                    "name": "submit_decisions",
                    "arguments": {
                        "decisions": [
                            {
                                "action": "BUY_NO",
                                "market_id": "market-2",
                                "confidence": 0.7,
                                "recommended_size_pct": 0.03,
                                "reasoning": "second",
                            }
                        ]
                    },
                },
            ]
        ),
    )

    decisions = asyncio.run(advisor.evaluate_markets(MarketContext(timestamp=0)))

    assert [item.market_id for item in decisions] == ["market-1", "market-2"]


def test_ai_advisor_merges_multiple_tool_calls_for_risk_adjustment():
    advisor = AIAdvisor(
        make_test_config(ai_enabled=True, ai_override_risk=True),
        provider=_StubProvider(
            tool_calls=[
                {
                    "name": "submit_risk_adjustment",
                    "arguments": {
                        "adjustments": {"max_exposure_factor": 1.2},
                        "reasoning": "volatility elevated",
                    },
                },
                {
                    "name": "submit_risk_adjustment",
                    "arguments": {
                        "adjustments": {"daily_loss_factor": 0.8},
                        "reasoning": "drawdown rising",
                    },
                },
            ]
        ),
    )

    adjustments = asyncio.run(advisor.adjust_risk_params(MarketContext(timestamp=0)))

    assert adjustments == {
        "max_exposure_factor": 1.2,
        "daily_loss_factor": 0.8,
    }
