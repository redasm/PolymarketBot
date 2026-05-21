"""Strategy orchestrator tests for research overlay behavior."""

from __future__ import annotations

from polymarket_arb.models import MarketInfo, ResearchSignal, ResearchSignalReport, TokenInfo
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)


def _make_market(condition_id: str = "cond-1234567890abcdef", event_id: str = "event-1") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question="Will BTC break 100k before June?",
        slug="btc-100k",
        tokens=[TokenInfo(token_id="yes", outcome="Yes"), TokenInfo(token_id="no", outcome="No")],
        event_id=event_id,
    )


def test_research_overlay_boosts_aligned_signal():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = _make_market()
    market.raw["research_signals"] = [
        ResearchSignal(
            topic_id="event:event-1",
            event_candidates=["event-1"],
            summary="BTC momentum remains strong",
            sources=["polymarket_market", "google_news_rss"],
            confidence=0.72,
            freshness_sec=60.0,
            stance="bullish",
        ).to_dict()
    ]
    report = ResearchSignalReport(
        generated_at=1.0,
        window_sec=86400,
        market_count=1,
        row_count=2,
        topic_count=1,
        source_counts={"polymarket_market": 1, "google_news_rss": 1},
        signals=[],
    )
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id=market.condition_id[:12],
        description="Stat arb positive edge",
        expected_edge=80.0,
        confidence=0.60,
        recommended_size_usdc=100.0,
    )

    accepted = orchestrator.submit_signal(
        signal,
        active_markets=[market],
        research_report=report,
        research_signals=[],
    )
    ready = orchestrator.process_signals()

    assert accepted is True
    assert len(ready) == 1
    assert ready[0].confidence > 0.60
    assert ready[0].recommended_size_usdc > 100.0
    assert ready[0].payload["research_overlay"]["reasons"][0] == "research_aligned"
    assert signal.confidence == 0.60
    assert signal.recommended_size_usdc == 100.0


def test_research_overlay_adds_resonance_for_three_aligned_sources():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = _make_market()
    market.raw["research_signals"] = [
        ResearchSignal(
            topic_id="event:event-1-a",
            event_candidates=["event-1"],
            summary="BTC momentum remains strong",
            sources=["google_news_rss"],
            confidence=0.74,
            freshness_sec=60.0,
            stance="bullish",
        ).to_dict(),
        ResearchSignal(
            topic_id="event:event-1-b",
            event_candidates=["event-1"],
            summary="Order flow favors upside",
            sources=["polymarket_market"],
            confidence=0.70,
            freshness_sec=90.0,
            stance="bullish",
        ).to_dict(),
        ResearchSignal(
            topic_id="event:event-1-c",
            event_candidates=["event-1"],
            summary="Local model agrees with upside",
            sources=["local_kb"],
            confidence=0.72,
            freshness_sec=120.0,
            stance="bullish",
        ).to_dict(),
    ]
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id=market.condition_id[:12],
        description="Stat arb positive edge",
        expected_edge=80.0,
        confidence=0.60,
        recommended_size_usdc=100.0,
    )

    assert orchestrator.submit_signal(signal, active_markets=[market]) is True
    ready = orchestrator.process_signals()
    overlay = ready[0].payload["research_overlay"]

    assert "research_resonance" in overlay["reasons"]
    assert overlay["source_diversity"] == 3
    assert overlay["resonance_score"] >= 0.70
    assert ready[0].recommended_size_usdc > 110.0


def test_signal_size_cap_limits_compounded_overlay_boosts():
    orchestrator = StrategyOrchestrator(total_bankroll=1000, max_signal_size_multiplier=1.05)
    market = _make_market()
    market.raw["research_signals"] = [
        ResearchSignal(
            topic_id="event:event-1",
            event_candidates=["event-1"],
            summary="BTC momentum remains strong",
            sources=["google_news_rss"],
            confidence=0.90,
            freshness_sec=60.0,
            stance="bullish",
        ).to_dict(),
    ]
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id=market.condition_id[:12],
        description="Stat arb positive edge",
        expected_edge=80.0,
        confidence=0.60,
        recommended_size_usdc=100.0,
    )

    assert orchestrator.submit_signal(signal, active_markets=[market]) is True
    ready = orchestrator.process_signals()

    assert ready[0].recommended_size_usdc == 105.0
    assert ready[0].payload["risk_size_cap"]["max_signal_size_multiplier"] == 1.05


