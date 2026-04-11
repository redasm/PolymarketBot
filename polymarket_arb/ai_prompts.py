"""AI 决策的 Prompt 模板和结构化输出 schema.

三种场景:
  1. MARKET_EVALUATION: 批量扫描市场，识别 mispricing
  2. EXECUTION_DECISION: 对具体机会做执行决策
  3. RISK_ADJUSTMENT: 动态调整风控参数
"""

from __future__ import annotations

SYSTEM_BASE = (
    "You are a quantitative trading AI for Polymarket prediction markets. "
    "You analyze orderbook data, volatility, and market context to make trading decisions. "
    "Always respond with valid JSON matching the requested schema. "
    "Be conservative — only recommend trades with clear edge. "
    "Never recommend sizes exceeding 10% of available capital per position."
)

MARKET_EVALUATION_PROMPT = (
    "Analyze the following market data and identify mispriced markets. "
    "For each market where you see edge, provide a trading recommendation. "
    "Consider: orderbook imbalance, volatility regime, spread, liquidity depth. "
    "If no clear opportunities exist, return an empty list."
)

EXECUTION_DECISION_PROMPT = (
    "An arbitrage/trading opportunity has been detected by the rule engine. "
    "Evaluate whether to execute it given the current market context and risk state. "
    "Consider: execution risk, market impact, current exposure, recent PnL. "
    "Recommend a position size as a fraction of available capital (0 to skip)."
)

RISK_ADJUSTMENT_PROMPT = (
    "Review the current risk state and market conditions. "
    "Suggest adjustments to risk parameters if warranted by market regime changes. "
    "You can adjust: max_exposure (within 50%-150% of base), daily_loss_limit. "
    "If no changes needed, return empty adjustments."
)

MARKET_EVAL_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_decisions",
        "description": "Submit trading decisions for evaluated markets",
        "parameters": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["BUY_YES", "BUY_NO", "SELL_YES", "SELL_NO", "HOLD"],
                            },
                            "market_id": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "recommended_size_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
                            "reasoning": {"type": "string"},
                            "urgency": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["action", "market_id", "confidence", "recommended_size_pct", "reasoning"],
                    },
                },
            },
            "required": ["decisions"],
        },
    },
}

EXECUTION_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_execution_decision",
        "description": "Decide whether and how to execute a detected opportunity",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["BUY_YES", "BUY_NO", "SELL_YES", "SELL_NO", "HOLD"],
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "recommended_size_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
                "reasoning": {"type": "string"},
                "urgency": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["action", "confidence", "recommended_size_pct", "reasoning"],
        },
    },
}

RISK_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_risk_adjustment",
        "description": "Suggest adjustments to risk parameters",
        "parameters": {
            "type": "object",
            "properties": {
                "adjustments": {
                    "type": "object",
                    "properties": {
                        "max_exposure_factor": {
                            "type": "number",
                            "minimum": 0.5,
                            "maximum": 1.5,
                            "description": "Multiplier on base max_total_exposure",
                        },
                        "daily_loss_factor": {
                            "type": "number",
                            "minimum": 0.5,
                            "maximum": 1.5,
                            "description": "Multiplier on base max_daily_loss",
                        },
                    },
                },
                "reasoning": {"type": "string"},
            },
            "required": ["adjustments", "reasoning"],
        },
    },
}

JSON_SCHEMA_MARKET_EVAL = {
    "decisions": [
        {
            "action": "BUY_YES|BUY_NO|SELL_YES|SELL_NO|HOLD",
            "market_id": "string",
            "confidence": 0.0,
            "recommended_size_pct": 0.0,
            "reasoning": "string",
            "urgency": 0.5,
        }
    ]
}

JSON_SCHEMA_EXECUTION = {
    "action": "BUY_YES|BUY_NO|SELL_YES|SELL_NO|HOLD",
    "confidence": 0.0,
    "recommended_size_pct": 0.0,
    "reasoning": "string",
    "urgency": 0.5,
}

JSON_SCHEMA_RISK = {
    "adjustments": {
        "max_exposure_factor": 1.0,
        "daily_loss_factor": 1.0,
    },
    "reasoning": "string",
}
