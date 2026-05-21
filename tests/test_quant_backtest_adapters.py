from __future__ import annotations

import pytest

from research.backtest.adapters.quant_strategy_adapter import (
    EventCalendarBacktestAdapter,
    LogicalConstraintBacktestAdapter,
    WalletAlphaBacktestAdapter,
)
from polymarket_arb.strategies.logical_constraints import RelationRule
from polymarket_arb.strategies.wallet_alpha import WalletProfile


def test_logical_constraint_backtest_adapter_detects_violation_from_rows() -> None:
    adapter = LogicalConstraintBacktestAdapter(
        rules=[
            RelationRule(
                subject_market_id="candidate",
                bound_market_id="party",
                relation_type="subject_lte_bound",
                min_violation_bps=200,
            )
        ],
        order_size_usdc=12,
    )

    signals = adapter.detect_many(
        [
            {
                "condition_id": "candidate",
                "question": "Will candidate win?",
                "yes_best_bid": 0.62,
                "yes_best_ask": 0.64,
            },
            {
                "condition_id": "party",
                "question": "Will party win?",
                "yes_best_bid": 0.54,
                "yes_best_ask": 0.56,
                "yes_ask_size": 80,
            },
        ]
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.signal_type == "logical_constraint_directional_buy_bound"
    assert signal.market_id == "party"
    assert signal.expected_edge == 800.0
    assert adapter.to_order_request(signal, {"yes_best_ask": 0.56, "yes_ask_size": 80}) == {
        "side": "BUY",
        "size": 12 / 0.56,
        "best_ask": 0.56,
        "available_size": 80.0,
        "ask_levels": [(0.56, 80.0)],
    }


def test_event_calendar_backtest_adapter_detects_baseline_edge() -> None:
    adapter = EventCalendarBacktestAdapter(order_size_usdc=10, taker_fee_rate=0.0)

    signal = adapter.detect(
        {
            "condition_id": "event",
            "question": "Will CPI beat?",
            "yes_best_bid": 0.40,
            "yes_best_ask": 0.42,
            "yes_ask_size": 100,
            "baseline_probability": 0.50,
            "confidence": 0.80,
            "time_to_event_sec": 1800,
        }
    )

    assert signal is not None
    assert signal.signal_type == "event_calendar_buy_yes"
    assert signal.expected_edge == 900.0


def test_wallet_alpha_backtest_adapter_accepts_validated_wallet_observation() -> None:
    adapter = WalletAlphaBacktestAdapter(
        profiles={
            "0xgood": WalletProfile(
                wallet_address="0xgood",
                trade_count=40,
                realized_roi=0.12,
                lagged_follow_roi=0.07,
                max_drawdown=0.12,
                concentration_score=0.20,
                category_edges={"macro": 0.08},
            )
        },
        order_size_usdc=15,
    )

    signal = adapter.detect(
        {
            "condition_id": "market",
            "question": "Will macro event happen?",
            "wallet_address": "0xgood",
            "category": "macro",
            "action": "BUY_YES",
            "yes_best_ask": 0.48,
            "yes_ask_size": 20,
        }
    )

    assert signal is not None
    assert signal.signal_type == "wallet_alpha_buy_yes"
    assert signal.expected_edge == pytest.approx(700.0)