def test_research_overlay_penalizes_but_does_not_veto_two_row_conflict():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = _make_market()
    conflicting_rows = [
        ResearchSignal(
            topic_id="event:event-1",
            event_candidates=["event-1"],
            summary="BTC faces sharp downside risk",
            sources=["google_news_rss"],
            confidence=0.80,
            freshness_sec=90.0,
            stance="bearish",
        ).to_dict(),
        ResearchSignal(
            topic_id="event:event-1-2",
            event_candidates=["event-1"],
            summary="Traders turn defensive on BTC",
            sources=["polymarket_market"],
            confidence=0.82,
            freshness_sec=120.0,
            stance="bearish",
        ).to_dict(),
    ]
    market.raw["research_signals"] = conflicting_rows
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id=market.condition_id[:12],
        description="Stat arb positive edge",
        expected_edge=70.0,
        confidence=0.65,
        recommended_size_usdc=100.0,
    )

    accepted = orchestrator.submit_signal(
        signal,
        active_markets=[market],
        research_report=None,
        research_signals=[],
    )
    ready = orchestrator.process_signals()

    status = orchestrator.get_status()
    assert accepted is True
    assert ready[0].payload["research_overlay"]["veto"] is False
    assert "research_conflict" in ready[0].payload["research_overlay"]["reasons"]
    assert "high_conviction_conflict" in ready[0].payload["research_overlay"]["reasons"]
    assert ready[0].recommended_size_usdc < 100.0
    assert ready[0].confidence < 0.65
    assert status["meta"]["research_overlay"]["vetoed"] == 0


def test_tail_risk_discount_reduces_directional_high_tail_signal():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = _make_market()
    market.question = "Will there be an Iran Israel ceasefire this week?"
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id=market.condition_id[:12],
        description="directional edge",
        expected_edge=90.0,
        confidence=0.70,
        recommended_size_usdc=100.0,
    )

    assert orchestrator.submit_signal(signal, active_markets=[market]) is True
    ready = orchestrator.process_signals()
    tail_risk = ready[0].payload["tail_risk"]
    status = orchestrator.get_status()

    assert tail_risk["risk_class"] == "high_tail"
    assert tail_risk["size_multiplier"] == 0.5
    assert ready[0].recommended_size_usdc == 50.0
    assert ready[0].confidence == 0.60
    assert status["meta"]["tail_risk"]["high_risk"] == 1


def test_research_overlay_vetoes_only_on_three_row_high_conflict():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = _make_market()
    market.raw["research_signals"] = [
        ResearchSignal(
            topic_id="event:event-1",
            event_candidates=["event-1"],
            summary="BTC faces sharp downside risk",
            sources=["google_news_rss"],
            confidence=0.91,
            freshness_sec=90.0,
            stance="bearish",
        ).to_dict(),
        ResearchSignal(
            topic_id="event:event-1-2",
            event_candidates=["event-1"],
            summary="Traders turn defensive on BTC",
            sources=["polymarket_market"],
            confidence=0.88,
            freshness_sec=120.0,
            stance="bearish",
        ).to_dict(),
        ResearchSignal(
            topic_id="event:event-1-3",
            event_candidates=["event-1"],
            summary="Macro desk stays bearish BTC",
            sources=["local_kb"],
            confidence=0.87,
            freshness_sec=150.0,
            stance="bearish",
        ).to_dict(),
    ]
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id=market.condition_id[:12],
        description="Stat arb positive edge",
        expected_edge=70.0,
        confidence=0.65,
        recommended_size_usdc=100.0,
    )

    accepted = orchestrator.submit_signal(
        signal,
        active_markets=[market],
        research_report=None,
        research_signals=[],
    )

    status = orchestrator.get_status()
    assert accepted is False
    assert orchestrator.process_signals() == []
    assert signal.payload == {}
    assert status["meta"]["research_overlay"]["vetoed"] == 1


