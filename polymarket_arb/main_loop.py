"""主循环：协调市场扫描、套利检测、执行和通知的完整流程.

循环步骤:
1. 从 Gamma API 拉取活跃市场列表
2. 对每个二元市场执行快速套利扫描（best ask 级别）
3. 对多结果事件执行多腿套利扫描
4. 对发现的机会用 VWAP 做深度验证
5. 风控预检查
6. 执行交易（或 dry-run 记录）
7. 发送飞书通知
8. 休眠后重复
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import importlib
import json
import logging
import os
import re
import signal
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from polymarket_arb.ai_advisor import AIAdvisor, create_ai_advisor
from polymarket_arb.ai_context import MarketContextBuilder
from polymarket_arb.arbitrage_detector import (
    ArbitrageDetector,
    format_arb_opportunity_zh,
)
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.client_factory import build_readonly_client, build_trading_client
from polymarket_arb.config import ArbConfig
from polymarket_arb.data_janitor import DataJanitor
from polymarket_arb.dashboard_api import DashboardState, start_dashboard_server
from polymarket_arb.edge_engine import EdgeEngine
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.logger_setup import setup_logging
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    MarketInfo,
    OrderSide,
    ResearchSignalReport,
    TradeRecord,
    TradeStatus,
)
from polymarket_arb.notifier import NotificationManager
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.portfolio_sync import PortfolioSync
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.cross_platform import CrossPlatformScanner, KalshiClient
from polymarket_arb.strategies.maker_strategy import DynamicSpreadCalculator, MakerStrategy
from polymarket_arb.strategies.statistical_model import StatisticalMispricingDetector
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)
from polymarket_arb.tick_recorder import TickRecorder
from polymarket_arb.volatility_estimator import VolEstimator
from polymarket_arb.websocket_feed import OrderBookMirror, WebSocketFeed

if TYPE_CHECKING:
    from research_signal.service import ResearchSignalService

LOG = logging.getLogger("main_loop")

_SHUTDOWN_EVENT = threading.Event()
_AI_EVAL_TIMEOUT_SEC = 20.0
_TELEMETRY_HEARTBEAT_SEC = 60.0
_RESEARCH_RESUBMIT_COOLDOWN_SEC = 30.0
_FOCUS_ALIASES = {
    "btc": ("btc", "bitcoin"),
    "eth": ("eth", "ethereum"),
    "sol": ("sol", "solana"),
    "arb": ("arb", "arbitrum"),
}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_DEADLINE_RE = re.compile(
    r"\b(?:by|before)\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)


def _signal_handler(sig: int, frame: Any) -> None:
    LOG.info("收到信号 %d，准备优雅退出…", sig)
    _SHUTDOWN_EVENT.set()


def _build_run_instance_id(*, now_ts: float | None = None) -> str:
    ts = time.gmtime(now_ts if now_ts is not None else time.time())
    return f"run-{os.getpid()}-{time.strftime('%Y%m%dT%H%M%SZ', ts)}"


def _log_startup_summary(config: ArbConfig, run_id: str) -> None:
    safe_cfg = config.dump_safe()
    LOG.info("=" * 60)
    LOG.info("Polymarket 套利机器人启动")
    LOG.info("实例: run_id=%s pid=%d", run_id, os.getpid())
    LOG.info("模式: %s", "DRY RUN (仅扫描)" if config.dry_run else "LIVE (实盘交易)")
    LOG.info(
        "基础: clob=%s gamma=%s funder=%s",
        safe_cfg.get("clob_host"),
        safe_cfg.get("gamma_host"),
        safe_cfg.get("funder_address"),
    )
    LOG.info(
        "交易: edge>=$%.4f / %.2f%% scan=%.1fs universe_refresh=%.0fs hot_markets=%d hot_events=%d focus=%s order=$%.2f..$%.2f liquidity>=$%.0f vol24h>=$%.0f",
        config.min_edge_usd,
        config.min_edge_pct,
        config.scan_interval_sec,
        config.market_universe_refresh_sec,
        config.hot_market_pool_size,
        config.hot_event_pool_size,
        config.market_focus_keywords or "ALL",
        config.default_order_size_usdc,
        config.max_order_size_usdc,
        config.min_liquidity,
        config.min_volume_24h,
    )
    LOG.info(
        "风控: open_positions<=%d exposure_per_market<=%.2f total_exposure<=%.2f daily_loss<=%.2f failures<=%d cooldown=%.0fs",
        config.max_open_positions,
        config.max_exposure_per_market,
        config.max_total_exposure,
        config.max_daily_loss,
        config.max_consecutive_failures,
        config.risk_event_cooldown_sec,
    )
    LOG.info(
        "数据: ws=%s ws_markets=%d tick_record=%s telemetry=%s cleanup=%s portfolio_sync=%s/%.0fs",
        config.ws_enabled,
        config.ws_max_markets,
        config.tick_record_enabled,
        config.telemetry_record_enabled,
        config.data_cleanup_enabled,
        config.portfolio_sync_enabled,
        config.portfolio_sync_interval_sec,
    )
    LOG.info(
        "研究/AI: research=%s knowledge=%s ai=%s provider=%s model=%s",
        config.research_signal_enabled,
        config.research_signal_knowledge_enabled,
        config.ai_enabled,
        config.ai_provider,
        config.ai_model,
    )
    LOG.info("=" * 60)


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
        if not template:
            continue
        feeds.append((name, template))
    return feeds


def _parse_http_json_sources(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        LOG.error("HTTP JSON sources 配置解析失败: %s", e)
        return []
    if not isinstance(payload, list):
        LOG.error("HTTP JSON sources 配置必须是 JSON list")
        return []
    return [item for item in payload if isinstance(item, dict)]


def _create_research_signal_service(config: ArbConfig) -> Optional["ResearchSignalService"]:
    if not config.research_signal_enabled:
        return None

    try:
        module = importlib.import_module("research_signal.service")
        service_cls = getattr(module, "ResearchSignalService")
    except Exception as e:
        LOG.error("Research Signal 功能已禁用: 模块导入失败: %s", e, exc_info=True)
        return None

    try:
        return service_cls(
            max_items=config.research_signal_max_items,
            cache_ttl_sec=config.research_signal_cache_ttl_sec,
            cache_dir=config.research_signal_cache_dir,
            extra_rss_feeds=_parse_extra_rss_feeds(config.research_signal_extra_rss_feeds),
            http_json_sources=_parse_http_json_sources(config.research_signal_http_json_sources),
            surf_enabled=config.research_signal_surf_enabled,
            surf_api_key=config.research_signal_surf_api_key,
            surf_api_base=config.research_signal_surf_api_base,
            surf_model=config.research_signal_surf_model,
            surf_timeout_sec=config.research_signal_surf_timeout_sec,
            surf_cache_ttl_sec=config.research_signal_surf_cache_ttl_sec,
            knowledge_base_dir=config.research_signal_knowledge_dir,
            knowledge_base_enabled=config.research_signal_knowledge_enabled,
            knowledge_max_matches=config.research_signal_knowledge_max_matches,
        )
    except Exception as e:
        LOG.error("Research Signal 功能已禁用: 初始化失败: %s", e, exc_info=True)
        return None


def _create_cross_platform_scanner(config: ArbConfig, ob_analyzer: OrderBookAnalyzer) -> CrossPlatformScanner | None:
    raw = (config.cross_platform_pairs_json or "").strip()
    if not raw:
        return None

    try:
        pairs = json.loads(raw)
    except json.JSONDecodeError as e:
        LOG.error("跨平台配对配置解析失败: %s", e)
        return None
    if not isinstance(pairs, list):
        LOG.error("跨平台配对配置必须是 JSON list")
        return None

    scanner = CrossPlatformScanner(KalshiClient(), ob_analyzer)
    scanner.load_pairs_from_config([item for item in pairs if isinstance(item, dict)])
    LOG.info("跨平台扫描器已启用: 配对=%d", len(getattr(scanner, "_pairs", [])))
    return scanner


def _select_ws_targets(
    markets: list[MarketInfo],
    max_count: int,
) -> list[MarketInfo]:
    """从扫描结果中选出最适合 WebSocket 追踪的二元市场.

    排序策略: volume_24h * liquidity 联合打分，取 top-N。
    """
    binary = [m for m in markets if len(m.tokens) == 2 and not m.closed]
    binary.sort(key=lambda m: m.volume_24h * m.liquidity, reverse=True)
    return binary[:max_count]


def _focus_keywords(raw: str) -> list[str]:
    return [item.strip().lower() for item in (raw or "").split(",") if item.strip()]


def _market_focus_text(market: MarketInfo) -> str:
    parts = [
        market.question,
        market.slug,
        market.event_slug,
        getattr(market, "event_title", ""),
        getattr(market, "event_ticker", ""),
        str((market.raw or {}).get("description") or ""),
        " ".join(market.outcomes or []),
        " ".join((token.outcome or "") for token in market.tokens),
    ]
    return " ".join(part for part in parts if part).lower()


def _event_focus_text(event: Any) -> str:
    parts = [getattr(event, "title", ""), getattr(event, "slug", "")]
    for market in getattr(event, "markets", []) or []:
        parts.append(getattr(market, "question", ""))
        parts.append(getattr(market, "slug", ""))
    return " ".join(part for part in parts if part).lower()


def _matches_focus(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    normalized_tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", text.lower())
        if token
    ]
    for keyword in keywords:
        aliases = _FOCUS_ALIASES.get(keyword)
        if aliases is not None:
            if any(token == alias or token.startswith(f"{alias}-") for alias in aliases for token in normalized_tokens):
                return True
            continue
        if len(keyword) <= 3:
            if keyword in normalized_tokens:
                return True
            continue
        if any(token == keyword or token.startswith(keyword) for token in normalized_tokens):
            return True
    return False


def _collect_cross_platform_strategy_signals(
    *,
    config: ArbConfig,
    scanner: Any | None,
) -> list[StrategySignal]:
    if scanner is None:
        return []

    signals: list[StrategySignal] = []
    for opp in scanner.scan():
        signals.append(
            StrategySignal(
                tier=StrategyTier.CROSS_PLATFORM,
                signal_type=f"cross_platform_{opp.direction}",
                market_id=opp.pair.polymarket_condition_id,
                description=opp.pair.event_description[:120],
                expected_edge=opp.edge_pct * 100.0,
                confidence=opp.confidence,
                recommended_size_usdc=config.default_order_size_usdc,
                urgency=0.9,
                payload={
                    "direction": opp.direction,
                    "pair_id": opp.pair.pair_id,
                    "event_description": opp.pair.event_description,
                    "poly_cost": opp.poly_cost,
                    "kalshi_cost": opp.kalshi_cost,
                    "total_cost": opp.total_cost,
                    "net_edge": opp.net_edge,
                    "edge_pct": opp.edge_pct,
                },
            )
        )
    return signals


def _collect_statistical_strategy_signals(
    *,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    detector: StatisticalMispricingDetector,
)-> list[StrategySignal]:
    signals: list[StrategySignal] = []
    related_market_context = _build_t2_related_market_context(candidate_markets, ob_analyzer)
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            continue

        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        no_token = next((t for t in market.tokens if (t.outcome or "").lower() == "no"), market.tokens[-1])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        no_snap = ob_analyzer.get_snapshot(no_token.token_id)
        if snap is None or no_snap is None or snap.mid is None or no_snap.mid is None:
            continue
        quality = _evaluate_t2_market_quality(config=config, snap=snap, no_snap=no_snap)
        if quality["passes"] is False:
            continue

        bids_total_size = sum(level.size for level in snap.bids[:5])
        asks_total_size = sum(level.size for level in snap.asks[:5])
        estimate = detector.analyze(
            market_id=market.condition_id,
            outcome="YES",
            market_price=float(snap.mid),
            bids_total_size=bids_total_size,
            asks_total_size=asks_total_size,
            mid_price=float(snap.mid),
            related_market_prices=related_market_context.get(market.condition_id),
        )
        if estimate is None:
            continue

        action = "buy_yes" if estimate.is_underpriced else "buy_no"
        signals.append(
            StrategySignal(
                tier=StrategyTier.STATISTICAL_ARB,
                signal_type=f"statistical_{action}",
                market_id=market.condition_id,
                description=f"{market.question[:80]} | deviation={estimate.deviation:+.4f}",
                expected_edge=estimate.abs_edge * 10_000.0,
                confidence=estimate.confidence,
                recommended_size_usdc=config.default_order_size_usdc,
                urgency=min(1.0, 0.5 + estimate.confidence * 0.4),
                payload={
                    "outcome": estimate.outcome,
                    "model_prob": estimate.model_prob,
                    "market_prob": estimate.market_prob,
                    "deviation": estimate.deviation,
                    "deviation_pct": estimate.deviation_pct,
                    "signals": dict(estimate.signals),
                    "quality": quality,
                    "related_context_count": len(related_market_context.get(market.condition_id, {})),
                },
            )
        )
    return signals


def _extract_market_temporal_stem(question: str) -> str:
    normalized = re.sub(r"\s+", " ", (question or "")).strip().rstrip("?").strip().lower()
    normalized = re.sub(r"\bwill\s+", "", normalized)
    normalized = re.sub(r"\s+(?:by|before)\s+.+$", "", normalized)
    return normalized.strip(" .!?")


def _extract_market_deadline(question: str) -> datetime | None:
    match = _DEADLINE_RE.search(question or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    day = int(match.group(2))
    year = int(match.group(3))
    if month is None:
        return None
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _build_t2_related_market_context(
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
) -> dict[str, dict[str, Any]]:
    yes_mid_by_market: dict[str, float] = {}
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            continue
        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        if snap is None or snap.mid is None:
            continue
        yes_mid_by_market[market.condition_id] = float(snap.mid)

    contexts: dict[str, dict[str, Any]] = {cid: {} for cid in yes_mid_by_market}
    ladder_groups: dict[tuple[str, str], list[tuple[datetime, MarketInfo]]] = {}

    for market in candidate_markets:
        if market.condition_id not in yes_mid_by_market:
            continue
        stem = _extract_market_temporal_stem(market.question)
        deadline = _extract_market_deadline(market.question)
        if not stem or deadline is None:
            continue
        group_key = (market.event_id or market.event_slug or stem, stem)
        ladder_groups.setdefault(group_key, []).append((deadline, market))

    for ladder in ladder_groups.values():
        ladder.sort(key=lambda item: item[0])
        for idx, (_, market) in enumerate(ladder):
            current = contexts.setdefault(market.condition_id, {})
            if idx > 0:
                prev_market = ladder[idx - 1][1]
                prev_price = yes_mid_by_market.get(prev_market.condition_id)
                if prev_price is not None:
                    current[prev_market.condition_id] = {
                        "price": prev_price,
                        "relation": "lower_bound",
                        "weight": 1.35,
                    }
            if idx + 1 < len(ladder):
                next_market = ladder[idx + 1][1]
                next_price = yes_mid_by_market.get(next_market.condition_id)
                if next_price is not None:
                    current[next_market.condition_id] = {
                        "price": next_price,
                        "relation": "upper_bound",
                        "weight": 1.35,
                    }

    return {cid: ctx for cid, ctx in contexts.items() if ctx}


def _evaluate_t2_market_quality(*, config: ArbConfig, snap: Any, no_snap: Any) -> dict[str, Any]:
    yes_spread_bps = _spread_bps_from_snapshot(snap)
    no_spread_bps = _spread_bps_from_snapshot(no_snap)
    complement_error_bps = None
    if snap.mid is not None and no_snap.mid is not None:
        complement_error_bps = abs(1.0 - (float(snap.mid) + float(no_snap.mid))) * 10_000.0
    yes_top_depth = float(getattr(snap, "best_ask_size", 0.0) or 0.0)
    no_top_depth = float(getattr(no_snap, "best_ask_size", 0.0) or 0.0)

    reasons: list[str] = []
    if yes_spread_bps is None or no_spread_bps is None:
        reasons.append("missing_spread")
    elif max(float(yes_spread_bps), float(no_spread_bps)) > config.t2_max_spread_bps:
        reasons.append("spread_too_wide")
    if min(yes_top_depth, no_top_depth) < config.t2_min_top_depth:
        reasons.append("top_depth_too_low")
    if complement_error_bps is None:
        reasons.append("missing_complement_error")
    elif float(complement_error_bps) > config.t2_max_complement_error_bps:
        reasons.append("complement_error_too_high")

    return {
        "passes": not reasons,
        "reasons": reasons,
        "yes_spread_bps": round(float(yes_spread_bps), 2) if yes_spread_bps is not None else None,
        "no_spread_bps": round(float(no_spread_bps), 2) if no_spread_bps is not None else None,
        "yes_top_depth": round(yes_top_depth, 4),
        "no_top_depth": round(no_top_depth, 4),
        "complement_error_bps": round(float(complement_error_bps), 2) if complement_error_bps is not None else None,
    }


def _spread_bps_from_snapshot(snap: Any) -> float | None:
    if snap is None or getattr(snap, "best_bid", None) is None or getattr(snap, "best_ask", None) is None:
        return None
    mid = getattr(snap, "mid", None)
    spread = getattr(snap, "spread", None)
    if spread is None and getattr(snap, "best_bid", None) is not None and getattr(snap, "best_ask", None) is not None:
        spread = float(snap.best_ask) - float(snap.best_bid)
    if mid is None or spread is None or float(mid) <= 0:
        return None
    return (float(spread) / float(mid)) * 10_000.0


def _collect_maker_strategy_signals(
    *,
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    maker_strategy: MakerStrategy,
    fair_values_by_market: dict[str, float],
    detector: StatisticalMispricingDetector | None = None,
)-> list[StrategySignal]:
    signals: list[StrategySignal] = []
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            continue

        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        if snap is None or snap.mid is None:
            continue
        fair_value = fair_values_by_market.get(market.condition_id)
        if fair_value is None and detector is not None:
            bids_total_size = sum(level.size for level in snap.bids[:5])
            asks_total_size = sum(level.size for level in snap.asks[:5])
            estimate = detector.estimate_market_probability(
                market_id=market.condition_id,
                outcome="YES",
                market_price=float(snap.mid),
                bids_total_size=bids_total_size,
                asks_total_size=asks_total_size,
                mid_price=float(snap.mid),
            )
            fair_value = estimate.model_prob
        if fair_value is None:
            continue

        quote = maker_strategy.compute_quote(
            token_id=yes_token.token_id,
            condition_id=market.condition_id,
            fair_value=float(fair_value),
            tick_size=max(float(getattr(snap, "tick_size", 0.01) or 0.01), 0.01),
            mid_price=float(snap.mid),
        )
        if quote is None or (quote.bid_price is None and quote.ask_price is None):
            continue

        signals.append(
            StrategySignal(
                tier=StrategyTier.MARKET_MAKING,
                signal_type="maker_quote",
                market_id=market.condition_id,
                description=f"{market.question[:80]} | maker fair={fair_value:.4f} spread={quote.spread:.4f}",
                expected_edge=max(0.0, quote.spread) * 10_000.0,
                confidence=0.5,
                recommended_size_usdc=max(quote.bid_size, quote.ask_size),
                urgency=0.2,
                payload={
                    "quote": {
                        "bid_price": quote.bid_price,
                        "ask_price": quote.ask_price,
                        "bid_size": quote.bid_size,
                        "ask_size": quote.ask_size,
                        "spread": quote.spread,
                        "fair_value": quote.fair_value,
                    }
                },
            )
        )
    return signals


def _resolve_strategy_signal_action(signal: StrategySignal) -> str:
    payload_action = str(signal.payload.get("action", "")).upper()
    if payload_action:
        return payload_action
    signal_type = signal.signal_type.upper()
    if "BUY_YES" in signal_type:
        return "BUY_YES"
    if "BUY_NO" in signal_type:
        return "BUY_NO"
    if "SELL_YES" in signal_type:
        return "SELL_YES"
    if "SELL_NO" in signal_type:
        return "SELL_NO"
    if signal_type.endswith("BUY_YES"):
        return "BUY_YES"
    if signal_type.endswith("BUY_NO"):
        return "BUY_NO"
    return ""


def _find_market_for_signal(signal_market_id: str, markets: list[MarketInfo]) -> MarketInfo | None:
    for market in markets:
        if market.condition_id == signal_market_id:
            return market
        if (
            signal_market_id
            and len(signal_market_id) >= 8
            and (market.condition_id.startswith(signal_market_id) or signal_market_id.startswith(market.condition_id))
        ):
            return market
    return None


def _build_directional_opportunity_from_signal(
    *,
    config: ArbConfig,
    signal: StrategySignal,
    market: MarketInfo,
    ob_analyzer: OrderBookAnalyzer,
) -> tuple[ArbOpportunity | None, float, str]:
    action = _resolve_strategy_signal_action(signal)
    if action not in {"BUY_YES", "BUY_NO"}:
        return None, 0.0, "unsupported_direction"

    if len(market.tokens) < 2:
        return None, 0.0, "non_binary_market"

    yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
    no_token = next((t for t in market.tokens if (t.outcome or "").lower() == "no"), market.tokens[-1])
    target_token = yes_token if action == "BUY_YES" else no_token
    outcome_label = "Yes" if action == "BUY_YES" else "No"

    snap = ob_analyzer.get_snapshot(target_token.token_id)
    if snap is None or snap.best_ask is None or snap.best_ask <= 0:
        return None, 0.0, "missing_best_ask"

    target_notional = max(0.0, float(signal.recommended_size_usdc))
    if target_notional <= 0:
        return None, 0.0, "non_positive_notional"
    target_size = target_notional / float(snap.best_ask)
    executable = ob_analyzer.get_executable_ask_price(target_token.token_id, target_size)
    if executable is None:
        return None, 0.0, "insufficient_depth"
    execution_price, fillable_size = executable
    if fillable_size <= 0:
        return None, 0.0, "zero_fillable_size"

    gross_edge = abs(float(signal.payload.get("deviation", 0.0) or (signal.expected_edge / 10_000.0)))
    fee_estimate = float(config.polymarket_taker_fee_rate) * float(execution_price)
    net_edge = gross_edge - fee_estimate
    if net_edge <= 0:
        return None, 0.0, "edge_below_fee"

    opportunity = ArbOpportunity(
        arb_type=ArbType.DIRECTIONAL,
        event_id=market.event_id or market.condition_id,
        event_title=market.question,
        markets=[market],
        total_cost=float(execution_price),
        guaranteed_payout=1.0,
        gross_edge=gross_edge,
        net_edge=net_edge,
        edge_pct=(net_edge / float(execution_price)) * 100.0 if execution_price > 0 else 0.0,
        legs=[
            ArbLeg(
                token_id=target_token.token_id,
                condition_id=market.condition_id,
                outcome=outcome_label,
                side=OrderSide.BUY,
                price=float(execution_price),
                size=float(target_size),
                available_size=float(fillable_size),
                execution_price=float(execution_price),
                economic_cost=float(execution_price),
            )
        ],
        max_executable_size=float(fillable_size),
        confidence=float(signal.confidence),
    )
    return opportunity, float(target_size), ""


def _sum_trade_exposure(trades: list[Any]) -> float:
    total = 0.0
    for trade in trades:
        leg_cost = getattr(trade, "economic_cost", None)
        if leg_cost is None:
            leg_cost = getattr(trade, "price", 0.0)
        fill_size = getattr(trade, "fill_size", None)
        size = float(fill_size if fill_size is not None else getattr(trade, "size", 0.0) or 0.0)
        total += float(leg_cost or 0.0) * size
    return total


def _execute_strategy_signal(
    *,
    signal: StrategySignal,
    config: ArbConfig,
    active_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    orchestrator: StrategyOrchestrator,
    dash_state: DashboardState,
    event_recorder: EventRecorder,
    maker_strategy: MakerStrategy,
) -> tuple[bool, str]:
    market = _find_market_for_signal(signal.market_id, active_markets)
    if signal.tier == StrategyTier.CROSS_PLATFORM:
        if not config.dry_run:
            return False, "cross_platform_live_requires_external_executor"
        pair_cost = max(float(signal.payload.get("total_cost", 0.0) or 0.0), 1e-9)
        bundle_size = max(0.0, float(signal.recommended_size_usdc)) / pair_cost
        trades = [
            TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=str(uuid.uuid4())[:12],
                token_id=str(signal.payload.get("pair_id", "poly")),
                condition_id=signal.market_id,
                side=OrderSide.BUY,
                price=float(signal.payload.get("poly_cost", 0.0) or 0.0),
                size=bundle_size,
                status=TradeStatus.FILLED,
                fill_price=float(signal.payload.get("poly_cost", 0.0) or 0.0),
                fill_size=bundle_size,
                economic_cost=float(signal.payload.get("poly_cost", 0.0) or 0.0),
                simulated=True,
            ),
            TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=str(uuid.uuid4())[:12],
                token_id=f"kalshi:{signal.payload.get('pair_id', 'pair')}",
                condition_id=f"kalshi:{signal.market_id}",
                side=OrderSide.BUY,
                price=float(signal.payload.get("kalshi_cost", 0.0) or 0.0),
                size=bundle_size,
                status=TradeStatus.FILLED,
                fill_price=float(signal.payload.get("kalshi_cost", 0.0) or 0.0),
                fill_size=bundle_size,
                economic_cost=float(signal.payload.get("kalshi_cost", 0.0) or 0.0),
                simulated=True,
            ),
        ]
        if event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "tier": signal.tier.name,
                "signal_type": signal.signal_type,
                "market_id": signal.market_id,
                "status": "simulated",
                "trade_count": len(trades),
            })
        return True, ""

    if signal.tier == StrategyTier.STATISTICAL_ARB:
        if market is None:
            return False, "market_not_found"
        opportunity, target_size, build_reason = _build_directional_opportunity_from_signal(
            config=config,
            signal=signal,
            market=market,
            ob_analyzer=ob_analyzer,
        )
        if opportunity is None:
            return False, build_reason
        can_trade, reason, adj_size = risk_mgr.pre_trade_check(opportunity, target_size)
        if not can_trade:
            return False, reason
        balance_ok, balance_reason, _ = executor.ensure_sufficient_collateral(opportunity.total_cost * adj_size)
        if not balance_ok:
            return False, balance_reason
        trades = executor.execute_arbitrage(opportunity, adj_size)
        execution_success = executor.is_successful_execution(opportunity, trades)
        # 无论 dry_run 与否都记录到 risk_mgr，保证持仓/敞口/冷却期追踪生效；
        # dry_run 下 trades 均为 simulated，不会触发真实订单。
        risk_mgr.record_execution(opportunity, trades)
        orchestrator.record_execution(
            signal,
            success=execution_success,
            exposure_amount_usdc=_sum_trade_exposure(trades),
        )
        if event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "tier": signal.tier.name,
                "signal_type": signal.signal_type,
                "market_id": signal.market_id,
                "status": "executed" if execution_success else "attempted",
                "trade_count": len(trades),
                "arb_type": opportunity.arb_type.value,
            })
        for trade in trades:
            dash_state.append_trade({
                "trade_id": trade.trade_id,
                "arb_id": trade.arb_id,
                "side": trade.side.value,
                "price": trade.price,
                "size": trade.size,
                "status": trade.status.value,
                "token_id": trade.token_id[:20],
                "timestamp": trade.timestamp,
                "simulated": trade.simulated,
            })
        return True, ""

    if signal.tier == StrategyTier.MARKET_MAKING:
        if market is None:
            return False, "market_not_found"
        if len(market.tokens) < 2:
            return False, "non_binary_market"
        quote = signal.payload.get("quote", {}) if isinstance(signal.payload.get("quote", {}), dict) else {}
        bid_price = float(quote.get("bid_price") or 0.0)
        bid_size = float(quote.get("bid_size") or 0.0)
        if bid_price <= 0 or bid_size <= 0:
            return False, "maker_bid_missing"
        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        maker_opp = ArbOpportunity(
            arb_type=ArbType.MARKET_MAKING,
            event_id=market.event_id or market.condition_id,
            event_title=market.question,
            markets=[market],
            total_cost=bid_price,
            guaranteed_payout=1.0,
            gross_edge=max(0.0, float(quote.get("spread") or 0.0)),
            net_edge=max(0.0, float(quote.get("spread") or 0.0)),
            edge_pct=((float(quote.get("spread") or 0.0) / bid_price) * 100.0) if bid_price > 0 else 0.0,
            legs=[
                ArbLeg(
                    token_id=yes_token.token_id,
                    condition_id=market.condition_id,
                    outcome="Yes",
                    side=OrderSide.BUY,
                    price=bid_price,
                    size=bid_size,
                    available_size=bid_size,
                    execution_price=bid_price,
                    economic_cost=bid_price,
                )
            ],
            max_executable_size=bid_size,
            confidence=float(signal.confidence),
        )
        can_trade, reason, adj_size = risk_mgr.pre_trade_check(maker_opp, bid_size)
        if not can_trade:
            return False, reason
        balance_ok, balance_reason, _ = executor.ensure_sufficient_collateral(bid_price * adj_size)
        if not balance_ok:
            return False, balance_reason
        trade = executor.submit_limit_order(
            token_id=yes_token.token_id,
            condition_id=market.condition_id,
            outcome="Yes",
            side=OrderSide.BUY,
            price=bid_price,
            size=adj_size,
            post_only=True,
            order_type_name="GTC",
        )
        submission_success = trade.status in {TradeStatus.PENDING, TradeStatus.PARTIAL, TradeStatus.FILLED}
        risk_mgr.record_execution(
            maker_opp,
            [trade],
            count_pending_as_failure=False,
        )
        orchestrator.record_execution(
            signal,
            success=submission_success,
            exposure_amount_usdc=_sum_trade_exposure([trade]),
        )
        if trade.status == TradeStatus.FILLED and trade.fill_size:
            maker_strategy.update_inventory(yes_token.token_id, "BUY", float(trade.fill_size))
        dash_state.append_trade({
            "trade_id": trade.trade_id,
            "arb_id": trade.arb_id,
            "side": trade.side.value,
            "price": trade.price,
            "size": trade.size,
            "status": trade.status.value,
            "token_id": trade.token_id[:20],
            "timestamp": trade.timestamp,
            "simulated": trade.simulated,
            "post_only": True,
        })
        if event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "tier": signal.tier.name,
                "signal_type": signal.signal_type,
                "market_id": signal.market_id,
                "status": "submitted" if submission_success else "failed",
                "trade_status": trade.status.value,
                "post_only": True,
            })
        return submission_success, ""

    return False, "unsupported_strategy_tier"


def _market_priority_score(market: MarketInfo) -> tuple[float, float, float]:
    binary_boost = 1.0 if len(market.tokens) == 2 else 0.0
    return (
        binary_boost,
        float(market.volume_24h or 0.0),
        float(market.liquidity or 0.0),
    )


def _event_priority_score(event: Any) -> tuple[float, float, int]:
    markets = list(getattr(event, "markets", []) or [])
    total_volume = sum(float(getattr(market, "volume_24h", 0.0) or 0.0) for market in markets)
    total_liquidity = sum(float(getattr(market, "liquidity", 0.0) or 0.0) for market in markets)
    return (
        total_volume,
        total_liquidity,
        len(markets),
    )


def _select_scan_candidates(markets: list[MarketInfo], max_count: int, *, focus_keywords: list[str] | None = None) -> list[MarketInfo]:
    active = [
        market for market in markets
        if market.active and not market.closed and _matches_focus(_market_focus_text(market), focus_keywords or [])
    ]
    active.sort(key=_market_priority_score, reverse=True)
    return active[:max_count]


def _select_event_candidates(events: list[Any], max_count: int, *, focus_keywords: list[str] | None = None) -> list[Any]:
    active = [
        event for event in events
        if getattr(event, "active", True)
        and not getattr(event, "closed", False)
        and _matches_focus(_event_focus_text(event), focus_keywords or [])
    ]
    active.sort(key=_event_priority_score, reverse=True)
    return active[:max_count]


def _merge_focus_event_markets(
    markets: list[MarketInfo],
    events: list[Any],
    max_count: int,
    *,
    focus_keywords: list[str] | None = None,
) -> list[MarketInfo]:
    merged: dict[str, MarketInfo] = {
        market.condition_id: market
        for market in markets
        if market.active and not market.closed
    }

    for event in events:
        for market in getattr(event, "markets", []) or []:
            if not market.active or market.closed or len(market.tokens) != 2:
                continue
            if not market.event_id:
                market.event_id = getattr(event, "event_id", "")
            if not market.event_slug:
                market.event_slug = getattr(event, "slug", "")
            if not getattr(market, "event_title", ""):
                market.event_title = getattr(event, "title", "")
            if focus_keywords and not _matches_focus(_market_focus_text(market), focus_keywords):
                continue
            merged.setdefault(market.condition_id, market)

    ranked = list(merged.values())
    ranked.sort(key=_market_priority_score, reverse=True)
    return ranked[:max_count]


def _prime_candidate_orderbooks(
    *,
    candidate_markets: list[MarketInfo],
    candidate_events: list[Any],
    ob_analyzer: OrderBookAnalyzer,
) -> dict[str, Any]:
    token_ids: list[str] = []
    for market in candidate_markets:
        if len(market.tokens) == 2 and market.active and not market.closed:
            token_ids.extend(token.token_id for token in market.tokens if token.token_id)
    for event in candidate_events:
        for market in getattr(event, "markets", []) or []:
            if market.closed or not market.active:
                continue
            token_ids.extend(token.token_id for token in market.tokens if token.token_id)
    if not token_ids:
        return {}
    return ob_analyzer.batch_get_snapshots(list(dict.fromkeys(token_ids)), delay=0.0)


def _round_timing(value: float) -> float:
    return round(max(0.0, float(value or 0.0)), 4)


def _build_cycle_summary_payload(
    *,
    run_id: str,
    cycle: int,
    markets_scanned: int,
    universe_market_count: int,
    selected_event_count: int,
    arbs_found_total: int,
    arbs_executed_total: int,
    ws_status: dict[str, Any],
    research_count: int,
    daily_pnl: float,
    open_positions: int,
    focus_keywords: list[str],
    book_stats: dict[str, int],
    timing_stats: dict[str, float],
    cycle_status: str = "ok",
) -> dict[str, Any]:
    return {
        "event": "cycle_summary",
        "cycle_status": cycle_status,
        "run_id": run_id,
        "cycle": cycle,
        "markets_scanned": markets_scanned,
        "universe_market_count": universe_market_count,
        "selected_event_count": selected_event_count,
        "arbs_found_total": arbs_found_total,
        "arbs_executed_total": arbs_executed_total,
        "ws_connected": ws_status.get("connected", False),
        "ws_tokens": ws_status.get("subscribed_tokens", 0),
        "research_count": research_count,
        "daily_pnl": daily_pnl,
        "open_positions": open_positions,
        "focus_keywords": focus_keywords,
        "book_stats": {
            key: int(book_stats.get(key, 0))
            for key in (
                "requests",
                "ws_hit",
                "cache_hit",
                "rest_fallback",
                "rest_success",
                "rest_error",
                "missing_orderbook",
                "cooldown_skip",
            )
        },
        "timing": {
            key: _round_timing(value)
            for key, value in timing_stats.items()
        },
    }


def _emit_cycle_metrics(
    *,
    event_recorder: EventRecorder,
    ob_analyzer: OrderBookAnalyzer,
    cycle_perf_start: float,
    cycle_timing: dict[str, float],
    run_id: str,
    cycle: int,
    markets_scanned: int,
    universe_market_count: int,
    selected_event_count: int,
    arbs_found_total: int,
    arbs_executed_total: int,
    ws_status: dict[str, Any],
    research_count: int,
    daily_pnl: float,
    open_positions: int,
    focus_keywords: list[str],
    cycle_status: str = "ok",
) -> dict[str, Any]:
    cycle_book_stats = ob_analyzer.snapshot_stats(reset=True)
    timing_stats = dict(cycle_timing)
    timing_stats["total_cycle_sec"] = time.perf_counter() - cycle_perf_start
    payload = _build_cycle_summary_payload(
        run_id=run_id,
        cycle=cycle,
        markets_scanned=markets_scanned,
        universe_market_count=universe_market_count,
        selected_event_count=selected_event_count,
        arbs_found_total=arbs_found_total,
        arbs_executed_total=arbs_executed_total,
        ws_status=ws_status,
        research_count=research_count,
        daily_pnl=daily_pnl,
        open_positions=open_positions,
        focus_keywords=focus_keywords,
        book_stats=cycle_book_stats,
        timing_stats=timing_stats,
        cycle_status=cycle_status,
    )
    if event_recorder.is_enabled:
        event_recorder.write_event("cycle_metrics", payload)
    return payload


@dataclass
class _ResearchRefreshState:
    last_report: ResearchSignalReport | None = None
    last_report_signature: tuple[str, ...] = ()
    pending_future: Future | None = None
    pending_signature: tuple[str, ...] = ()
    last_submit_ts: float = 0.0


def _research_market_sample(
    *,
    universe_markets: list[MarketInfo],
    scanned_markets: list[MarketInfo],
    max_items: int,
) -> list[MarketInfo]:
    source_markets = universe_markets if universe_markets else scanned_markets
    if max_items <= 0:
        return list(source_markets)
    return list(source_markets[:max_items])


def _research_market_signature(markets: list[MarketInfo]) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{market.event_id}:{market.condition_id}:{(market.question or '').strip().lower()[:80]}"
            for market in markets
        )
    )


def _advance_research_refresh(
    *,
    research_signal_service: "ResearchSignalService" | None,
    research_executor: ThreadPoolExecutor | None,
    state: _ResearchRefreshState,
    universe_markets: list[MarketInfo],
    scanned_markets: list[MarketInfo],
    max_items: int,
    window_sec: int,
    refresh_interval_sec: float,
    now_ts: float | None = None,
) -> ResearchSignalReport | None:
    now_ts = float(now_ts if now_ts is not None else time.time())
    if state.pending_future is not None and state.pending_future.done():
        try:
            result = state.pending_future.result()
        except Exception as e:
            LOG.error("Research signal 刷新失败: %s", e, exc_info=True)
        else:
            state.last_report = result
            state.last_report_signature = state.pending_signature
        state.pending_future = None
        state.pending_signature = ()

    sample_markets = _research_market_sample(
        universe_markets=universe_markets,
        scanned_markets=scanned_markets,
        max_items=max_items,
    )
    current_signature = _research_market_signature(sample_markets)
    if (
        research_signal_service is not None
        and research_executor is not None
        and sample_markets
        and state.pending_future is None
    ):
        signature_changed = current_signature != state.last_report_signature
        periodic_refresh_due = (
            state.last_report is not None
            and not signature_changed
            and (now_ts - state.last_submit_ts) >= max(1.0, float(refresh_interval_sec))
        )
        initial_refresh_due = (
            state.last_report is None
            and (state.last_submit_ts <= 0 or (now_ts - state.last_submit_ts) >= _RESEARCH_RESUBMIT_COOLDOWN_SEC)
        )
        if signature_changed or periodic_refresh_due or initial_refresh_due:
            state.pending_future = research_executor.submit(
                research_signal_service.collect_report,
                sample_markets,
                window_sec,
            )
            state.pending_signature = current_signature
            state.last_submit_ts = now_ts

    if state.last_report is None or state.last_report_signature != current_signature:
        return None
    return state.last_report


def _refresh_market_universe(
    *,
    scanner: MarketScanner,
    config: ArbConfig,
    cached_markets: list[MarketInfo],
    cached_events: list[Any],
    last_refresh_ts: float,
) -> tuple[list[MarketInfo], list[Any], float, bool]:
    now = time.time()
    should_refresh = (
        not cached_markets
        or not cached_events
        or (now - last_refresh_ts) >= config.market_universe_refresh_sec
    )
    if not should_refresh:
        return cached_markets, cached_events, last_refresh_ts, False

    markets = scanner.fetch_active_markets(
        min_liquidity=config.min_liquidity,
        min_volume_24h=config.min_volume_24h,
    )
    events = scanner.fetch_active_events(limit=max(50, config.hot_event_pool_size * 2))
    return markets, events, now, True


def _start_ws_feed(
    targets: list[MarketInfo],
    enhanced_store: EnhancedBookStore,
    tick_recorder: TickRecorder | None = None,
) -> tuple[WebSocketFeed, OrderBookMirror]:
    """为选中的目标市场创建并启动 WebSocket feed.

    将第一个市场绑定到 EnhancedBookStore（用于 EdgeEngine），
    所有市场的 token 都订阅到 OrderBookMirror。
    """
    mirror = OrderBookMirror()
    if tick_recorder is not None and tick_recorder.is_enabled:
        tick_recorder.register_markets(targets)
        mirror.register_callback(tick_recorder.on_book_update)

    primary = targets[0]
    yes_token = next((t for t in primary.tokens if t.outcome.lower() == "yes"), primary.tokens[0])
    no_token = next((t for t in primary.tokens if t.outcome.lower() == "no"), primary.tokens[-1])
    enhanced_store.set_market(primary.condition_id, yes_token.token_id, no_token.token_id)

    all_token_ids: list[str] = []
    for m in targets:
        for t in m.tokens:
            all_token_ids.append(t.token_id)

    feed = WebSocketFeed(mirror=mirror, enhanced_store=enhanced_store)
    feed.subscribe(all_token_ids)
    feed.start()

    LOG.info(
        "WebSocket 已启动: 主市场=%s (%s), 共订阅 %d 个 token",
        primary.condition_id[:12],
        primary.question[:40],
        len(all_token_ids),
    )
    return feed, mirror


def main(dotenv_path: str | None = None) -> None:
    """套利机器人主入口."""
    _SHUTDOWN_EVENT.clear()
    config = ArbConfig.from_env(dotenv_path)
    setup_logging(config.log_level, config.log_file)
    run_id = _build_run_instance_id()
    _log_startup_summary(config, run_id)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    ro_client = build_readonly_client(config)
    trading_client = None
    if not config.dry_run:
        trading_client = build_trading_client(config)

    scanner = MarketScanner(config)
    ob_analyzer = OrderBookAnalyzer(
        ro_client,
        snapshot_ttl_sec=config.orderbook_snapshot_ttl_sec,
        ws_snapshot_max_age_sec=config.orderbook_ws_snapshot_max_age_sec,
        retry_count=config.orderbook_retry_count,
        retry_delay_sec=config.orderbook_retry_delay_sec,
        missing_orderbook_cooldown_sec=config.orderbook_missing_cooldown_sec,
    )
    detector = ArbitrageDetector(config, ob_analyzer)
    executor = ExecutionEngine(config, trading_client or ro_client)
    risk_mgr = RiskManager(config)
    notifier = NotificationManager(config)

    enhanced_store = EnhancedBookStore()
    vol_estimator = VolEstimator(
        fast_minutes=config.vol_fast_minutes,
        slow_minutes=config.vol_slow_minutes,
        min_bars=config.vol_min_bars,
    )
    edge_engine = EdgeEngine(
        min_edge_bps=config.edge_min_bps,
        min_depth=config.min_liquidity * 0.05,
        max_spread_bps=config.edge_max_spread_bps,
        min_confidence=config.edge_min_confidence,
        confidence_full_bps=config.edge_confidence_full_bps,
        confidence_imbalance_weight=config.edge_confidence_imbalance_weight,
        volatility_spike_ratio=config.edge_volatility_spike_ratio,
        volatility_spike_penalty=config.edge_volatility_spike_penalty,
        volatility_calm_ratio=config.edge_volatility_calm_ratio,
        volatility_calm_boost=config.edge_volatility_calm_boost,
    )
    statistical_detector = StatisticalMispricingDetector(
        min_deviation=config.t2_min_deviation,
        min_confidence=config.edge_min_confidence,
    )
    maker_strategy = MakerStrategy(
        spread_calc=DynamicSpreadCalculator(vol_estimator=vol_estimator),
        default_size=config.default_order_size_usdc,
        max_inventory=max(config.max_exposure_per_market, config.default_order_size_usdc),
    )
    cross_platform_scanner = _create_cross_platform_scanner(config, ob_analyzer)
    tick_recorder = TickRecorder(
        output_dir=config.tick_record_dir,
        enabled=config.tick_record_enabled,
    )
    event_recorder = EventRecorder(
        output_dir=config.telemetry_record_dir,
        enabled=config.telemetry_record_enabled,
    )
    data_janitor = DataJanitor(
        enabled=config.data_cleanup_enabled,
        interval_sec=config.data_cleanup_interval_sec,
        tick_dir=config.tick_record_dir,
        tick_retention_days=config.data_ticks_retention_days,
        tick_max_gb=config.data_ticks_max_gb,
        telemetry_dir=config.telemetry_record_dir,
        telemetry_retention_days=config.data_telemetry_retention_days,
        telemetry_max_gb=config.data_telemetry_max_gb,
        research_cache_dir=config.research_signal_cache_dir,
        research_cache_retention_days=config.data_research_cache_retention_days,
        research_cache_max_gb=config.data_research_cache_max_gb,
        research_knowledge_dir=config.research_signal_knowledge_dir,
        backtest_data_dir=config.backtest_data_dir,
        backtest_retention_days=config.data_backtest_retention_days,
        backtest_max_gb=config.data_backtest_max_gb,
    )
    if config.tick_record_enabled:
        LOG.info("Tick 录制已开启: %s", config.tick_record_dir)
    focus_keywords = _focus_keywords(config.market_focus_keywords)
    if config.telemetry_record_enabled:
        LOG.info("Telemetry 录制已开启: %s", config.telemetry_record_dir)
        event_recorder.write_event("risk_events", {
            "event": "startup",
            "run_id": run_id,
            "pid": os.getpid(),
            "mode": "dry_run" if config.dry_run else "live",
            "scan_interval_sec": config.scan_interval_sec,
            "universe_refresh_sec": config.market_universe_refresh_sec,
            "hot_market_pool_size": config.hot_market_pool_size,
            "hot_event_pool_size": config.hot_event_pool_size,
            "focus_keywords": focus_keywords,
            "ws_enabled": config.ws_enabled,
            "research_enabled": config.research_signal_enabled,
            "ai_enabled": config.ai_enabled,
        })
    if config.data_cleanup_enabled:
        LOG.info("Data 定期清理已开启: interval=%.0fs", config.data_cleanup_interval_sec)

    orchestrator = StrategyOrchestrator(total_bankroll=config.max_total_exposure)
    ctx_builder = MarketContextBuilder()
    research_signal_service: Optional["ResearchSignalService"] = _create_research_signal_service(config)
    research_signal_enabled = bool(config.research_signal_enabled and research_signal_service is not None)
    research_executor: ThreadPoolExecutor | None = (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-signal")
        if research_signal_service is not None
        else None
    )
    research_refresh_state = _ResearchRefreshState()
    research_refresh_interval_sec = max(5.0, float(config.research_signal_cache_ttl_sec))
    portfolio_sync = PortfolioSync(config) if config.portfolio_sync_enabled else None
    if portfolio_sync is not None:
        LOG.info(
            "账户同步已启用: interval=%.0fs timeout=%.1fs address=%s",
            config.portfolio_sync_interval_sec,
            config.portfolio_sync_timeout_sec,
            (portfolio_sync.source_address[:10] + "…") if portfolio_sync.source_address else "",
        )

    ai_advisor: Optional[AIAdvisor] = None
    if config.ai_enabled:
        ai_advisor = create_ai_advisor(config)
        if ai_advisor:
            LOG.info(
                "AI 决策引擎已启用: provider=%s, model=%s, interval=%.0fs",
                config.ai_provider, config.ai_model, config.ai_eval_interval_sec,
            )

    ws_feed: Optional[WebSocketFeed] = None
    ws_mirror: Optional[OrderBookMirror] = None
    ws_target_ids: list[str] = []
    last_vol_feed_ts = 0.0
    cached_universe_markets: list[MarketInfo] = []
    cached_universe_events: list[Any] = []
    last_universe_refresh_ts = 0.0
    last_telemetry_heartbeat_ts = 0.0
    last_portfolio_sync_ts = 0.0

    dash_state = DashboardState()
    dash_state.update(
        is_dry_run=config.dry_run,
        scan_interval=config.scan_interval_sec,
    )
    if config.dashboard_enabled:
        start_dashboard_server(dash_state, port=config.dashboard_port)
        LOG.info("Dashboard 已启动: http://127.0.0.1:%d", config.dashboard_port)

    notifier.notify_startup(
        mode="DRY RUN" if config.dry_run else "LIVE",
        min_profit_usd=config.min_edge_usd,
        min_profit_pct=config.min_edge_pct,
        scan_interval_sec=config.scan_interval_sec,
        ws_enabled=config.ws_enabled,
        portfolio_sync_enabled=config.portfolio_sync_enabled,
    )

    cycle = 0
    total_arbs_found = 0
    total_arbs_executed = 0
    total_simulated_executed = 0
    total_live_expected_profit = 0.0
    total_simulated_expected_profit = 0.0
    consecutive_api_errors = 0

    while not _SHUTDOWN_EVENT.is_set():
        cycle += 1
        cycle_start = time.time()
        cycle_perf_start = time.perf_counter()
        cycle_timing: dict[str, float] = {
            "universe_refresh_sec": 0.0,
            "candidate_select_sec": 0.0,
            "ws_refresh_sec": 0.0,
            "prewarm_sec": 0.0,
            "scan_cycle_sec": 0.0,
            "research_sec": 0.0,
            "strategy_sec": 0.0,
            "execution_sec": 0.0,
            "ai_sec": 0.0,
        }
        ob_analyzer.snapshot_stats(reset=True)
        scanned_markets: list[MarketInfo] = []
        event_candidates: list[Any] = []
        universe_markets: list[MarketInfo] = []
        if data_janitor.should_run():
            for cleanup_stats in data_janitor.run_once():
                if cleanup_stats.deleted_files > 0:
                    event_recorder.write_event("risk_events", {
                        "event": "data_cleanup",
                        **cleanup_stats.to_dict(),
                    })

        dash_state.update(
            cycle_count=cycle,
            markets_scanned=0,
            universe_status={
                "universe_market_count": len(cached_universe_markets),
                "hot_market_pool_size": config.hot_market_pool_size,
                "hot_event_pool_size": config.hot_event_pool_size,
                "focus_keywords": focus_keywords,
                "last_universe_refresh_ts": last_universe_refresh_ts or None,
            },
            ws_status=_build_ws_status(
                config=config,
                enhanced_store=enhanced_store,
                ws_target_ids=ws_target_ids,
                phase_hint="initializing",
            ),
            book_summary=enhanced_store.get_summary(),
            volatility=vol_estimator.snapshot(),
        )

        try:
            phase_start = time.perf_counter()
            cached_universe_markets, cached_universe_events, last_universe_refresh_ts, universe_refreshed = _refresh_market_universe(
                scanner=scanner,
                config=config,
                cached_markets=cached_universe_markets,
                cached_events=cached_universe_events,
                last_refresh_ts=last_universe_refresh_ts,
            )
            cycle_timing["universe_refresh_sec"] += time.perf_counter() - phase_start

            phase_start = time.perf_counter()
            scanned_markets = _select_scan_candidates(
                cached_universe_markets,
                config.hot_market_pool_size,
                focus_keywords=focus_keywords,
            )
            event_candidates = _select_event_candidates(
                cached_universe_events,
                config.hot_event_pool_size,
                focus_keywords=focus_keywords,
            )
            scanned_markets = _merge_focus_event_markets(
                scanned_markets,
                event_candidates,
                config.hot_market_pool_size,
                focus_keywords=focus_keywords,
            )
            cycle_timing["candidate_select_sec"] += time.perf_counter() - phase_start

            if config.ws_enabled and scanned_markets:
                phase_start = time.perf_counter()
                need_refresh = (
                    ws_feed is None
                    or cycle % config.ws_refresh_cycles == 0
                )
                if need_refresh:
                    targets = _select_ws_targets(scanned_markets, config.ws_max_markets)
                    if targets:
                        new_ids = sorted(t.token_id for m in targets for t in m.tokens)
                        if new_ids != ws_target_ids:
                            if ws_feed is not None:
                                ws_feed.stop()
                                LOG.info("旧 WebSocket feed 已停止，切换到新目标市场")
                            ws_feed, ws_mirror = _start_ws_feed(targets, enhanced_store, tick_recorder)
                            ws_target_ids = new_ids
                    elif cycle == 1 or cycle % 20 == 0:
                        LOG.warning("未选出可订阅的 WS 市场，可能是市场 token 解析为空或筛选结果为空")
                cycle_timing["ws_refresh_sec"] += time.perf_counter() - phase_start
            ob_analyzer.set_live_mirror(ws_mirror)
            phase_start = time.perf_counter()
            _prime_candidate_orderbooks(
                candidate_markets=scanned_markets,
                candidate_events=event_candidates,
                ob_analyzer=ob_analyzer,
            )
            cycle_timing["prewarm_sec"] += time.perf_counter() - phase_start

            universe_markets = list(cached_universe_markets)
            phase_start = time.perf_counter()
            opportunities = _scan_cycle(
                detector=detector,
                config=config,
                candidate_markets=scanned_markets,
                candidate_events=event_candidates,
                universe_market_count=len(cached_universe_markets),
                universe_refreshed=universe_refreshed,
                progress_cb=lambda **kwargs: dash_state.update(
                    markets_scanned=kwargs.get("scanned_markets", 0),
                    arbs_found=total_arbs_found + kwargs.get("opportunities_found", 0),
                    ws_status=_build_ws_status(
                        config=config,
                        enhanced_store=enhanced_store,
                        ws_target_ids=ws_target_ids,
                        phase_hint=kwargs.get("phase", "initializing"),
                        scanned_orderbooks=kwargs.get("scanned_orderbooks", 0),
                        scanned_events=kwargs.get("scanned_events", 0),
                    ),
                ),
            )
            cycle_timing["scan_cycle_sec"] += time.perf_counter() - phase_start
            consecutive_api_errors = 0
            dash_state.update(
                markets_scanned=len(scanned_markets),
                ws_status=_build_ws_status(
                    config=config,
                    enhanced_store=enhanced_store,
                    ws_target_ids=ws_target_ids,
                    phase_hint="scan_complete",
                ),
            )
        except Exception as e:
            consecutive_api_errors += 1
            LOG.error("扫描周期 #%d 异常: %s", cycle, e, exc_info=True)
            event_recorder.write_event("risk_events", {
                "event": "scan_cycle_error",
                "cycle": cycle,
                "error": str(e),
                "consecutive_api_errors": consecutive_api_errors,
            })
            dash_state.append_error({"message": str(e), "timestamp": time.time()})
            dash_state.update(
                markets_scanned=0,
                ws_status=_build_ws_status(
                    config=config,
                    enhanced_store=enhanced_store,
                    ws_target_ids=ws_target_ids,
                    phase_hint="scan_error",
                ),
            )
            _emit_cycle_metrics(
                event_recorder=event_recorder,
                ob_analyzer=ob_analyzer,
                cycle_perf_start=cycle_perf_start,
                cycle_timing=cycle_timing,
                run_id=run_id,
                cycle=cycle,
                markets_scanned=len(scanned_markets),
                universe_market_count=len(cached_universe_markets),
                selected_event_count=len(event_candidates),
                arbs_found_total=total_arbs_found,
                arbs_executed_total=total_arbs_executed,
                ws_status=_build_ws_status(
                    config=config,
                    enhanced_store=enhanced_store,
                    ws_target_ids=ws_target_ids,
                    phase_hint="scan_error",
                ),
                research_count=0,
                daily_pnl=risk_mgr.state.daily_pnl,
                open_positions=risk_mgr.state.open_positions,
                focus_keywords=focus_keywords,
                cycle_status="error",
            )
            if consecutive_api_errors >= 10:
                LOG.error("连续 %d 次 API 错误，暂停 60 秒", consecutive_api_errors)
                notifier.notify_fatal_error(
                    f"连续 {consecutive_api_errors} 次 API 错误，主循环将暂停 60 秒",
                    error_key="api_error_streak",
                )
                time.sleep(60)
            else:
                time.sleep(config.scan_interval_sec)
            continue

        # --- VolEstimator 喂入 mid price ---
        now = time.time()
        if (
            config.ws_enabled
            and enhanced_store.is_ready()
            and now - last_vol_feed_ts >= config.ws_vol_feed_interval_sec
        ):
            mid = enhanced_store.get_yes_mid()
            if mid is not None and mid > 0:
                vol_estimator.update_1m_close(mid, int(now * 1000))
                last_vol_feed_ts = now

        if opportunities:
            total_arbs_found += len(opportunities)
            LOG.info(
                "周期 #%d: 发现 %d 个套利机会",
                cycle,
                len(opportunities),
            )
            for opp in opportunities:
                event_recorder.write_event("opportunities", _serialize_opportunity_event(opp, stage="detected"))
                dash_state.append_opportunity({
                    "arb_type": opp.arb_type.value,
                    "mode": "theoretical",
                    "stage": "detected",
                    "event_title": opp.event_title,
                    "total_cost": opp.total_cost,
                    "net_edge": opp.net_edge,
                    "edge_pct": opp.edge_pct,
                    "confidence": opp.confidence,
                    "max_size": opp.max_executable_size,
                    "legs": len(opp.legs),
                    "timestamp": opp.timestamp,
                })

        research_signals = []
        research_report: ResearchSignalReport | None = None
        phase_start = time.perf_counter()
        if research_signal_service is not None:
            research_report = _advance_research_refresh(
                research_signal_service=research_signal_service,
                research_executor=research_executor,
                state=research_refresh_state,
                universe_markets=universe_markets,
                scanned_markets=scanned_markets,
                max_items=config.research_signal_max_items,
                window_sec=config.research_signal_window_sec,
                refresh_interval_sec=research_refresh_interval_sec,
            )
            if research_report is not None:
                research_signals = research_report.signals
        if research_report is not None:
            scanner.enrich_markets_with_research(
                universe_markets if universe_markets else scanned_markets,
                research_signal_service,
                window_sec=config.research_signal_window_sec,
                report=research_report,
            )
        elif research_signal_service is not None:
            scanner.enrich_markets_with_research(
                universe_markets if universe_markets else scanned_markets,
                research_signal_service,
                signals=[],
            )
        cycle_timing["research_sec"] += time.perf_counter() - phase_start

        edge_decision = edge_engine.evaluate(enhanced_store, vol_estimator)
        if edge_decision.direction != "NONE":
            dash_state.append_opportunity({
                "arb_type": "edge_engine",
                "mode": "signal",
                "stage": "signal",
                "event_title": f"[Edge] {edge_decision.market_id or 'active_market'}",
                "total_cost": edge_decision.market_price,
                "net_edge": edge_decision.edge_bps / 10000.0,
                "edge_pct": edge_decision.edge_bps / 100.0,
                "confidence": edge_decision.confidence,
                "direction": edge_decision.direction,
                "fair_value": edge_decision.fair_value,
                "timestamp": time.time(),
            })

        phase_start = time.perf_counter()
        strategy_signals = _collect_cross_platform_strategy_signals(
            config=config,
            scanner=cross_platform_scanner,
        )
        statistical_signals = _collect_statistical_strategy_signals(
            config=config,
            candidate_markets=scanned_markets,
            ob_analyzer=ob_analyzer,
            detector=statistical_detector,
        )
        strategy_signals.extend(statistical_signals)
        fair_values_by_market = {
            signal.market_id: float(signal.payload.get("model_prob"))
            for signal in statistical_signals
            if signal.payload.get("model_prob") is not None
        }
        maker_signals = _collect_maker_strategy_signals(
            candidate_markets=scanned_markets,
            ob_analyzer=ob_analyzer,
            maker_strategy=maker_strategy,
            fair_values_by_market=fair_values_by_market,
            detector=statistical_detector,
        )
        strategy_signals.extend(maker_signals)

        active_markets_for_overlay = universe_markets if universe_markets else scanned_markets
        for strategy_signal in strategy_signals:
            submitted = orchestrator.submit_signal(
                strategy_signal,
                active_markets=active_markets_for_overlay,
                research_report=research_report,
                research_signals=research_signals,
            )
            signal_for_record = _find_pending_signal(orchestrator, strategy_signal) or strategy_signal
            overlay_payload = _find_pending_signal_overlay(orchestrator, strategy_signal)
            dash_state.append_opportunity({
                "arb_type": signal_for_record.signal_type,
                "mode": "signal",
                "stage": "signal",
                "event_title": signal_for_record.description,
                "total_cost": None,
                "net_edge": signal_for_record.expected_edge / 10_000.0,
                "edge_pct": signal_for_record.expected_edge / 100.0,
                "confidence": signal_for_record.confidence,
                "market_id": signal_for_record.market_id,
                "tier": signal_for_record.tier.name,
                "recommended_size_usdc": signal_for_record.recommended_size_usdc,
                "submitted": submitted,
                "timestamp": signal_for_record.timestamp,
            })
            if event_recorder.is_enabled:
                event_recorder.write_event(
                    "strategy_signals",
                    _serialize_strategy_signal(signal_for_record, submitted=submitted, research_overlay=overlay_payload),
                )

        cycle_timing["strategy_sec"] += time.perf_counter() - phase_start

        phase_start = time.perf_counter()
        for opp in opportunities:
            if _SHUTDOWN_EVENT.is_set():
                break

            arb_text = format_arb_opportunity_zh(opp)
            LOG.info("\n%s", arb_text)
            notifier.notify_arb_found(arb_text)

            target_size = min(
                config.default_order_size_usdc / opp.total_cost if opp.total_cost > 0 else 0,
                opp.max_executable_size,
            )

            verified = detector.verify_opportunity_with_depth(opp, target_size)
            if verified is None:
                LOG.info("深度验证失败，跳过")
                event_recorder.write_event("risk_events", {
                    "event": "depth_verification_failed",
                    "event_id": opp.event_id,
                    "arb_type": opp.arb_type.value,
                    "target_size": target_size,
                })
                continue
            event_recorder.write_event("opportunities", _serialize_opportunity_event(verified, stage="verified"))
            dash_state.append_opportunity({
                "arb_type": verified.arb_type.value,
                "mode": "theoretical",
                "stage": "verified",
                "event_title": verified.event_title,
                "total_cost": verified.total_cost,
                "net_edge": verified.net_edge,
                "edge_pct": verified.edge_pct,
                "confidence": verified.confidence,
                "max_size": verified.max_executable_size,
                "legs": len(verified.legs),
                "timestamp": time.time(),
            })

            can_trade, reason, adj_size = risk_mgr.pre_trade_check(verified, target_size)
            if not can_trade:
                LOG.info("风控拒绝: %s", reason)
                event_recorder.write_event("risk_events", {
                    "event": "pre_trade_reject",
                    "event_id": verified.event_id,
                    "arb_type": verified.arb_type.value,
                    "reason": reason,
                    "target_size": target_size,
                    "adjusted_size": adj_size,
                })
                continue

            balance_ok, balance_reason, _ = executor.ensure_sufficient_collateral(verified.total_cost * adj_size)
            if not balance_ok:
                LOG.info("余额校验拒绝: %s", balance_reason)
                event_recorder.write_event("risk_events", {
                    "event": "balance_reject",
                    "event_id": verified.event_id,
                    "arb_type": verified.arb_type.value,
                    "reason": balance_reason,
                    "adjusted_size": adj_size,
                })
                continue

            trades = executor.execute_arbitrage(verified, adj_size)
            risk_mgr.record_execution(verified, trades)
            live_execution_success = _is_live_execution_success(config, executor, verified, trades)
            trade_payload = _serialize_trade_execution(verified, trades, live_execution_success, adj_size)
            event_recorder.write_event("trades", trade_payload)
            dashboard_execution_success = bool(trade_payload.get("arb_success", False))
            if live_execution_success:
                total_arbs_executed += 1
                total_live_expected_profit += float(trade_payload.get("trade_outcome_estimate") or 0.0)
            elif bool(trade_payload.get("simulated", False)) and dashboard_execution_success:
                total_simulated_executed += 1
                total_simulated_expected_profit += float(trade_payload.get("trade_outcome_estimate") or 0.0)

            for trade_row in _build_dashboard_trade_rows(
                opp=verified,
                trades=trades,
                live_execution_success=live_execution_success,
                dashboard_execution_success=dashboard_execution_success,
            ):
                dash_state.append_trade(trade_row)

            filled = [t for t in trades if t.status.value == "filled"]
            if ai_advisor is not None and not config.dry_run:
                ai_advisor.record_trade_outcome(
                    _estimate_ai_trade_outcome(verified, trades, live_execution_success, adj_size)
                )
            if live_execution_success and filled:
                notifier.notify_trade_success(
                    event_title=opp.event_title,
                    arb_type=opp.arb_type.value,
                    filled_legs=len(filled),
                    total_legs=len(opp.legs),
                    expected_profit=opp.net_edge * adj_size,
                    simulated=bool(trade_payload.get("simulated", False)),
                )
            elif trades and not config.dry_run:
                failure_reasons = sorted({
                    str(getattr(trade, "error", "")).strip()
                    for trade in trades
                    if str(getattr(trade, "error", "")).strip()
                })
                notifier.notify_trade_failure(
                    event_title=opp.event_title,
                    filled_legs=len(filled),
                    total_legs=len(opp.legs),
                    simulated=bool(trade_payload.get("simulated", False)),
                    details="; ".join(failure_reasons[:2]),
                )
        cycle_timing["execution_sec"] += time.perf_counter() - phase_start

        if ai_advisor and ai_advisor.should_evaluate():
            phase_start = time.perf_counter()
            _run_ai_cycle(
                ai_advisor=ai_advisor,
                ctx_builder=ctx_builder,
                active_markets=universe_markets if universe_markets else scanned_markets,
                recent_trades=executor.get_recent_trades(),
                book_store=enhanced_store,
                vol_estimator=vol_estimator,
                edge_decision=edge_decision,
                risk_mgr=risk_mgr,
                orchestrator=orchestrator,
                dash_state=dash_state,
                config=config,
                research_report=research_report,
                research_signals=research_signals,
                event_recorder=event_recorder,
            )
            cycle_timing["ai_sec"] += time.perf_counter() - phase_start

        phase_start = time.perf_counter()
        # T2/T3 信号从 scanned_markets 生成（含 event markets），universe_markets 可能不含这些市场；
        # 合并两个列表以确保 _find_market_for_signal 能找到信号对应的市场。
        _scanned_ids = {m.condition_id for m in scanned_markets}
        execution_markets = scanned_markets + [
            m for m in active_markets_for_overlay if m.condition_id not in _scanned_ids
        ]
        for processed_signal in orchestrator.process_signals():
            executed, reason = _execute_strategy_signal(
                signal=processed_signal,
                config=config,
                active_markets=execution_markets,
                ob_analyzer=ob_analyzer,
                executor=executor,
                risk_mgr=risk_mgr,
                orchestrator=orchestrator,
                dash_state=dash_state,
                event_recorder=event_recorder,
                maker_strategy=maker_strategy,
            )
            if executed:
                continue
            orchestrator.record_processed(processed_signal)
            if event_recorder.is_enabled:
                event_recorder.write_event("strategy_executions", {
                    "tier": processed_signal.tier.name,
                    "signal_type": processed_signal.signal_type,
                    "market_id": processed_signal.market_id,
                    "status": "skipped",
                    "reason": reason,
                })
        cycle_timing["strategy_execution_sec"] = time.perf_counter() - phase_start

        if (
            portfolio_sync is not None
            and (time.time() - last_portfolio_sync_ts) >= config.portfolio_sync_interval_sec
        ):
            try:
                snapshot = portfolio_sync.refresh()
                risk_mgr.sync_portfolio_snapshot(
                    snapshot.positions,
                    realized_daily_pnl=snapshot.realized_daily_pnl,
                    synced_at=snapshot.synced_at,
                )
                last_portfolio_sync_ts = snapshot.synced_at
                if event_recorder.is_enabled:
                    event_recorder.write_event("risk_events", {
                        "event": "portfolio_sync",
                        "source_address": snapshot.source_address,
                        "positions": len(snapshot.positions),
                        "realized_daily_pnl": snapshot.realized_daily_pnl,
                        "synced_at": snapshot.synced_at,
                    })
            except Exception as exc:
                risk_mgr.mark_portfolio_sync_error(str(exc))
                LOG.warning("账户同步失败: %s", exc)
                if event_recorder.is_enabled:
                    event_recorder.write_event("risk_events", {
                        "event": "portfolio_sync_error",
                        "error": str(exc),
                        "ts": time.time(),
                    })

        risk_s = risk_mgr.state
        vol_snap = vol_estimator.snapshot()
        ws_status = _build_ws_status(
            config=config,
            enhanced_store=enhanced_store,
            ws_target_ids=ws_target_ids,
            phase_hint="scan_complete",
        )
        dash_state.update(
            cycle_count=cycle,
            arbs_found=total_arbs_found,
            arbs_executed=total_arbs_executed,
            markets_scanned=len(scanned_markets),
            universe_status={
                "universe_market_count": len(cached_universe_markets),
                "hot_market_pool_size": config.hot_market_pool_size,
                "hot_event_pool_size": config.hot_event_pool_size,
                "selected_market_count": len(scanned_markets),
                "selected_event_count": len(event_candidates),
                "focus_keywords": focus_keywords,
                "last_universe_refresh_ts": last_universe_refresh_ts or None,
                "universe_refreshed": universe_refreshed,
            },
            risk_state={
                "is_halted": risk_s.is_halted,
                "halt_reason": risk_s.halt_reason,
                "open_positions": risk_s.open_positions,
                "max_positions": config.max_open_positions,
                "total_exposure": risk_s.total_exposure,
                "daily_pnl": risk_s.daily_pnl,
                "consecutive_failures": risk_s.consecutive_failures,
                "portfolio_sync_enabled": config.portfolio_sync_enabled,
                "last_portfolio_sync_ts": risk_s.last_portfolio_sync_ts or None,
                "portfolio_sync_ok": risk_s.portfolio_sync_ok,
                "portfolio_sync_error": risk_s.portfolio_sync_error,
            },
            volatility=vol_snap,
            edge_decision=edge_decision.to_dict() if edge_decision else None,
            book_summary=enhanced_store.get_summary(),
            ws_status=ws_status,
            market_catalog=_summarize_market_catalog(universe_markets if universe_markets else scanned_markets),
            strategy_status=orchestrator.get_status(),
            execution_summary={
                "theoretical_opportunities": total_arbs_found,
                "live_successes": total_arbs_executed,
                "simulated_successes": total_simulated_executed,
                "live_profit_total": round(total_live_expected_profit, 6),
                "simulated_profit_total": round(total_simulated_expected_profit, 6),
            },
            research_signal_status={
                "enabled": research_signal_enabled,
                "count": len(research_signals),
                "topic_count": research_report.topic_count if research_report else 0,
                "row_count": research_report.row_count if research_report else 0,
                "cache_hit": research_report.cache_hit if research_report else False,
                "source_counts": dict(research_report.source_counts) if research_report else {},
                "items": [signal.to_dict() for signal in research_signals[: config.research_signal_max_items]],
            } if config.research_signal_enabled else {},
            backtest_last_report=_load_last_backtest_report(config.backtest_reports_dir),
            current_positions=[
                {
                    "token_id": position.token_id,
                    "condition_id": position.condition_id,
                    "outcome": position.outcome,
                    "size": position.size,
                    "avg_price": position.avg_price,
                    "current_value": position.current_value,
                    "unrealized_pnl": position.unrealized_pnl,
                }
                for position in risk_s.positions
            ],
        )
        dash_state.append_pnl_point({
            "timestamp": time.time(),
            "cumulative_pnl": risk_s.daily_pnl,
        })

        cycle_summary_payload = _emit_cycle_metrics(
            event_recorder=event_recorder,
            ob_analyzer=ob_analyzer,
            cycle_perf_start=cycle_perf_start,
            cycle_timing=cycle_timing,
            run_id=run_id,
            cycle=cycle,
            markets_scanned=len(scanned_markets),
            universe_market_count=len(cached_universe_markets),
            selected_event_count=len(event_candidates),
            arbs_found_total=total_arbs_found,
            arbs_executed_total=total_arbs_executed,
            ws_status=ws_status,
            research_count=len(research_signals),
            daily_pnl=risk_s.daily_pnl,
            open_positions=risk_s.open_positions,
            focus_keywords=focus_keywords,
            cycle_status="ok",
        )

        now_ts = time.time()
        if event_recorder.is_enabled and (now_ts - last_telemetry_heartbeat_ts) >= _TELEMETRY_HEARTBEAT_SEC:
            event_recorder.write_event("risk_events", cycle_summary_payload)
            last_telemetry_heartbeat_ts = now_ts

        notifier.observe_cycle(
            daily_pnl=risk_s.daily_pnl,
            open_positions=risk_s.open_positions,
            total_exposure=risk_s.total_exposure,
            is_halted=risk_s.is_halted,
            halt_reason=risk_s.halt_reason,
            now_ts=now_ts,
        )
        if risk_s.is_halted and risk_s.halt_reason:
            notifier.notify_fatal_error(
                f"风控已熔断\n原因: {risk_s.halt_reason}",
                error_key=f"risk_halt:{risk_s.halt_reason}",
                now_ts=now_ts,
            )
        notifier.maybe_notify_pnl_alert(
            daily_pnl=risk_s.daily_pnl,
            now_ts=now_ts,
        )
        notifier.maybe_notify_daily_summary(now_ts=now_ts)

        elapsed = time.time() - cycle_start
        if cycle % 100 == 0:
            LOG.info(
                "状态: 已扫描 %d 周期, 发现 %d 机会, 执行 %d 次, WS=%s, 本周期 %.1fs",
                cycle,
                total_arbs_found,
                total_arbs_executed,
                "连接" if ws_status.get("connected") else "未连接",
                elapsed,
            )

        sleep_time = max(0, config.scan_interval_sec - elapsed)
        if sleep_time > 0 and not _SHUTDOWN_EVENT.is_set():
            time.sleep(sleep_time)

    if ws_feed is not None:
        ws_feed.stop()
        LOG.info("WebSocket feed 已停止")
    if research_refresh_state.pending_future is not None:
        research_refresh_state.pending_future.cancel()
    if research_executor is not None:
        research_executor.shutdown(wait=False, cancel_futures=True)
    if event_recorder.is_enabled:
        event_recorder.write_event("risk_events", {
            "event": "shutdown",
            "run_id": run_id,
            "pid": os.getpid(),
            "cycle": cycle,
            "arbs_found_total": total_arbs_found,
            "arbs_executed_total": total_arbs_executed,
        })
    tick_recorder.close()
    event_recorder.close()
    dash_state.update(is_running=False)
    LOG.info("机器人已停止: run_id=%s。总计: %d 周期, %d 机会, %d 执行", run_id, cycle, total_arbs_found, total_arbs_executed)
    notifier.notify_shutdown(
        run_id=run_id,
        cycle_count=cycle,
        total_arbs_found=total_arbs_found,
        total_arbs_executed=total_arbs_executed,
        simulated_successes=total_simulated_executed,
    )


def _scan_cycle(
    detector: ArbitrageDetector,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    candidate_events: list[Any],
    universe_market_count: int,
    universe_refreshed: bool,
    progress_cb: Any | None = None,
) -> list[ArbOpportunity]:
    """单次扫描周期：扫描 hot pool 市场与事件."""
    opportunities: list[ArbOpportunity] = []
    if progress_cb is not None:
        progress_cb(
            phase="scanning_books",
            scanned_markets=len(candidate_markets),
            universe_markets=universe_market_count,
            universe_refreshed=universe_refreshed,
        )

    for idx, market in enumerate(candidate_markets, start=1):
        if len(market.tokens) == 2:
            opp = detector.scan_binary_market(market)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)
        if progress_cb is not None and (idx == 1 or idx % 25 == 0 or idx == len(candidate_markets)):
            progress_cb(
                phase="scanning_books",
                scanned_markets=len(candidate_markets),
                scanned_orderbooks=idx,
                universe_markets=universe_market_count,
                universe_refreshed=universe_refreshed,
                opportunities_found=len(opportunities),
            )

    if progress_cb is not None:
        progress_cb(
            phase="scanning_events",
            scanned_markets=len(candidate_markets),
            scanned_events=len(candidate_events),
            universe_markets=universe_market_count,
            universe_refreshed=universe_refreshed,
            opportunities_found=len(opportunities),
        )
    for event in candidate_events:
        if len(event.markets) >= 2:
            opp = detector.scan_multi_outcome_event(event)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)

    opportunities.sort(key=lambda o: o.net_edge, reverse=True)
    return opportunities


def _run_ai_cycle(
    *,
    ai_advisor: AIAdvisor,
    ctx_builder: MarketContextBuilder,
    active_markets: list[MarketInfo],
    recent_trades: list[Any],
    book_store: EnhancedBookStore,
    vol_estimator: VolEstimator,
    edge_decision: Any,
    risk_mgr: RiskManager,
    orchestrator: StrategyOrchestrator,
    dash_state: DashboardState,
    config: ArbConfig,
    research_report: ResearchSignalReport | dict | None = None,
    research_signals: list[Any] | None = None,
    event_recorder: EventRecorder | None = None,
) -> None:
    """在主循环中执行一次 AI 评估（同步包装 async 调用）."""
    market_catalog = _summarize_market_catalog(active_markets)
    context = ctx_builder.build(
        active_markets=active_markets,
        book_store=book_store,
        vol_estimator=vol_estimator,
        edge_signals=[edge_decision.to_dict()] if edge_decision else [],
        recent_trades=[_serialize_recent_trade(t) for t in recent_trades],
        risk_state=risk_mgr.state,
        research_report=research_report,
        research_signals=research_signals,
    )

    loop = _get_or_create_event_loop()
    try:
        decisions = loop.run_until_complete(
            asyncio.wait_for(ai_advisor.evaluate_markets(context), timeout=_AI_EVAL_TIMEOUT_SEC)
        )
    except TimeoutError:
        LOG.error("AI 评估超时: %.1fs", _AI_EVAL_TIMEOUT_SEC)
        dash_state.append_error({"message": "AI error: evaluation_timeout", "timestamp": time.time()})
        if event_recorder is not None:
            event_recorder.write_event("risk_events", {
                "event": "ai_evaluation_timeout",
                "timeout_sec": _AI_EVAL_TIMEOUT_SEC,
            })
        return
    except Exception as e:
        LOG.error("AI 评估异常: %s", e, exc_info=True)
        dash_state.append_error({"message": f"AI error: {e}", "timestamp": time.time()})
        if event_recorder is not None:
            event_recorder.write_event("risk_events", {
                "event": "ai_evaluation_error",
                "error": str(e),
            })
        return

    for dec in decisions:
        if dec.action == "HOLD":
            continue
        signal = StrategySignal(
            tier=StrategyTier.STATISTICAL_ARB,
            signal_type=f"ai_{dec.action.lower()}",
            market_id=dec.market_id,
            description=dec.reasoning[:120],
            expected_edge=dec.confidence * 100,
            confidence=dec.confidence,
            recommended_size_usdc=dec.recommended_size_pct * config.max_total_exposure,
            urgency=dec.urgency,
            payload={"action": dec.action},
        )
        submitted = orchestrator.submit_signal(
            signal,
            active_markets=active_markets,
            research_report=research_report,
            research_signals=research_signals,
        )
        overlay_payload = _find_pending_signal_overlay(orchestrator, signal)
        market_info = _lookup_market_snapshot(dec.market_id, market_catalog)
        dash_state.append_ai_decision({
            **dec.to_dict(),
            "market_question": market_info.get("question", ""),
            "decision_price": market_info.get("yes_price"),
            "submitted": submitted,
            "research_overlay": overlay_payload,
        })
        if event_recorder is not None:
            event_recorder.write_event("ai_decisions", {
                **dec.to_dict(),
                "market_question": market_info.get("question", ""),
                "decision_price": market_info.get("yes_price"),
                "submitted": submitted,
                "research_overlay": overlay_payload,
            })

    if config.ai_override_risk:
        try:
            adjustments = loop.run_until_complete(
                asyncio.wait_for(ai_advisor.adjust_risk_params(context), timeout=_AI_EVAL_TIMEOUT_SEC)
            )
            if adjustments:
                risk_mgr.apply_ai_adjustment(adjustments)
        except TimeoutError:
            LOG.error("AI 风控调整超时: %.1fs", _AI_EVAL_TIMEOUT_SEC)
            if event_recorder is not None:
                event_recorder.write_event("risk_events", {
                    "event": "ai_risk_timeout",
                    "timeout_sec": _AI_EVAL_TIMEOUT_SEC,
                })
        except Exception as e:
            LOG.error("AI 风控调整异常: %s", e, exc_info=True)
            if event_recorder is not None:
                event_recorder.write_event("risk_events", {
                    "event": "ai_risk_error",
                    "error": str(e),
                })

    dash_state.update(
        ai_status=ai_advisor.get_status(),
        strategy_status=orchestrator.get_status(),
    )


def _estimate_ai_trade_outcome(verified: ArbOpportunity, trades: list[Any], arb_success: bool, adj_size: float) -> float:
    if arb_success:
        filled_sizes = [float(t.fill_size or t.size or 0.0) for t in trades if getattr(t, "status", None) and t.status.value == "filled"]
        realized_size = min(filled_sizes) if filled_sizes else adj_size
        return verified.net_edge * realized_size

    realized_cost = 0.0
    for trade in trades:
        fill_size = float(getattr(trade, "fill_size", None) or 0.0)
        if fill_size <= 0:
            continue
        leg_cost = getattr(trade, "economic_cost", None)
        if leg_cost is None:
            leg_cost = getattr(trade, "price", 0.0)
        realized_cost += float(leg_cost) * fill_size

    if realized_cost > 0:
        return -realized_cost
    return -verified.total_cost * adj_size


def _has_simulated_trades(trades: list[Any]) -> bool:
    return any(bool(getattr(trade, "simulated", False)) for trade in trades)


def _reported_execution_success(*, live_execution_success: bool, trades: list[Any]) -> bool:
    if _has_simulated_trades(trades):
        filled = [
            trade for trade in trades
            if getattr(getattr(trade, "status", None), "value", "") == "filled"
        ]
        return bool(trades) and len(filled) == len(trades)
    return live_execution_success


def _find_pending_signal_overlay(orchestrator: StrategyOrchestrator, signal: StrategySignal) -> dict[str, Any]:
    pending = _find_pending_signal(orchestrator, signal)
    if pending is not None:
        return dict(pending.payload.get("research_overlay", {}))
    return {}


def _find_pending_signal(orchestrator: StrategyOrchestrator, signal: StrategySignal) -> StrategySignal | None:
    for pending in getattr(orchestrator, "_pending_signals", []):
        if (
            pending.market_id == signal.market_id
            and pending.signal_type == signal.signal_type
            and abs(float(pending.timestamp) - float(signal.timestamp)) < 1e-6
        ):
            return pending
    return None


def _serialize_opportunity_event(opp: ArbOpportunity, *, stage: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "arb_type": opp.arb_type.value,
        "event_id": opp.event_id,
        "event_title": opp.event_title,
        "total_cost": opp.total_cost,
        "net_edge": opp.net_edge,
        "edge_pct": opp.edge_pct,
        "confidence": opp.confidence,
        "max_executable_size": opp.max_executable_size,
        "legs": [
            {
                "token_id": leg.token_id,
                "condition_id": leg.condition_id,
                "outcome": leg.outcome,
                "side": leg.side.value,
                "price": leg.price,
                "execution_price": leg.execution_price,
                "economic_cost": leg.economic_cost,
                "size": leg.size,
                "available_size": leg.available_size,
            }
            for leg in opp.legs
        ],
    }


def _serialize_trade_execution(opp: ArbOpportunity, trades: list[Any], arb_success: bool, adj_size: float) -> dict[str, Any]:
    simulated = _has_simulated_trades(trades)
    reported_success = _reported_execution_success(
        live_execution_success=arb_success,
        trades=trades,
    )
    return {
        "arb_type": opp.arb_type.value,
        "event_id": opp.event_id,
        "event_title": opp.event_title,
        "arb_success": reported_success,
        "live_execution_success": arb_success,
        "simulated": simulated,
        "requested_size": adj_size,
        "expected_net_edge": opp.net_edge,
        "expected_total_cost": opp.total_cost,
        "trade_outcome_estimate": _estimate_ai_trade_outcome(opp, trades, reported_success, adj_size),
        "trades": [
            {
                "trade_id": getattr(trade, "trade_id", ""),
                "token_id": getattr(trade, "token_id", ""),
                "condition_id": getattr(trade, "condition_id", ""),
                "side": getattr(getattr(trade, "side", None), "value", ""),
                "status": getattr(getattr(trade, "status", None), "value", ""),
                "price": getattr(trade, "price", None),
                "size": getattr(trade, "size", None),
                "fill_price": getattr(trade, "fill_price", None),
                "fill_size": getattr(trade, "fill_size", None),
                "economic_cost": getattr(trade, "economic_cost", None),
                "order_id": getattr(trade, "order_id", None),
                "error": getattr(trade, "error", None),
                "rolled_back": getattr(trade, "rolled_back", False),
            }
            for trade in trades
        ],
    }


def _build_dashboard_trade_rows(
    *,
    opp: ArbOpportunity,
    trades: list[Any],
    live_execution_success: bool,
    dashboard_execution_success: bool,
) -> list[dict[str, Any]]:
    mode = "simulated" if _has_simulated_trades(trades) else "live"
    expected_profit = _estimate_ai_trade_outcome(
        opp,
        trades,
        dashboard_execution_success,
        min(
            [float(getattr(trade, "size", 0.0) or 0.0) for trade in trades] or [0.0]
        ),
    )
    rows: list[dict[str, Any]] = []
    for trade in trades:
        rows.append({
            "trade_id": getattr(trade, "trade_id", ""),
            "arb_id": getattr(trade, "arb_id", ""),
            "event_title": opp.event_title,
            "arb_type": opp.arb_type.value,
            "mode": mode,
            "execution_success": dashboard_execution_success,
            "live_execution_success": live_execution_success,
            "side": getattr(getattr(trade, "side", None), "value", ""),
            "price": getattr(trade, "price", None),
            "size": getattr(trade, "size", None),
            "status": getattr(getattr(trade, "status", None), "value", ""),
            "token_id": getattr(trade, "token_id", "")[:20],
            "outcome": getattr(trade, "outcome", None),
            "timestamp": getattr(trade, "timestamp", time.time()),
            "expected_profit": expected_profit,
        })
    return rows


def _serialize_strategy_signal(
    signal: StrategySignal,
    *,
    submitted: bool,
    research_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tier": signal.tier.name,
        "signal_type": signal.signal_type,
        "market_id": signal.market_id,
        "description": signal.description,
        "expected_edge": signal.expected_edge,
        "confidence": signal.confidence,
        "recommended_size_usdc": signal.recommended_size_usdc,
        "urgency": signal.urgency,
        "submitted": submitted,
        "research_overlay": dict(research_overlay or {}),
        "payload": dict(signal.payload),
        "timestamp": signal.timestamp,
    }


def _lookup_market_snapshot(market_id: str, market_catalog: dict[str, dict]) -> dict[str, Any]:
    if not market_id:
        return {}
    if market_id in market_catalog:
        return dict(market_catalog[market_id])
    for key, value in market_catalog.items():
        if key.startswith(market_id) or market_id.startswith(key):
            return dict(value)
    return {}


def _summarize_market_catalog(markets: list[MarketInfo], limit: int = 300) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for market in markets[:limit]:
        catalog[market.condition_id] = {
            "question": market.question,
            "yes_price": _resolve_market_yes_price(market),
            "volume_24h": market.volume_24h,
            "liquidity": market.liquidity,
            "updated_at": time.time(),
        }
    return catalog


def _resolve_market_yes_price(market: MarketInfo) -> float | None:
    for idx, token in enumerate(market.tokens):
        outcome = (token.outcome or "").strip().lower()
        if outcome == "yes":
            if 0.0 < float(token.price or 0.0) < 1.0:
                return float(token.price)
            if idx < len(market.outcome_prices):
                price = float(market.outcome_prices[idx])
                if 0.0 < price < 1.0:
                    return price

    for price in market.outcome_prices:
        numeric = float(price)
        if 0.0 < numeric < 1.0:
            return numeric

    for token in market.tokens:
        numeric = float(token.price or 0.0)
        if 0.0 < numeric < 1.0:
            return numeric
    return None


def _is_live_execution_success(
    config: ArbConfig,
    executor: ExecutionEngine,
    opp: ArbOpportunity,
    trades: list[Any],
) -> bool:
    if config.dry_run:
        return False
    return executor.is_successful_execution(opp, trades)


def _get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """获取或创建事件循环，兼容在非 async 上下文中调用."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


