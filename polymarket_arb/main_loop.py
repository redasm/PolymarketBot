"""主循环：协调市场扫描、套利检测、执行和通知的完整流程.

循环步骤:
1. 从 Gamma API 拉取活跃市场列表
2. 对每个二元市场执行快速套利扫描（best ask 级别）
3. 对多结果事件执行多腿套利扫描
4. 对发现的机会用 VWAP 做深度验证
5. 风控预检查
6. 执行交易（或 dry-run 记录）
7. 发送 Telegram 通知
8. 休眠后重复
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import signal
import threading
import time
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
from polymarket_arb.models import ArbOpportunity, ArbType, MarketInfo, ResearchSignalReport
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)
from polymarket_arb.telegram_notifier import TelegramNotifier
from polymarket_arb.tick_recorder import TickRecorder
from polymarket_arb.volatility_estimator import VolEstimator
from polymarket_arb.websocket_feed import OrderBookMirror, WebSocketFeed

if TYPE_CHECKING:
    from research_signal.service import ResearchSignalService

LOG = logging.getLogger("main_loop")

_SHUTDOWN_EVENT = threading.Event()
_AI_EVAL_TIMEOUT_SEC = 20.0
_TELEMETRY_HEARTBEAT_SEC = 60.0


def _signal_handler(sig: int, frame: Any) -> None:
    LOG.info("收到信号 %d，准备优雅退出…", sig)
    _SHUTDOWN_EVENT.set()


def _log_startup_summary(config: ArbConfig) -> None:
    safe_cfg = config.dump_safe()
    LOG.info("=" * 60)
    LOG.info("Polymarket 套利机器人启动")
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
        "数据: ws=%s ws_markets=%d tick_record=%s telemetry=%s cleanup=%s",
        config.ws_enabled,
        config.ws_max_markets,
        config.tick_record_enabled,
        config.telemetry_record_enabled,
        config.data_cleanup_enabled,
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
            knowledge_base_dir=config.research_signal_knowledge_dir,
            knowledge_base_enabled=config.research_signal_knowledge_enabled,
            knowledge_max_matches=config.research_signal_knowledge_max_matches,
        )
    except Exception as e:
        LOG.error("Research Signal 功能已禁用: 初始化失败: %s", e, exc_info=True)
        return None


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
    return any(keyword in text for keyword in keywords)


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
    _log_startup_summary(config)

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
        retry_count=config.orderbook_retry_count,
        retry_delay_sec=config.orderbook_retry_delay_sec,
    )
    detector = ArbitrageDetector(config, ob_analyzer)
    executor = ExecutionEngine(config, trading_client or ro_client)
    risk_mgr = RiskManager(config)
    notifier = TelegramNotifier(config)

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

    dash_state = DashboardState()
    dash_state.update(
        is_dry_run=config.dry_run,
        scan_interval=config.scan_interval_sec,
    )
    if config.dashboard_enabled:
        start_dashboard_server(dash_state, port=config.dashboard_port)
        LOG.info("Dashboard 已启动: http://127.0.0.1:%d", config.dashboard_port)

    notifier.send(
        f"🤖 套利机器人已启动\n"
        f"模式: {'DRY RUN' if config.dry_run else 'LIVE'}\n"
        f"最小利润: ${config.min_edge_usd} / {config.min_edge_pct}%\n"
        f"扫描间隔: {config.scan_interval_sec}s\n"
        f"WebSocket: {'启用' if config.ws_enabled else '禁用'}",
        category="startup",
        force=True,
    )

    cycle = 0
    total_arbs_found = 0
    total_arbs_executed = 0
    consecutive_api_errors = 0

    while not _SHUTDOWN_EVENT.is_set():
        cycle += 1
        cycle_start = time.time()
        scanned_markets: list[MarketInfo] = []
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
            cached_universe_markets, cached_universe_events, last_universe_refresh_ts, universe_refreshed = _refresh_market_universe(
                scanner=scanner,
                config=config,
                cached_markets=cached_universe_markets,
                cached_events=cached_universe_events,
                last_refresh_ts=last_universe_refresh_ts,
            )
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
            universe_markets = list(cached_universe_markets)
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
            if consecutive_api_errors >= 10:
                LOG.error("连续 %d 次 API 错误，暂停 60 秒", consecutive_api_errors)
                notifier.notify_error(f"连续 {consecutive_api_errors} 次 API 错误")
                time.sleep(60)
            else:
                time.sleep(config.scan_interval_sec)
            continue

        # --- WebSocket 启动 / 刷新 ---
        if config.ws_enabled and scanned_markets:
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
        if research_signal_service is not None:
            research_report = research_signal_service.collect_report(
                universe_markets[: config.research_signal_max_items] if universe_markets else scanned_markets[: config.research_signal_max_items],
                config.research_signal_window_sec,
            )
            research_signals = research_report.signals
            scanner.enrich_markets_with_research(
                universe_markets if universe_markets else scanned_markets,
                research_signal_service,
                window_sec=config.research_signal_window_sec,
                report=research_report,
            )

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

            trades = executor.execute_arbitrage(verified, adj_size)
            if not config.dry_run:
                risk_mgr.record_execution(verified, trades)
            arb_success = _is_live_execution_success(config, executor, verified, trades)
            event_recorder.write_event("trades", _serialize_trade_execution(verified, trades, arb_success, adj_size))
            if arb_success:
                total_arbs_executed += 1

            if not config.dry_run:
                for t in trades:
                    dash_state.append_trade({
                        "trade_id": t.trade_id,
                        "arb_id": t.arb_id,
                        "side": t.side.value,
                        "price": t.price,
                        "size": t.size,
                        "status": t.status.value,
                        "token_id": t.token_id[:20],
                        "timestamp": t.timestamp,
                    })

            filled = [t for t in trades if t.status.value == "filled"]
            if ai_advisor is not None and not config.dry_run:
                ai_advisor.record_trade_outcome(
                    _estimate_ai_trade_outcome(verified, trades, arb_success, adj_size)
                )
            if arb_success and filled:
                trade_msg = (
                    f"✅ 套利已执行\n"
                    f"事件: {opp.event_title}\n"
                    f"类型: {opp.arb_type.value}\n"
                    f"腿数: {len(filled)}/{len(opp.legs)}\n"
                    f"预期净利: ${opp.net_edge * adj_size:.4f}"
                )
                notifier.notify_trade(trade_msg)
            elif trades and not config.dry_run:
                notifier.notify_error(
                    f"套利执行未完成，已进入失败处理\n"
                    f"事件: {opp.event_title}\n"
                    f"已成交腿数: {len(filled)}/{len(opp.legs)}"
                )

        edge_decision = edge_engine.evaluate(enhanced_store, vol_estimator)
        if edge_decision.direction != "NONE":
            dash_state.append_opportunity({
                "arb_type": "edge_engine",
                "event_title": f"[Edge] {edge_decision.market_id or 'active_market'}",
                "total_cost": edge_decision.market_price,
                "net_edge": edge_decision.edge_bps / 10000.0,
                "edge_pct": edge_decision.edge_bps / 100.0,
                "confidence": edge_decision.confidence,
                "direction": edge_decision.direction,
                "fair_value": edge_decision.fair_value,
                "timestamp": time.time(),
            })

        if ai_advisor and ai_advisor.should_evaluate():
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
            },
            volatility=vol_snap,
            edge_decision=edge_decision.to_dict() if edge_decision else None,
            book_summary=enhanced_store.get_summary(),
            ws_status=ws_status,
            market_catalog=_summarize_market_catalog(universe_markets if universe_markets else scanned_markets),
            strategy_status=orchestrator.get_status(),
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
        )
        dash_state.append_pnl_point({
            "timestamp": time.time(),
            "cumulative_pnl": risk_s.daily_pnl,
        })

        now_ts = time.time()
        if event_recorder.is_enabled and (now_ts - last_telemetry_heartbeat_ts) >= _TELEMETRY_HEARTBEAT_SEC:
            event_recorder.write_event("risk_events", {
                "event": "cycle_summary",
                "cycle": cycle,
                "markets_scanned": len(scanned_markets),
                "universe_market_count": len(cached_universe_markets),
                "selected_event_count": len(event_candidates),
                "arbs_found_total": total_arbs_found,
                "arbs_executed_total": total_arbs_executed,
                "ws_connected": ws_status.get("connected", False),
                "ws_tokens": ws_status.get("subscribed_tokens", 0),
                "research_count": len(research_signals),
                "daily_pnl": risk_s.daily_pnl,
                "open_positions": risk_s.open_positions,
                "focus_keywords": focus_keywords,
            })
            last_telemetry_heartbeat_ts = now_ts

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
            notifier.notify_status(risk_mgr.format_status_zh())

        sleep_time = max(0, config.scan_interval_sec - elapsed)
        if sleep_time > 0 and not _SHUTDOWN_EVENT.is_set():
            time.sleep(sleep_time)

    if ws_feed is not None:
        ws_feed.stop()
        LOG.info("WebSocket feed 已停止")
    tick_recorder.close()
    event_recorder.close()
    dash_state.update(is_running=False)
    LOG.info("机器人已停止。总计: %d 周期, %d 机会, %d 执行", cycle, total_arbs_found, total_arbs_executed)
    notifier.send("🛑 套利机器人已停止", category="shutdown", force=True)


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

    for signal in orchestrator.process_signals():
        orchestrator.record_processed(signal)

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


def _find_pending_signal_overlay(orchestrator: StrategyOrchestrator, signal: StrategySignal) -> dict[str, Any]:
    for pending in getattr(orchestrator, "_pending_signals", []):
        if (
            pending.market_id == signal.market_id
            and pending.signal_type == signal.signal_type
            and abs(float(pending.timestamp) - float(signal.timestamp)) < 1e-6
        ):
            return dict(pending.payload.get("research_overlay", {}))
    return {}


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
    return {
        "arb_type": opp.arb_type.value,
        "event_id": opp.event_id,
        "event_title": opp.event_title,
        "arb_success": arb_success,
        "requested_size": adj_size,
        "expected_net_edge": opp.net_edge,
        "expected_total_cost": opp.total_cost,
        "trade_outcome_estimate": _estimate_ai_trade_outcome(opp, trades, arb_success, adj_size),
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
