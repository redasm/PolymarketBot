"""CLI entrypoint for refreshing research signals."""

from __future__ import annotations

import argparse
import json

from polymarket_arb.config import ArbConfig
from polymarket_arb.market_scanner import MarketScanner
from research_signal.service import ResearchSignalService


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
        feeds_file=config.research_signal_feeds_file,
        crypto_macro_enabled=config.research_signal_crypto_macro_enabled,
        coingecko_enabled=config.research_signal_coingecko_enabled,
        funding_rate_enabled=config.research_signal_funding_rate_enabled,
        econ_calendar_enabled=config.research_signal_econ_calendar_enabled,
        defillama_enabled=config.research_signal_defillama_enabled,
        polymarket_activity_enabled=config.research_signal_polymarket_activity_enabled,
        manifold_enabled=config.research_signal_manifold_enabled,
    )
    report = service.collect_report(markets, config.research_signal_window_sec)
    print(json.dumps(report.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