def test_strategy_status_exposes_tier_budgets_and_overlay_meta():
    # Allocations were re-weighted toward T3 (maker) after Becker 2025
    # documented the maker-side structural edge — see DEFAULT_ALLOCATIONS
    # in strategy_orchestrator.py. The expected budgets below track the
    # current 30 / 5 / 15 / 50 split on a $500 bankroll.
    orchestrator = StrategyOrchestrator(total_bankroll=500)

    status = orchestrator.get_status()

    assert status["T0"]["budget"] == 150.0
    assert status["T1"]["budget"] == 25.0
    assert status["T2"]["budget"] == 75.0
    assert status["T3"]["budget"] == 250.0
    assert status["meta"]["research_overlay"]["applied"] == 0


def test_research_overlay_ignores_unrelated_global_signals():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = _make_market()
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id=market.condition_id[:12],
        description="Stat arb positive edge",
        expected_edge=80.0,
        confidence=0.60,
        recommended_size_usdc=100.0,
    )
    research_signals = [
        ResearchSignal(
            topic_id="event:event-1",
            event_candidates=["event-1"],
            summary="BTC momentum remains strong",
            sources=["google_news_rss"],
            confidence=0.72,
            freshness_sec=60.0,
            stance="bullish",
        ),
        ResearchSignal(
            topic_id="event:event-2",
            event_candidates=["event-2"],
            summary="ETH sentiment turns sharply bearish",
            sources=["google_news_rss"],
            confidence=0.95,
            freshness_sec=60.0,
            stance="bearish",
        ),
    ]

    accepted = orchestrator.submit_signal(
        signal,
        active_markets=[market],
        research_report=None,
        research_signals=research_signals,
    )
    ready = orchestrator.process_signals()

    assert accepted is True
    assert ready[0].payload["research_overlay"]["matched_count"] == 1
    assert ready[0].payload["research_overlay"]["dominant_stance"] == "bullish"
    assert "mixed_research_stance" not in ready[0].payload["research_overlay"]["reasons"]


def test_research_overlay_does_not_apply_without_market_match():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = _make_market()
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id="unknown-market",
        description="Stat arb positive edge",
        expected_edge=80.0,
        confidence=0.60,
        recommended_size_usdc=100.0,
    )

    accepted = orchestrator.submit_signal(
        signal,
        active_markets=[market],
        research_report=None,
        research_signals=[
            ResearchSignal(
                topic_id="event:event-1",
                event_candidates=["event-1"],
                summary="BTC momentum remains strong",
                sources=["google_news_rss"],
                confidence=0.99,
                freshness_sec=60.0,
                stance="bearish",
            )
        ],
    )
    ready = orchestrator.process_signals()

    assert accepted is True
    assert ready[0].payload["research_overlay"]["applied"] is False
    assert signal.recommended_size_usdc == 100.0


def test_process_signals_can_be_marked_processed_without_leaking_pending():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_buy_yes",
        market_id="market-1",
        description="signal",
        expected_edge=80.0,
        confidence=0.60,
        recommended_size_usdc=100.0,
    )

    accepted = orchestrator.submit_signal(signal)
    ready = orchestrator.process_signals()
    for item in ready:
        orchestrator.record_processed(item)

    status = orchestrator.get_status()
    assert accepted is True
    assert len(ready) == 1
    assert status["meta"]["pending_signals"] == 0
    assert status["meta"]["executed_signals"] == 1


