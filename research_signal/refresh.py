"""CLI entrypoint for refreshing research signals."""

from __future__ import annotations

import argparse
import json

from polymarket_arb.config import ArbConfig
from polymarket_arb.market_scanner import MarketScanner
from research_signal.service import ResearchSignalService


def _parse_extra_rss_feeds(raw: str) -> list[tuple[str, str]]:
    feeds: list[tuple[str, str]] = []
    for idx, item in enumerate(raw.split(","), start=1):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, template = item.split("=", 1)
            name = name.strip() or f"rss_feed_{idx}"
        else:
            name, template = f"rss_feed_{idx}", item
        template = template.strip()
        if template:
            feeds.append((name, template))
    return feeds


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh research signals")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    config = ArbConfig.from_env()
    scanner = MarketScanner(config)
    markets = scanner.fetch_active_markets(limit=args.limit)
    service = ResearchSignalService(
        max_items=config.research_signal_max_items,
        cache_ttl_sec=config.research_signal_cache_ttl_sec,
        cache_dir=config.research_signal_cache_dir,
        extra_rss_feeds=_parse_extra_rss_feeds(config.research_signal_extra_rss_feeds),
        knowledge_base_dir=config.research_signal_knowledge_dir,
        knowledge_base_enabled=config.research_signal_knowledge_enabled,
        knowledge_max_matches=config.research_signal_knowledge_max_matches,
    )
    report = service.collect_report(markets, config.research_signal_window_sec)
    print(json.dumps(report.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
