"""MarketScanner enrichment tests."""

from types import SimpleNamespace

from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.models import MarketInfo, ResearchSignal, ResearchSignalReport, TokenInfo
from research_signal.service import ResearchSignalService

from tests.conftest import make_test_config


def test_market_scanner_enriches_markets_with_research_signals():
    scanner = MarketScanner(make_test_config())
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        )
    ]

    enriched = scanner.enrich_markets_with_research(
        markets,
        ResearchSignalService(max_items=3),
        window_sec=86400,
    )

    assert enriched[0].raw.get("research_signals")


def test_market_scanner_enrichment_can_reuse_collected_report(monkeypatch):
    scanner = MarketScanner(make_test_config())
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        )
    ]
    report = ResearchSignalReport(
        generated_at=1.0,
        window_sec=86400,
        market_count=1,
        row_count=2,
        topic_count=1,
        source_counts={"polymarket_market": 1, "google_news_rss": 1},
        signals=[
            ResearchSignal(
                topic_id="event:e1",
                event_candidates=["e1"],
                summary="BTC sentiment improving across sources",
                sources=["polymarket_market", "google_news_rss"],
                confidence=0.7,
                freshness_sec=100.0,
                stance="bullish",
            )
        ],
    )

    dummy_service = SimpleNamespace(
        attach_to_markets=lambda markets, signals: ResearchSignalService().attach_to_markets(markets, signals)
    )
    get_signals_called = {"value": False}

    def fail_get_signals(*args, **kwargs):
        get_signals_called["value"] = True
        raise AssertionError("get_signals should not be called when report is provided")

    dummy_service.get_signals = fail_get_signals

    enriched = scanner.enrich_markets_with_research(
        markets,
        dummy_service,  # type: ignore[arg-type]
        report=report,
    )

    assert enriched[0].raw.get("research_signals")
    assert get_signals_called["value"] is False


def test_market_scanner_enrichment_can_clear_stale_research_rows():
    scanner = MarketScanner(make_test_config())
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
            raw={"research_signals": [{"topic_id": "old"}]},
        )
    ]

    dummy_service = SimpleNamespace(
        attach_to_markets=lambda markets, signals: ResearchSignalService().attach_to_markets(markets, signals)
    )

    enriched = scanner.enrich_markets_with_research(
        markets,
        dummy_service,  # type: ignore[arg-type]
        signals=[],
    )

    assert enriched[0].raw["research_signals"] == []