def test_record_execution_can_book_real_exposure_on_failed_trade():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    signal = StrategySignal(
        tier=StrategyTier.STRUCTURAL_ARB,
        signal_type="binary_arb",
        market_id="market-1",
        description="signal",
        expected_edge=30.0,
        confidence=0.95,
        recommended_size_usdc=100.0,
    )

    orchestrator.record_execution(
        signal,
        success=False,
        exposure_amount_usdc=42.5,
    )

    status = orchestrator.get_status()
    assert status["T0"]["current_exposure"] == 42.5
    assert status["T0"]["trade_count"] == 1


def test_drawdown_scaling_reduces_directional_signal_size_after_losses():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    loss = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="loss-market",
        description="loss",
        expected_edge=100.0,
        confidence=0.8,
        recommended_size_usdc=100.0,
    )
    orchestrator.record_execution(loss, success=True, pnl=-60.0, exposure_amount_usdc=0.0)
    next_signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="next-market",
        description="next",
        expected_edge=100.0,
        confidence=0.8,
        recommended_size_usdc=100.0,
    )

    assert orchestrator.submit_signal(next_signal) is True
    ready = orchestrator.process_signals()

    assert ready[0].recommended_size_usdc == 50.0
    assert ready[0].payload["drawdown_size_scaling"]["multiplier"] == 0.5


def test_drawdown_scaling_blocks_directional_signals_after_deep_drawdown():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    loss = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="loss-market",
        description="loss",
        expected_edge=100.0,
        confidence=0.8,
        recommended_size_usdc=100.0,
    )
    orchestrator.record_execution(loss, success=True, pnl=-160.0, exposure_amount_usdc=0.0)
    next_signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="next-market",
        description="next",
        expected_edge=100.0,
        confidence=0.8,
        recommended_size_usdc=100.0,
    )

    assert orchestrator.submit_signal(next_signal) is False
    assert orchestrator.process_signals() == []


def test_process_signals_prefers_higher_confidence_after_edge_normalization():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    low_conf_large_edge = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_large_edge",
        market_id="market-1",
        description="large raw edge but weaker confidence",
        expected_edge=200.0,
        confidence=0.20,
        recommended_size_usdc=50.0,
        urgency=0.50,
    )
    high_conf_smaller_edge = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="stat_high_conf",
        market_id="market-2",
        description="smaller raw edge but much stronger confidence",
        expected_edge=60.0,
        confidence=0.90,
        recommended_size_usdc=50.0,
        urgency=0.90,
    )

    orchestrator.submit_signal(low_conf_large_edge)
    orchestrator.submit_signal(high_conf_smaller_edge)
    ready = orchestrator.process_signals()

    assert [signal.market_id for signal in ready] == ["market-2", "market-1"]


def test_process_signals_reallocates_idle_budget_for_small_bankroll():
    orchestrator = StrategyOrchestrator(total_bankroll=3.0)
    stat_signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_no",
        market_id="market-stat",
        description="stat",
        expected_edge=250.0,
        confidence=0.8,
        recommended_size_usdc=1.0,
    )
    maker_signal = StrategySignal(
        tier=StrategyTier.MARKET_MAKING,
        signal_type="maker_quote",
        market_id="market-maker",
        description="maker",
        expected_edge=500.0,
        confidence=0.5,
        recommended_size_usdc=1.0,
        urgency=0.2,
    )

    orchestrator.submit_signal(stat_signal)
    orchestrator.submit_signal(maker_signal)
    ready = orchestrator.process_signals()

    assert {signal.market_id for signal in ready} == {"market-stat", "market-maker"}
    assert orchestrator.get_last_skip_reasons()["total"] == 0


def test_record_processed_prunes_executed_signal_history():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)

    for idx in range(2505):
        orchestrator.record_processed(
            StrategySignal(
                tier=StrategyTier.STATISTICAL_ARB,
                signal_type=f"signal-{idx}",
                market_id=f"market-{idx}",
                description="signal",
                expected_edge=10.0,
                confidence=0.5,
                recommended_size_usdc=10.0,
            )
        )

    status = orchestrator.get_status()

    assert status["meta"]["executed_signals"] == 2000
