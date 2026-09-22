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
    """high_tail is now vetoed regardless of barbell — covers regression
    where geopolitical maker quotes blew the per-market exposure cap."""
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
    accepted = orchestrator.submit_signal(signal, active_markets=[_market()])
    assert accepted is False
    assert orchestrator.process_signals() == []


def test_orchestrator_enabled_barbell_does_not_override_high_tail_veto():
    """Veto preempts barbell — the relaxation pool can no longer rescue
    a geopolitical / war headline signal."""
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
    accepted = orchestrator.submit_signal(signal, active_markets=[_market()])
    assert accepted is False
    assert orchestrator.process_signals() == []
    status = orchestrator.get_status()
    assert status["meta"]["tail_risk"]["vetoed"] == 1


def test_orchestrator_tail_bucket_no_longer_books_vetoed_signals():
    """After veto, the tail bucket should not accumulate exposure for
    high_tail signals — they never reach the booking step."""
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
    assert orchestrator.submit_signal(make_signal(cid_a), active_markets=[_market(cid_a)]) is False
    assert orchestrator.submit_signal(make_signal(cid_b), active_markets=[_market(cid_b)]) is False
    assert orchestrator.process_signals() == []

    status = orchestrator.get_status()
    barbell_meta = status["meta"]["barbell"]
    assert barbell_meta["exposure_usdc"]["tail"] == 0.0


def test_orchestrator_barbell_irrelevant_after_t3_maker_veto():
    """T3 maker on geopolitical text is vetoed by tail_risk — barbell
    never sees the signal."""
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
    accepted = orchestrator.submit_signal(signal, active_markets=[_market()])
    assert accepted is False
    assert orchestrator.process_signals() == []
