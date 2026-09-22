"""Research signal subsystem tests."""

import json
from pathlib import Path

import requests

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import MarketInfo, TokenInfo
from research_signal.service import ResearchSignalService
from research_signal.collectors.base import (
    GenericHTTPJSONCollector,
    GenericRSSCollector,
    WebSearchCollector,
)
from research_signal.scorers.scoring import compute_confidence


def test_research_signal_groups_related_rows_into_topics():
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        ),
        MarketInfo(
            condition_id="c2",
            question="Will BTC go up this week?",
            slug="btc-up-2",
            tokens=[TokenInfo(token_id="t2", outcome="Yes")],
            event_id="e1",
        ),
    ]

    service = ResearchSignalService(max_items=5)
    service._web_collector.collect = lambda topics: []
    report = service.collect_report(markets, 86400)
    signals = report.signals

    assert len(signals) >= 1
    assert signals[0].topic_id
    assert "polymarket_market" in signals[0].sources
    assert report.topic_count >= 1


def test_research_signal_limits_output_count():
    markets = [
        MarketInfo(
            condition_id=f"c{i}",
            question=f"Question {i}?",
            slug=f"q-{i}",
            tokens=[TokenInfo(token_id=f"t{i}", outcome="Yes")],
            event_id=f"e{i}",
        )
        for i in range(10)
    ]

    service = ResearchSignalService(max_items=3)
    service._web_collector.collect = lambda topics: []
    signals = service.get_signals(markets, 86400)

    assert len(signals) <= 3


def test_web_search_collector_parses_google_news_rss(monkeypatch):
    class _Resp:
        text = """
        <rss><channel>
          <item><title>BTC rallies on ETF optimism</title><link>https://example.com/1</link><pubDate>Mon</pubDate></item>
          <item><title>Polymarket traders turn bullish on BTC</title><link>https://example.com/2</link><pubDate>Tue</pubDate></item>
        </channel></rss>
        """

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Resp())
    collector = WebSearchCollector(max_items_per_topic=2)

    rows = collector.collect(["Will BTC go up this week?"])

    assert len(rows) == 2
    assert rows[0]["source"] == "google_news_rss"
    assert "BTC" in rows[0]["summary"]
    assert "published_ts" in rows[0]


def test_generic_rss_collector_parses_custom_feed(monkeypatch):
    class _Resp:
        text = """
        <rss><channel>
          <item><title>Custom feed says BTC is moving</title><link>https://example.com/custom</link><pubDate>Mon</pubDate></item>
        </channel></rss>
        """

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Resp())
    collector = GenericRSSCollector(
        feeds=[("custom_rss", "https://example.com/rss?q={query}")],
        max_items_per_feed=1,
    )

    rows = collector.collect(["Will BTC go up this week?"])

    assert len(rows) == 1
    assert rows[0]["source"] == "custom_rss"
    assert "BTC" in rows[0]["summary"]


def test_generic_http_json_collector_parses_custom_api(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "articles": [
                    {
                        "headline": "BTC rallies on ETF demand",
                        "url": "https://example.com/btc",
                        "published_at": "2099-04-10T00:00:00Z",
                    }
                ]
            }

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Resp())
    collector = GenericHTTPJSONCollector(
        sources=[
            {
                "name": "json_news",
                "url": "https://example.com/news",
                "items_path": "articles",
                "summary_path": "headline",
                "link_path": "url",
                "published_path": "published_at",
            }
        ],
        max_items_per_source=1,
    )

    rows = collector.collect(["Will BTC go up this week?"])

    assert len(rows) == 1
    assert rows[0]["source"] == "json_news"
    assert "BTC" in rows[0]["summary"]
    assert rows[0]["published_ts"] is not None


