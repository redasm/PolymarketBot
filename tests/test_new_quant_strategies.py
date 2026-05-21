from __future__ import annotations

from polymarket_arb.models import MarketInfo, TokenInfo
from polymarket_arb.strategies.event_calendar_model import EventCalendarModel, EventPricingInput
from polymarket_arb.strategies.logical_constraints import LogicalConstraintDetector, RelationRule
from polymarket_arb.strategies.sniper_gate import SniperGate, SniperGateConfig
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)
from polymarket_arb.strategies.wallet_alpha import WalletAlphaScorer, WalletProfile


def _market(
    condition_id: str = "cond-a",
    question: str = "Will Candidate A win?",
    *,
    liquidity: float = 1000.0,
    volume_24h: float = 500.0,
) -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question=question,
        slug=condition_id,
        tokens=[TokenInfo(token_id=f"{condition_id}-yes", outcome="Yes")],
        liquidity=liquidity,
        volume_24h=volume_24h,
    )


def test_sniper_gate_rejects_directional_signal_below_edge_and_confidence() -> None:
    gate = SniperGate(SniperGateConfig(min_net_edge_bps=250.0, min_confidence=0.75))
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="event_buy_yes",
        market_id="cond-a",
        description="weak directional signal",
        expected_edge=180.0,
        confidence=0.70,
        recommended_size_usdc=25.0,
    )

    decision = gate.evaluate(signal, market=_market())

    assert decision.accepted is False
    assert "edge_below_min" in decision.reasons
    assert "confidence_below_min" in decision.reasons


def test_orchestrator_uses_optional_sniper_gate_without_blocking_structural_arb() -> None:
    gate = SniperGate(SniperGateConfig(min_net_edge_bps=250.0, min_confidence=0.75))
    orchestrator = StrategyOrchestrator(total_bankroll=1000, sniper_gate=gate)
    weak_directional = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="event_buy_yes",
        market_id="cond-a",
        description="weak directional signal",
        expected_edge=180.0,
        confidence=0.70,
        recommended_size_usdc=25.0,
    )
    structural = StrategySignal(
        tier=StrategyTier.STRUCTURAL_ARB,
        signal_type="binary_arb",
        market_id="cond-b",
        description="locked binary arb",
        expected_edge=20.0,
        confidence=0.95,
        recommended_size_usdc=25.0,
    )

    assert orchestrator.submit_signal(weak_directional, active_markets=[_market()]) is False
    assert orchestrator.submit_signal(structural) is True

    ready = orchestrator.process_signals()
    status = orchestrator.get_status()
    assert [signal.market_id for signal in ready] == ["cond-b"]
    assert status["meta"]["sniper_gate"]["rejected"] == 1


def test_logical_constraint_detector_emits_signal_when_upper_bound_is_too_cheap() -> None:
    detector = LogicalConstraintDetector(
        rules=[
            RelationRule(
                subject_market_id="candidate-a",
                bound_market_id="party-a",
                relation_type="subject_lte_bound",
                min_violation_bps=200.0,
            )
        ],
        default_size_usdc=10.0,
    )

    signals = detector.detect(
        markets={
            "candidate-a": _market("candidate-a", "Will Candidate A win?"),
            "party-a": _market("party-a", "Will Party A win?"),
        },
        yes_prices={"candidate-a": 0.62, "party-a": 0.55},
    )

    assert len(signals) == 1
    assert signals[0].signal_type == "logical_constraint_buy_bound"
    assert signals[0].market_id == "party-a"
    assert signals[0].expected_edge == 700.0
    assert signals[0].payload["relation_type"] == "subject_lte_bound"


def test_event_calendar_model_requires_near_event_and_fee_adjusted_edge() -> None:
    model = EventCalendarModel(
        min_edge_bps=300.0,
        min_confidence=0.70,
        max_time_to_event_sec=24 * 3600,
        default_size_usdc=8.0,
    )

    signal = model.evaluate(
        EventPricingInput(
            market=_market("cpi", "Will CPI come in above 3.0%?"),
            market_price=0.42,
            baseline_probability=0.48,
            confidence=0.76,
            time_to_event_sec=2 * 3600,
            taker_fee_rate=0.05,
        )
    )
    stale = model.evaluate(
        EventPricingInput(
            market=_market("late", "Will CPI come in above 3.0%?"),
            market_price=0.42,
            baseline_probability=0.60,
            confidence=0.90,
            time_to_event_sec=7 * 24 * 3600,
            taker_fee_rate=0.05,
        )
    )

    assert signal is not None
    assert signal.signal_type == "event_calendar_buy_yes"
    assert signal.expected_edge > 300.0
    assert stale is None


def test_wallet_alpha_scorer_scores_only_lagged_repeatable_alpha() -> None:
    scorer = WalletAlphaScorer(min_trades=30, min_lagged_roi=0.04, max_concentration=0.35)
    good = WalletProfile(
        wallet_address="0xgood",
        trade_count=42,
        realized_roi=0.31,
        lagged_follow_roi=0.08,
        max_drawdown=0.12,
        concentration_score=0.22,
        category_edges={"weather": 0.11},
    )
    flashy_but_unfollowable = WalletProfile(
        wallet_address="0xflash",
        trade_count=9,
        realized_roi=1.50,
        lagged_follow_roi=-0.03,
        max_drawdown=0.40,
        concentration_score=0.80,
        category_edges={"crypto": 1.50},
    )

    good_decision = scorer.evaluate(good, category="weather")
    bad_decision = scorer.evaluate(flashy_but_unfollowable, category="crypto")

    assert good_decision.accepted is True
    assert good_decision.confidence > 0.70
    assert bad_decision.accepted is False
    assert "insufficient_trades" in bad_decision.reasons
    assert "lagged_roi_below_min" in bad_decision.reasons
