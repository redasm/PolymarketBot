"""CLI tests for manual research runner."""

from __future__ import annotations

import sys

from polymarket_arb.models import MarketInfo, ResearchSignal, ResearchSignalReport, TokenInfo
import run_research


def test_run_research_outputs_json(monkeypatch, capsys):
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
                summary="BTC traders turn bullish",
                sources=["polymarket_market", "google_news_rss"],
                confidence=0.7,
                freshness_sec=30.0,
                stance="bullish",
            )
        ],
    )

    monkeypatch.setattr(
        run_research.ArbConfig,
        "from_env",
        lambda *args, **kwargs: type(
            "Cfg",
            (),
            {
                "log_level": "WARNING",
                "log_file": "",
                "gamma_host": "https://gamma-api.polymarket.com",
                "research_signal_max_items": 5,
                "research_signal_cache_ttl_sec": 300,
                "research_signal_cache_dir": "data/research_signal",
                "research_signal_feeds_file": "data/quant_inputs/research_feeds.json",
                "research_signal_crypto_macro_enabled": False,
                "research_signal_coingecko_enabled": False,
                "research_signal_funding_rate_enabled": False,
                "research_signal_econ_calendar_enabled": False,
                "research_signal_defillama_enabled": False,
                "research_signal_polymarket_activity_enabled": False,
                "research_signal_window_sec": 86400,
            },
        )(),
    )
    monkeypatch.setattr(run_research.MarketScanner, "fetch_active_markets", lambda self, limit: markets)
    monkeypatch.setattr(run_research.ResearchSignalService, "collect_report", lambda self, markets, window_sec: report)
    monkeypatch.setattr(sys, "argv", ["run_research.py", "--json"])

    exit_code = run_research.main()

    out = capsys.readouterr().out
    assert exit_code == 0
    assert '"matched_market_count": 1' in out
    assert '"topic_id": "event:e1"' in out