def test_research_signal_service_reloads_feeds_when_file_changes(tmp_path: Path, monkeypatch):
    feeds_file = tmp_path / "feeds.json"
    feeds_file.write_text(
        json.dumps(
            {
                "generated_at": 1.0,
                "feeds": [{"name": "alpha", "url_template": "https://alpha.example/rss?q={query}"}],
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, list[tuple[str, str]]] = {"feeds": []}

    class _StubCollector:
        def __init__(self, feeds):
            captured["feeds"] = list(feeds)

        def collect(self, topics):
            return []

    monkeypatch.setattr("research_signal.service.GenericRSSCollector", _StubCollector)

    service = ResearchSignalService(
        max_items=2,
        cache_ttl_sec=300,
        cache_dir=str(tmp_path / "cache"),
        feeds_file=str(feeds_file),
    )

    assert captured["feeds"] == [("alpha", "https://alpha.example/rss?q={query}")]

    feeds_file.write_text(
        json.dumps(
            {
                "generated_at": 2.0,
                "feeds": [
                    {"name": "alpha", "url_template": "https://alpha.example/rss?q={query}"},
                    {"name": "beta", "url_template": "https://beta.example/rss?q={query}"},
                ],
            }
        ),
        encoding="utf-8",
    )
    import os
    new_ts = service._feeds_mtime + 5.0
    os.utime(feeds_file, (new_ts, new_ts))

    service._maybe_reload_feeds()

    assert captured["feeds"] == [
        ("alpha", "https://alpha.example/rss?q={query}"),
        ("beta", "https://beta.example/rss?q={query}"),
    ]


def test_research_signal_service_skips_feeds_missing_query_placeholder(tmp_path: Path, monkeypatch):
    feeds_file = tmp_path / "feeds.json"
    feeds_file.write_text(
        json.dumps(
            {
                "feeds": [
                    {"name": "good", "url_template": "https://good.example/rss?q={query}"},
                    {"name": "bad", "url_template": "https://bad.example/static-rss"},
                ]
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, list[tuple[str, str]]] = {"feeds": []}

    class _StubCollector:
        def __init__(self, feeds):
            captured["feeds"] = list(feeds)

        def collect(self, topics):
            return []

    monkeypatch.setattr("research_signal.service.GenericRSSCollector", _StubCollector)

    ResearchSignalService(
        max_items=2,
        cache_ttl_sec=300,
        cache_dir=str(tmp_path / "cache"),
        feeds_file=str(feeds_file),
    )

    assert captured["feeds"] == [("good", "https://good.example/rss?q={query}")]


def test_research_signal_service_uses_cache(tmp_path: Path, monkeypatch):
    class _Resp:
        text = """
        <rss><channel>
          <item><title>BTC rallies on ETF optimism</title><link>https://example.com/1</link><pubDate>Mon</pubDate></item>
        </channel></rss>
        """

        def raise_for_status(self):
            return None

    call_count = {"n": 0}

    def fake_get(*args, **kwargs):
        call_count["n"] += 1
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        )
    ]

    service = ResearchSignalService(max_items=5, cache_ttl_sec=300, cache_dir=str(tmp_path))
    first = service.collect_report(markets, 86400)
    second = service.collect_report(markets, 86400)

    assert first.signals
    assert second.signals
    assert call_count["n"] == 1
    assert second.cache_hit is True


def test_research_signal_service_uses_disk_cache(tmp_path: Path, monkeypatch):
    class _Resp:
        text = """
        <rss><channel>
          <item><title>BTC rallies on ETF optimism</title><link>https://example.com/1</link><pubDate>Mon</pubDate></item>
        </channel></rss>
        """

        def raise_for_status(self):
            return None

    call_count = {"n": 0}

    def fake_get(*args, **kwargs):
        call_count["n"] += 1
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        )
    ]

    service = ResearchSignalService(max_items=5, cache_ttl_sec=300, cache_dir=str(tmp_path))
    first = service.collect_report(markets, 86400)
    assert first.signals
    assert call_count["n"] == 1

    service2 = ResearchSignalService(max_items=5, cache_ttl_sec=300, cache_dir=str(tmp_path))
    second = service2.collect_report(markets, 86400)

    assert second.signals
    assert call_count["n"] == 1
    assert second.cache_hit is True


