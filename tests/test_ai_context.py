"""AI context tests for research signal integration."""

from polymarket_arb.ai_context import MarketContextBuilder
from polymarket_arb.models import MarketInfo, ResearchSignal, ResearchSignalReport, TokenInfo


def test_market_context_builder_includes_research_signals():
    builder = MarketContextBuilder()
    report = ResearchSignalReport(
        generated_at=1.0,
        window_sec=86400,
        market_count=1,
        row_count=4,
        topic_count=2,
        source_counts={"polymarket_market": 1, "google_news_rss": 3},
        cache_hit=True,
        signals=[
            ResearchSignal(
                topic_id="btc go up",
                summary="BTC sentiment improving across sources",
                sources=["polymarket_market", "google_news_rss"],
                confidence=0.7,
                freshness_sec=120.0,
                stance="bullish",
            )
        ],
    )
    ctx = builder.build(
        active_markets=[
            MarketInfo(
                condition_id="c1",
                question="Will BTC go up?",
                slug="btc",
                tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            )
        ],
        research_report=report,
        research_signals=report.signals,
    )

    assert ctx.research_signals
    assert ctx.research_overview["signal_count"] == 1
    assert ctx.research_overview["cache_hit"] is True
    prompt = builder.to_prompt_text(ctx)
    assert "Research Overview" in prompt
    assert "Research Signals" in prompt
    assert "source_mix" in prompt