def _load_last_backtest_report(backtest_reports_dir: str) -> dict:
    from pathlib import Path
    import json

    output_dir = Path(backtest_reports_dir)
    if not output_dir.exists():
        return {"enabled": False, "reports_dir": backtest_reports_dir}

    report_files = sorted(output_dir.glob("*_report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not report_files:
        return {"enabled": False, "reports_dir": backtest_reports_dir}

    try:
        data = json.loads(report_files[0].read_text(encoding="utf-8"))
        data["enabled"] = True
        data["path"] = str(report_files[0])
        trades_path = report_files[0].with_name(report_files[0].name.replace("_report.json", "_trades.jsonl"))
        if trades_path.exists():
            preview_rows = []
            for line in trades_path.read_text(encoding="utf-8").splitlines()[-5:]:
                if not line.strip():
                    continue
                preview_rows.append(json.loads(line))
            data["trades_path"] = str(trades_path)
            data["recent_trade_rows"] = preview_rows
        return data
    except Exception:
        return {"enabled": True, "reports_dir": backtest_reports_dir, "error": "report_parse_failed"}


def _serialize_recent_trade(trade: Any) -> dict:
    if hasattr(trade, "__dict__"):
        return {
            "trade_id": getattr(trade, "trade_id", ""),
            "token_id": getattr(trade, "token_id", ""),
            "status": getattr(getattr(trade, "status", None), "value", ""),
            "price": getattr(trade, "price", None),
            "size": getattr(trade, "size", None),
            "timestamp": getattr(trade, "timestamp", None),
        }
    return dict(trade)


def _build_ws_status(
    *,
    config: ArbConfig,
    enhanced_store: EnhancedBookStore,
    ws_target_ids: list[str],
    phase_hint: str,
    scanned_orderbooks: int = 0,
    scanned_events: int = 0,
) -> dict:
    book_snapshot = enhanced_store.snapshot()
    connected = (
        bool(book_snapshot.get("connected"))
        and book_snapshot.get("ts_ms") is not None
        and book_snapshot.get("ts_ms", 0) > 0
    )
    if connected:
        phase = "connected"
    elif not config.ws_enabled:
        phase = "disabled"
    elif ws_target_ids:
        phase = "initializing"
    else:
        phase = phase_hint
    return {
        "enabled": config.ws_enabled,
        "connected": connected,
        "market_id": book_snapshot.get("market_id"),
        "subscribed_tokens": len(ws_target_ids),
        "phase": phase,
        "scanned_orderbooks": scanned_orderbooks,
        "scanned_events": scanned_events,
    }
