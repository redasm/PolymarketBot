#!/usr/bin/env python3
"""研究信号调试入口：手动拉取市场并生成 research report."""

from __future__ import annotations

import argparse
import json
from typing import Iterable

from polymarket_arb.config import ArbConfig
from polymarket_arb.logger_setup import setup_logging
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.models import MarketInfo, ResearchSignalReport
from research_signal.service import ResearchSignalService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket research signal runner")
    parser.add_argument("--dotenv", default=None, help="Path to .env file")
    parser.add_argument("--limit", type=int, default=25, help="How many active markets to fetch before filtering")
    parser.add_argument("--query", default="", help="Filter markets by question / slug / event slug")
    parser.add_argument("--window-sec", type=int, default=None, help="Research freshness window in seconds")
    parser.add_argument("--max-signals", type=int, default=None, help="Max number of summarized signals")
    parser.add_argument("--show-markets", action="store_true", help="Print matched markets before the report")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def _filter_markets(markets: list[MarketInfo], query: str) -> list[MarketInfo]:
    query_norm = query.strip().lower()
    if not query_norm:
        return markets
    filtered = []
    for market in markets:
        haystacks = (market.question, market.slug, market.event_slug)
        if any(query_norm in (field or "").lower() for field in haystacks):
            filtered.append(market)
    return filtered


def _signal_preview(report: ResearchSignalReport) -> list[dict]:
    return [signal.to_dict() for signal in report.signals]


def _print_markets(markets: Iterable[MarketInfo]) -> None:
    for idx, market in enumerate(markets, start=1):
        print(
            f"{idx:>2}. {market.question} | event={market.event_id or '-'} | "
            f"liq=${market.liquidity:.0f} | vol24h=${market.volume_24h:.0f}"
        )


def _print_report(report: ResearchSignalReport) -> None:
    print(
        f"Research report: signals={len(report.signals)}, topics={report.topic_count}, "
        f"rows={report.row_count}, dropped={report.dropped_rows}, cache_hit={report.cache_hit}"
    )
    if report.source_counts:
        source_summary = ", ".join(f"{name}={count}" for name, count in sorted(report.source_counts.items()))
        print(f"Sources: {source_summary}")
    for idx, signal in enumerate(report.signals, start=1):
        evidence = signal.metadata.get("evidence", [])
        print(
            f"{idx:>2}. {signal.topic_id} | stance={signal.stance} | "
            f"conf={signal.confidence:.2f} | freshness={signal.freshness_sec:.0f}s"
        )
        print(f"    {signal.summary}")
        if evidence:
            print(f"    evidence={len(evidence)} items")


def main() -> int:
    args = _build_parser().parse_args()
    config = ArbConfig.from_env(args.dotenv, require_wallet=False)
    setup_logging(config.log_level, config.log_file)

    scanner = MarketScanner(config)
    fetched_markets = scanner.fetch_active_markets(limit=max(1, args.limit))
    markets = _filter_markets(fetched_markets, args.query)
    if args.max_signals is not None:
        max_items = max(1, args.max_signals)
    else:
        max_items = config.research_signal_max_items

    service = ResearchSignalService(
        max_items=max_items,
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
    report = service.collect_report(
        markets,
        window_sec=args.window_sec or config.research_signal_window_sec,
    )

    if args.json:
        payload = {
            "matched_market_count": len(markets),
            "markets": [
                {
                    "condition_id": market.condition_id,
                    "event_id": market.event_id,
                    "question": market.question,
                    "slug": market.slug,
                    "event_slug": market.event_slug,
                    "liquidity": market.liquidity,
                    "volume_24h": market.volume_24h,
                }
                for market in markets[:max_items]
            ],
            "report": report.to_dict(),
            "signals": _signal_preview(report),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Matched markets: {len(markets)} / fetched {len(fetched_markets)}")
    if args.show_markets:
        _print_markets(markets[:max_items])
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