def test_research_signal_service_dedupes_duplicate_news_rows(tmp_path: Path, monkeypatch):
    class _Resp:
        text = """
        <rss><channel>
          <item><title>BTC rallies on ETF optimism</title><link>https://example.com/1</link><pubDate>Mon, 10 Apr 2099 00:00:00 GMT</pubDate></item>
          <item><title>BTC rallies on ETF optimism</title><link>https://example.com/1</link><pubDate>Mon, 10 Apr 2099 00:00:00 GMT</pubDate></item>
        </channel></rss>
        """

        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Resp())
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        )
    ]

    service = ResearchSignalService(max_items=5, cache_ttl_sec=300, cache_dir=str(tmp_path))
    report = service.collect_report(markets, 86400)

    assert report.row_count == 2
    assert report.dropped_rows >= 1
    assert report.source_counts["google_news_rss"] == 1
    assert any(signal.metadata["sample_size"] == 1 for signal in report.signals)


def test_research_signal_service_dedupes_cross_source_same_headline(tmp_path: Path):
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        )
    ]
    service = ResearchSignalService(max_items=5, cache_ttl_sec=300, cache_dir=str(tmp_path))
    service._web_collector.collect = lambda topics: [
        {
            "topic": topics[0],
            "summary": "BTC rallies on ETF optimism",
            "source": "google_news_rss",
            "published_ts": 4102444800,
            "link": "https://news.google.com/articles/abc",
        }
    ]
    service._generic_rss_collector.collect = lambda topics: [
        {
            "topic": topics[0],
            "summary": "BTC rallies on ETF optimism",
            "source": "reuters_rss",
            "published_ts": 4102444801,
            "link": "https://www.reuters.com/world/us/btc-etf-story",
        }
    ]

    report = service.collect_report(markets, 86400)

    assert report.dropped_rows >= 1
    assert report.row_count == 2
    assert "reuters_rss" in report.source_counts or "google_news_rss" in report.source_counts


def test_compute_confidence_rewards_higher_quality_sources():
    low_quality_rows = [
        {"source": "google_news_rss", "summary": "BTC jumps", "published_ts": 4102444800, "link": "https://news.google.com/1"},
        {"source": "custom_rss", "summary": "BTC gains", "published_ts": 4102444800, "link": "https://example.com/2"},
    ]
    high_quality_rows = [
        {"source": "reuters", "summary": "BTC jumps", "published_ts": 4102444800, "link": "https://reuters.com/1"},
        {"source": "bloomberg", "summary": "BTC gains", "published_ts": 4102444800, "link": "https://bloomberg.com/2"},
    ]

    assert compute_confidence(high_quality_rows) > compute_confidence(low_quality_rows)


def test_attach_to_markets_matches_on_topic_overlap():
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC break 100k before June?",
            slug="btc-100k",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="",
        )
    ]
    service = ResearchSignalService(max_items=5)
    service._web_collector.collect = lambda topics: []
    signals = service.get_signals(markets, 86400)

    enriched = service.attach_to_markets(markets, signals)

    assert enriched[0].raw["research_signals"]


def test_research_signal_signal_metadata_includes_source_profiles(tmp_path: Path):
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC hit 100k before June?",
            slug="btc-100k",
            tokens=[TokenInfo(token_id="t1", outcome="Yes")],
            event_id="e1",
        )
    ]
    service = ResearchSignalService(max_items=5, cache_ttl_sec=300, cache_dir=str(tmp_path))
    service._web_collector.collect = lambda topics: [
        {
            "topic": topics[0],
            "summary": "BTC rallies as ETF momentum builds",
            "source": "google_news_rss",
            "published_ts": 4102444800,
            "link": "https://news.google.com/abc",
        }
    ]

    report = service.collect_report(markets, 86400)

    assert report.signals
    metadata = report.signals[0].metadata
    assert "source_profiles" in metadata
    assert "source_types" in metadata
    assert metadata["source_profiles"]["google_news_rss"]["type"] == "aggregator"


def test_config_can_load_in_research_mode_without_wallet(tmp_path: Path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("ARB_DRY_RUN=true\n", encoding="utf-8")
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)

    cfg = ArbConfig.from_env(env_path, require_wallet=False)

    assert cfg.private_key == "research-mode"
    assert cfg.funder_address == "research-mode"
