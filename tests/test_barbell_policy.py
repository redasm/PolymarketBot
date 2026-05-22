"""Unit tests for BarbellPolicy and its orchestrator integration."""

from __future__ import annotations

import pytest

from polymarket_arb.models import MarketInfo, TokenInfo
from polymarket_arb.strategies.signal_policies import BarbellPolicy
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)


def _market(condition_id: str = "cond-1234567890abcdef") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question="Will Iran and Israel reach a ceasefire this week?",
        slug="iran-israel-ceasefire",
        tokens=[
            TokenInfo(token_id="yes", outcome="Yes"),
            TokenInfo(token_id="no", outcome="No"),
        ],
    )


def test_policy_disabled_is_passthrough():
    policy = BarbellPolicy(enabled=False, tail_budget_usdc=100.0)
    decision = policy.decide(
        risk_class="high_tail", signal_size_usdc=50.0, current_tail_exposure_usdc=0.0
    )
    assert decision.bucket == "tail"
    assert decision.multiplier_override is None


def test_policy_routes_high_tail_to_tail_bucket_with_room():
    policy = BarbellPolicy(
        enabled=True, tail_budget_usdc=100.0, tail_relaxed_multiplier=0.85
    )
    decision = policy.decide(
        risk_class="high_tail", signal_size_usdc=50.0, current_tail_exposure_usdc=0.0
    )
    assert decision.bucket == "tail"
    assert decision.multiplier_override == 0.85


def test_policy_blocks_relaxation_when_tail_bucket_full():
    policy = BarbellPolicy(enabled=True, tail_budget_usdc=100.0)
    decision = policy.decide(
        risk_class="high_tail", signal_size_usdc=50.0, current_tail_exposure_usdc=80.0
    )
    assert decision.bucket == "tail_full"
    assert decision.multiplier_override is None


def test_policy_non_tail_signal_uses_data_driven_bucket():
    policy = BarbellPolicy(enabled=True, tail_budget_usdc=100.0)
    decision = policy.decide(
        risk_class="medium_tail", signal_size_usdc=50.0, current_tail_exposure_usdc=0.0
    )
    assert decision.bucket == "data_driven"
    assert decision.multiplier_override is None


def test_orchestrator_disabled_barbell_does_not_relax_high_tail_discount():
    orchestrator = StrategyOrchestrator(
        total_bankroll=1000,
        barbell_policy=BarbellPolicy(enabled=False, tail_budget_usdc=100.0),
    )
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="cond-1234567890abcdef",
        description="buy yes on a tail market",
        expected_edge=80.0,
        confidence=0.70,
        recommended_size_usdc=100.0,
    )
    orchestrator.submit_signal(signal, active_markets=[_market()])
    ready = orchestrator.process_signals()
    # Without barbell relaxation, the tail_risk rule discounts to 50.
    assert ready[0].recommended_size_usdc == 50.0
    barbell = ready[0].payload["barbell"]
    assert barbell["enabled"] is False
    assert barbell["applied"] is False


def test_orchestrator_enabled_barbell_relaxes_high_tail_discount_when_bucket_empty():
    orchestrator = StrategyOrchestrator(
        total_bankroll=1000,
        barbell_policy=BarbellPolicy(
            enabled=True, tail_budget_usdc=200.0, tail_relaxed_multiplier=0.85
        ),
    )
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="cond-1234567890abcdef",
        description="buy yes on a tail market",
        expected_edge=80.0,
        confidence=0.70,
        recommended_size_usdc=100.0,
    )
    orchestrator.submit_signal(signal, active_markets=[_market()])
    ready = orchestrator.process_signals()
    # Barbell relaxes to 0.85 against ORIGINAL size: 100 * 0.85 = 85
    assert ready[0].recommended_size_usdc == 85.0
    barbell = ready[0].payload["barbell"]
    assert barbell["applied"] is True
    assert barbell["bucket"] == "tail"
    assert barbell["multiplier_override"] == 0.85


def test_orchestrator_tail_bucket_fills_and_subsequent_signals_use_harsh_discount():
    orchestrator = StrategyOrchestrator(
        total_bankroll=1000,
        barbell_policy=BarbellPolicy(
            enabled=True, tail_budget_usdc=100.0, tail_relaxed_multiplier=0.85
        ),
    )

    def make_signal(market_id: str, size: float = 100.0) -> StrategySignal:
        return StrategySignal(
            tier=StrategyTier.STATISTICAL_ARB,
            signal_type="statistical_buy_yes",
            market_id=market_id,
            description="iran ceasefire vote",
            expected_edge=80.0,
            confidence=0.70,
            recommended_size_usdc=size,
        )

    cid_a = "cond-aaaaaaaaaaaaaaaa"
    cid_b = "cond-bbbbbbbbbbbbbbbb"
    # First signal — tail bucket empty → relaxed (×0.85 → 85)
    s1 = make_signal(cid_a)
    orchestrator.submit_signal(s1, active_markets=[_market(cid_a)])
    # Booked 85 against tail bucket. Next signal of size 100 → projected
    # 185 > budget 100 → no relaxation → harsh rule discount 0.5 applies.
    s2 = make_signal(cid_b)
    orchestrator.submit_signal(s2, active_markets=[_market(cid_b)])
    ready = orchestrator.process_signals()

    sizes = {sig.market_id: sig.recommended_size_usdc for sig in ready}
    assert sizes[cid_a] == 85.0
    assert sizes[cid_b] == 50.0  # fell back to TailRiskRule's 0.5

    status = orchestrator.get_status()
    barbell_meta = status["meta"]["barbell"]
    assert barbell_meta["enabled"] is True
    assert barbell_meta["tail_budget_usdc"] == 100.0
    # Tail bucket should be loaded with at least the first signal's 85.
    assert barbell_meta["exposure_usdc"]["tail"] >= 85.0


def test_orchestrator_barbell_skipped_for_non_t2_tier():
    orchestrator = StrategyOrchestrator(
        total_bankroll=1000,
        barbell_policy=BarbellPolicy(enabled=True, tail_budget_usdc=100.0),
    )
    signal = StrategySignal(
        tier=StrategyTier.MARKET_MAKING,
        signal_type="maker_quote",
        market_id="cond-1234567890abcdef",
        description="war headline",
        expected_edge=80.0,
        confidence=0.70,
        recommended_size_usdc=100.0,
    )
    orchestrator.submit_signal(signal, active_markets=[_market()])
    ready = orchestrator.process_signals()
    barbell = ready[0].payload["barbell"]
    assert barbell["applied"] is False
    assert barbell["bucket"] == "not_applicable"
