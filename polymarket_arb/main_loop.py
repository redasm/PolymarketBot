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

from concurrent.futures import Future, ThreadPoolExecutor
import json
import logging
import os
import signal
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

from polymarket_arb.ai_advisor import AIAdvisor, create_ai_advisor
from polymarket_arb.ai_context import MarketContextBuilder
from polymarket_arb.arbitrage_detector import ArbitrageDetector
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.client_factory import build_readonly_client, build_trading_client
from polymarket_arb.config import ArbConfig
from polymarket_arb.data_janitor import DataJanitor
from polymarket_arb.dashboard_api import DashboardState, start_dashboard_server
from polymarket_arb.edge_engine import EdgeEngine
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.logger_setup import setup_logging
from polymarket_arb.main_helpers.ai_cycle import (
    AI_EVAL_TIMEOUT_SEC as _AI_EVAL_TIMEOUT_SEC,
    run_ai_cycle as _run_ai_cycle,
)
from polymarket_arb.main_helpers.cycle_runners import (
    find_pending_signal as _find_pending_signal,
    find_pending_signal_overlay as _find_pending_signal_overlay,
    refresh_market_universe as _refresh_market_universe,
    scan_cycle as _scan_cycle,
    start_ws_feed as _start_ws_feed,
)
from polymarket_arb.main_helpers.flow_aggregator import FlowAggregator, FlowIngest
from polymarket_arb.main_helpers.order_sync import (
    cancel_stale_maker_orders as _cancel_stale_maker_orders,
    sync_live_order_statuses as _sync_live_order_statuses,
)
from polymarket_arb.main_helpers.directional_opportunity import (
    build_directional_opportunity_from_signal as _build_directional_opportunity_from_signal,
)
from polymarket_arb.main_helpers.cycle_telemetry import (
    build_cycle_summary_payload as _build_cycle_summary_payload,
    emit_cycle_metrics as _emit_cycle_metrics,
)
from polymarket_arb.main_helpers.cli_setup import (
    build_run_instance_id as _build_run_instance_id,
    create_cross_platform_scanner as _create_cross_platform_scanner,
    create_research_signal_service as _create_research_signal_service,
    get_or_create_event_loop as _get_or_create_event_loop,
    load_last_backtest_report as _load_last_backtest_report,
    log_startup_summary as _log_startup_summary,
    parse_extra_rss_feeds as _parse_extra_rss_feeds,
    parse_http_json_sources as _parse_http_json_sources,
    round_timing as _round_timing,
)
from polymarket_arb.main_helpers.research_refresh import (
    RESEARCH_RESUBMIT_COOLDOWN_SEC as _RESEARCH_RESUBMIT_COOLDOWN_SEC,
    ResearchRefreshState as _ResearchRefreshState,
    advance_research_refresh as _advance_research_refresh,
    research_market_sample as _research_market_sample,
    research_market_signature as _research_market_signature,
)
from polymarket_arb.main_helpers.signal_collectors import (
    collect_cross_platform_strategy_signals as _collect_cross_platform_strategy_signals,
    collect_maker_strategy_signals as _collect_maker_strategy_signals,
    collect_statistical_strategy_signals as _collect_statistical_strategy_signals,
)
from polymarket_arb.main_helpers.signal_helpers import (
    apply_maker_fill_to_inventory as _apply_maker_fill_to_inventory,
    build_t2_related_market_context as _build_t2_related_market_context,
    evaluate_t2_market_quality as _evaluate_t2_market_quality,
    extract_market_deadline as _extract_market_deadline,
    extract_market_temporal_stem as _extract_market_temporal_stem,
    find_market_for_signal as _find_market_for_signal,
    resolve_strategy_signal_action as _resolve_strategy_signal_action,
    set_signal_execution_check as _set_signal_execution_check,
    spread_bps_from_snapshot as _spread_bps_from_snapshot,
    sum_trade_exposure as _sum_trade_exposure,
)
from polymarket_arb.main_helpers.signal_telemetry import StrategySignalTelemetryCompressor
from polymarket_arb.main_helpers.virtual_fill_emitter import (
    VirtualFillEmitter as _VirtualFillEmitter,
)
from polymarket_arb.main_helpers.strategy_execution import (
    ExecutionDelta,
    execute_strategy_signal as _execute_strategy_signal,
)
from polymarket_arb.main_helpers.t0_execution import (
    execute_t0_opportunity as _execute_t0_opportunity,
)
from polymarket_arb.main_helpers.dashboard_cycle_payload import (
    build_dashboard_cycle_payload as _build_dashboard_cycle_payload,
)
from polymarket_arb.main_helpers.dirty_market_tracker import DirtyMarketTracker
from polymarket_arb.main_helpers.dashboard_serializers import (
    build_dashboard_trade_rows as _build_dashboard_trade_rows,
    build_ws_status as _build_ws_status,
    estimate_ai_trade_outcome as _estimate_ai_trade_outcome,
    is_live_execution_success as _is_live_execution_success,
    serialize_opportunity_event as _serialize_opportunity_event,
    serialize_strategy_signal as _serialize_strategy_signal,
    serialize_trade_execution as _serialize_trade_execution,
    summarize_market_catalog as _summarize_market_catalog,
)
from polymarket_arb.main_helpers.scan_focus import (
    event_focus_text as _event_focus_text,
    event_priority_score as _event_priority_score,
    focus_keywords as _focus_keywords,
    market_focus_text as _market_focus_text,
    market_priority_score as _market_priority_score,
    matches_focus as _matches_focus,
    merge_focus_event_markets as _merge_focus_event_markets,
    prime_candidate_orderbooks as _prime_candidate_orderbooks,
    select_event_candidates as _select_event_candidates,
    select_scan_candidates as _select_scan_candidates,
    select_ws_targets as _select_ws_targets,
)
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.models import (
    MarketInfo,
    ResearchSignalReport,
)
from polymarket_arb.notifier import NotificationManager
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.portfolio_sync import PortfolioSync
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.maker_strategy import DynamicSpreadCalculator, MakerStrategy
from polymarket_arb.strategies.recent_exit_cooldown import make_recent_exit_cooldown_store
from polymarket_arb.strategies.t2_exit_manager import T2ExitManager, t2_exit_telemetry
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
_TELEMETRY_HEARTBEAT_SEC = 60.0


def _signal_handler(sig: int, frame: Any) -> None:
    LOG.info("收到信号 %d，准备优雅退出…", sig)
    _SHUTDOWN_EVENT.set()


def _refresh_portfolio_snapshot(
    *,
    portfolio_sync: PortfolioSync | None,
    risk_mgr: RiskManager,
    event_recorder: EventRecorder,
    last_portfolio_sync_ts: float,
    forced: bool = False,
) -> tuple[float, bool]:
    """Refresh account ground truth and reconcile risk state.

    Returns `(last_sync_ts, ok)`. Forced refreshes are used after rollback
    transport failures, where a remote order may still be live and the next
    opening trade should not proceed against stale in-memory exposure.
    """
    if portfolio_sync is None:
        if forced:
            message = "forced_portfolio_resync_requested_but_not_configured"
            risk_mgr.mark_portfolio_sync_error(message)
            LOG.error("强制账户同步失败: portfolio_sync 未配置")
            if event_recorder.is_enabled:
                event_recorder.write_event("risk_events", {
                    "event": "portfolio_sync_error",
                    "forced": True,
                    "error": message,
                    "ts": time.time(),
                })
            return last_portfolio_sync_ts, False
        return last_portfolio_sync_ts, True

    try:
        snapshot = portfolio_sync.refresh()
        risk_mgr.sync_portfolio_snapshot(
            snapshot.positions,
            realized_daily_pnl=snapshot.realized_daily_pnl,
            synced_at=snapshot.synced_at,
        )
        if event_recorder.is_enabled:
            event_recorder.write_event("risk_events", {
                "event": "portfolio_sync",
                "forced": forced,
                "source_address": snapshot.source_address,
                "positions": len(snapshot.positions),
                "realized_daily_pnl": snapshot.realized_daily_pnl,
                "synced_at": snapshot.synced_at,
            })
        return snapshot.synced_at, True
    except Exception as exc:
        risk_mgr.mark_portfolio_sync_error(str(exc))
        LOG.warning("账户同步失败%s: %s", " (forced)" if forced else "", exc)
        if event_recorder.is_enabled:
            event_recorder.write_event("risk_events", {
                "event": "portfolio_sync_error",
                "forced": forced,
                "error": str(exc),
                "ts": time.time(),
            })
        return last_portfolio_sync_ts, False


def _handle_forced_portfolio_resync(
    *,
    executor: ExecutionEngine,
    portfolio_sync: PortfolioSync | None,
    risk_mgr: RiskManager,
    event_recorder: EventRecorder,
    last_portfolio_sync_ts: float,
) -> tuple[float, bool]:
    if not executor.consume_force_portfolio_resync():
        return last_portfolio_sync_ts, True
    LOG.warning("检测到回滚撤单 transport 失败，先强制同步账户状态")
    return _refresh_portfolio_snapshot(
        portfolio_sync=portfolio_sync,
        risk_mgr=risk_mgr,
        event_recorder=event_recorder,
        last_portfolio_sync_ts=last_portfolio_sync_ts,
        forced=True,
    )


# Startup / parsing / boot helpers (build_run_instance_id, log_startup_summary,
# parse_extra_rss_feeds, parse_http_json_sources, create_research_signal_service,
# create_cross_platform_scanner, round_timing, get_or_create_event_loop,
# load_last_backtest_report) were extracted to
# `polymarket_arb.main_helpers.cli_setup` and re-imported above under their
# underscore aliases to keep the existing call graph stable.


# Scan-focus / candidate-ranking helpers live in
# `polymarket_arb.main_helpers.scan_focus`. They are re-imported above
# under their original underscore names so the rest of this module keeps
# its existing call graph.


# Per-tier signal collectors (cross-platform / statistical / maker) and
# the per-tier executor (`execute_strategy_signal`) live in
# `polymarket_arb.main_helpers.{signal_collectors, strategy_execution}`.
# Re-imported above under their underscore aliases so the existing call
# graph stays unchanged.


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
        flow_inventory_weight=config.t3_flow_bias_inventory_weight,
    )
    cross_platform_scanner = _create_cross_platform_scanner(config, ob_analyzer)
    tick_recorder = TickRecorder(
        output_dir=config.tick_record_dir,
        enabled=config.tick_record_enabled,
    )
    event_recorder = EventRecorder(
        output_dir=config.telemetry_record_dir,
        enabled=config.telemetry_record_enabled,
        async_write=config.telemetry_async_write,
        queue_size=config.telemetry_async_queue_size,
    )
    if config.dry_run:
        virtual_fill_emitter = _VirtualFillEmitter(
            event_recorder=event_recorder,
            book_snapshot_provider=lambda token_id: ob_analyzer.get_snapshot(
                token_id, allow_rest_fallback=False, count_request=False
            ),
            taker_fee_rate=config.polymarket_taker_fee_rate,
        )
        executor.set_virtual_fill_emitter(virtual_fill_emitter)
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
            "maker_enabled": config.maker_strategy_enabled,
        })
    if config.data_cleanup_enabled:
        LOG.info("Data 定期清理已开启: interval=%.0fs", config.data_cleanup_interval_sec)

    orchestrator = StrategyOrchestrator(
        total_bankroll=config.max_total_exposure,
        max_signals_per_market_per_hour=config.t2_max_signals_per_market_per_hour,
    )
    flow_aggregator: Optional[FlowAggregator] = None
    flow_ingest: Optional[FlowIngest] = None
    if config.t3_flow_bias_enabled:
        flow_aggregator = FlowAggregator(
            window_sec=config.t3_flow_bias_window_sec,
            min_trades=config.t3_flow_bias_min_trades,
            strong_threshold=config.t3_flow_bias_strong_threshold,
            state_file=config.t3_flow_state_file or None,
        )
        flow_ingest = FlowIngest(flow_aggregator)
        LOG.info(
            "T3 flow-bias 数据基础已启用: window=%.0fs min_trades=%d strong_threshold=%.2f state=%s",
            config.t3_flow_bias_window_sec,
            config.t3_flow_bias_min_trades,
            config.t3_flow_bias_strong_threshold,
            config.t3_flow_state_file or "(in-memory)",
        )

    cooldown_store = make_recent_exit_cooldown_store(config)
    if cooldown_store.cooldown_sec > 0:
        LOG.info(
            "post-exit cooldown 启用: file=%s ttl=%.0fs (已加载 %d 条历史)",
            config.t2_recent_exits_state_file,
            cooldown_store.cooldown_sec,
            len(cooldown_store.snapshot()),
        )
    t2_exit_manager = T2ExitManager(
        config=config,
        executor=executor,
        ob_analyzer=ob_analyzer,
        risk_manager=risk_mgr,
        notifier=notifier,
        cooldown_store=cooldown_store,
    )
    LOG.info(
        "T2 退出策略: stop_loss=%.0fbps tp_capture=%.0f%% max_hold=%.0fs eval=%.0fs optimal_stopping=%s",
        config.t2_stop_loss_bps,
        config.t2_take_profit_capture_pct * 100,
        config.t2_max_hold_sec,
        config.t2_exit_eval_interval_sec,
        config.t2_optimal_stopping_enabled,
    )
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
    # P0-dirty: producer side lives in the WS callback thread,
    # consumer side is the main loop's scan_cycle priority reorder +
    # early sleep wake. ``wake_threshold`` from config; default 1
    # means "wake on any change".
    dirty_market_tracker = DirtyMarketTracker(
        wake_threshold=config.dirty_market_wake_threshold,
    )
    last_vol_feed_ts = 0.0
    cached_universe_markets: list[MarketInfo] = []
    cached_universe_events: list[Any] = []
    last_universe_refresh_ts = 0.0
    # Reuse the candidate selection between universe refreshes — selecting
    # ~5 events from 369 markets took ~0.18s × 8000 cycles/day = 24min CPU
    # spent re-doing identical work. Recompute only when the universe
    # itself changed.
    cached_scanned_markets: list[MarketInfo] = []
    cached_event_candidates: list[Any] = []
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
        max_order_size_usdc=config.max_order_size_usdc,
        max_exposure_per_market=config.max_exposure_per_market,
        max_total_exposure=config.max_total_exposure,
        max_daily_loss=config.max_daily_loss,
        max_open_positions=config.max_open_positions,
    )

    cycle = 0
    total_theoretical_opportunities = 0
    total_live_successes = 0
    total_simulated_successes = 0
    total_live_submissions = 0
    total_simulated_submissions = 0
    total_live_expected_profit = 0.0
    total_simulated_expected_profit = 0.0
    # P0-5: T0 structural arbs vs T1/T2/T3 directional signals are mixed
    # into `total_theoretical_opportunities` for backward compat. Track
    # them separately so cycle_metrics can answer "how many T0 arbs did
    # the detector find today" without grepping signal NDJSON.
    total_t0_opportunities = 0
    total_directional_signals = 0
    consecutive_api_errors = 0
    # Daily counters that roll over at UTC midnight. Lifetime totals above
    # are useful for run-level summaries but operators reading hourly
    # telemetry want to know how many opportunities materialised *today*.
    today_theoretical_opportunities = 0
    today_live_successes = 0
    today_t0_opportunities = 0
    today_directional_signals = 0
    today_utc_date = ""
    signal_telemetry = StrategySignalTelemetryCompressor(cooldown_sec=60.0)

    while not _SHUTDOWN_EVENT.is_set():
        cycle += 1
        cycle_start = time.time()
        cycle_perf_start = time.perf_counter()
        # Roll today's counters at UTC midnight.
        current_utc_date = time.strftime("%Y-%m-%d", time.gmtime(cycle_start))
        if current_utc_date != today_utc_date:
            today_utc_date = current_utc_date
            today_theoretical_opportunities = 0
            today_live_successes = 0
            today_t0_opportunities = 0
            today_directional_signals = 0
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
            if universe_refreshed or not cached_scanned_markets:
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
                cached_scanned_markets = scanned_markets
                cached_event_candidates = event_candidates
            else:
                scanned_markets = cached_scanned_markets
                event_candidates = cached_event_candidates
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
                            ws_feed, ws_mirror = _start_ws_feed(
                                targets,
                                enhanced_store,
                                tick_recorder,
                                flow_ingest=flow_ingest,
                                dirty_tracker=dirty_market_tracker,
                            )
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
            priority_dirty = dirty_market_tracker.drain()
            opportunities = _scan_cycle(
                detector=detector,
                config=config,
                candidate_markets=scanned_markets,
                candidate_events=event_candidates,
                universe_market_count=len(cached_universe_markets),
                universe_refreshed=universe_refreshed,
                priority_condition_ids=priority_dirty,
                progress_cb=lambda **kwargs: dash_state.update(
                    markets_scanned=kwargs.get("scanned_markets", 0),
                    # Dashboard "arbs_found" counter should reflect actual
                    # T0 structural opportunities, not the mixed
                    # `total_theoretical_opportunities` that includes
                    # T2/T3 directional signals.
                    arbs_found=total_t0_opportunities + kwargs.get("opportunities_found", 0),
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
                theoretical_opportunities_total=total_theoretical_opportunities,
                live_successes_total=total_live_successes,
                simulated_successes_total=total_simulated_successes,
                live_submissions_total=total_live_submissions,
                simulated_submissions_total=total_simulated_submissions,
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
                unrealized_pnl=risk_mgr.state.unrealized_pnl,
                total_pnl=risk_mgr.state.total_pnl,
                current_position_value=risk_mgr.state.current_position_value,
                cycle_status="error",
                theoretical_opportunities_today=today_theoretical_opportunities,
                t0_opportunities_total=total_t0_opportunities,
                t0_opportunities_today=today_t0_opportunities,
                directional_signals_total=total_directional_signals,
                directional_signals_today=today_directional_signals,
                live_successes_today=today_live_successes,
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
            total_theoretical_opportunities += len(opportunities)
            today_theoretical_opportunities += len(opportunities)
            total_t0_opportunities += len(opportunities)
            today_t0_opportunities += len(opportunities)
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
        maker_signals = (
            _collect_maker_strategy_signals(
                candidate_markets=scanned_markets,
                ob_analyzer=ob_analyzer,
                maker_strategy=maker_strategy,
                fair_values_by_market=fair_values_by_market,
                detector=statistical_detector,
                flow_aggregator=flow_aggregator,
            )
            if config.maker_strategy_enabled
            else []
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
                signal_payload = _serialize_strategy_signal(
                    signal_for_record,
                    submitted=submitted,
                    research_overlay=overlay_payload,
                )
                for compressed_payload in signal_telemetry.consume(signal_payload):
                    event_recorder.write_event("strategy_signals", compressed_payload)

        cycle_timing["strategy_sec"] += time.perf_counter() - phase_start

        phase_start = time.perf_counter()
        execution_blocked_by_forced_sync = False
        for opp in opportunities:
            if _SHUTDOWN_EVENT.is_set():
                break
            t0_delta = _execute_t0_opportunity(
                opp=opp,
                config=config,
                detector=detector,
                executor=executor,
                risk_mgr=risk_mgr,
                notifier=notifier,
                ai_advisor=ai_advisor,
                dash_state=dash_state,
                event_recorder=event_recorder,
            )
            total_live_successes += t0_delta.live_successes
            today_live_successes += t0_delta.live_successes
            total_simulated_successes += t0_delta.simulated_successes
            total_live_expected_profit += t0_delta.live_profit_total
            total_simulated_expected_profit += t0_delta.simulated_profit_total
            if not config.dry_run:
                last_portfolio_sync_ts, forced_sync_ok = _handle_forced_portfolio_resync(
                    executor=executor,
                    portfolio_sync=portfolio_sync,
                    risk_mgr=risk_mgr,
                    event_recorder=event_recorder,
                    last_portfolio_sync_ts=last_portfolio_sync_ts,
                )
                if not forced_sync_ok:
                    execution_blocked_by_forced_sync = True
                    break
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
        insufficient_balance_skips: dict[str, int] = {}
        insufficient_balance_last_reason: str = ""
        processed_signals = [] if execution_blocked_by_forced_sync else orchestrator.process_signals()
        total_theoretical_opportunities += len(processed_signals)
        today_theoretical_opportunities += len(processed_signals)
        total_directional_signals += len(processed_signals)
        today_directional_signals += len(processed_signals)
        process_skip_summary = orchestrator.get_last_skip_reasons()
        if process_skip_summary.get("total") and event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "tier": "AGGREGATE",
                "signal_type": "orchestrator_skipped",
                "market_id": "",
                "status": "skipped",
                "reason": "orchestrator_process_filter",
                "skip_reasons": process_skip_summary.get("reasons", {}),
                "count_by_tier": process_skip_summary.get("by_tier", {}),
                "total": process_skip_summary.get("total", 0),
            })
        for processed_signal in processed_signals:
            executed, reason, delta = _execute_strategy_signal(
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
                notifier=notifier,
                t2_exit_manager=t2_exit_manager,
                cooldown_store=cooldown_store,
            )
            total_live_successes += delta.live_successes
            today_live_successes += delta.live_successes
            total_simulated_successes += delta.simulated_successes
            total_live_submissions += delta.live_submissions
            total_simulated_submissions += delta.simulated_submissions
            total_live_expected_profit += delta.live_profit_total
            total_simulated_expected_profit += delta.simulated_profit_total
            if executed and not config.dry_run:
                last_portfolio_sync_ts, forced_sync_ok = _handle_forced_portfolio_resync(
                    executor=executor,
                    portfolio_sync=portfolio_sync,
                    risk_mgr=risk_mgr,
                    event_recorder=event_recorder,
                    last_portfolio_sync_ts=last_portfolio_sync_ts,
                )
                if not forced_sync_ok:
                    break
            if executed:
                continue
            orchestrator.record_processed(processed_signal)
            if reason.startswith("insufficient_balance"):
                key = processed_signal.tier.name
                insufficient_balance_skips[key] = insufficient_balance_skips.get(key, 0) + 1
                insufficient_balance_last_reason = reason
                continue
            if event_recorder.is_enabled:
                event_recorder.write_event("strategy_executions", {
                    "tier": processed_signal.tier.name,
                    "signal_type": processed_signal.signal_type,
                    "market_id": processed_signal.market_id,
                    "status": "skipped",
                    "reason": reason,
                    "execution_check": dict(processed_signal.payload.get("execution_check") or {}),
                })
        if insufficient_balance_skips and event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "tier": "AGGREGATE",
                "signal_type": "insufficient_balance_skipped",
                "market_id": "",
                "status": "skipped",
                "reason": insufficient_balance_last_reason,
                "count_by_tier": insufficient_balance_skips,
                "total": sum(insufficient_balance_skips.values()),
            })
        cycle_timing["strategy_execution_sec"] = time.perf_counter() - phase_start

        # T2 exit evaluation: stop-loss / take-profit / time-stop / optimal stopping.
        # Runs every cycle; the manager internally rate-limits per-position via
        # `t2_exit_eval_interval_sec`. Without this loop directional positions
        # would ride to settlement.
        try:
            exit_result = t2_exit_manager.evaluate(active_markets=execution_markets)
            if exit_result.attempted > 0 and event_recorder.is_enabled:
                outcome_kinds = sum(
                    1 for value in (exit_result.triggered, exit_result.partial, exit_result.failed)
                    if value > 0
                )
                if outcome_kinds > 1:
                    exit_status = "mixed"
                elif exit_result.failed:
                    exit_status = "exit_failed"
                elif exit_result.partial:
                    exit_status = "partial_exit"
                else:
                    exit_status = "exited"
                telemetry = t2_exit_telemetry(
                    exit_result,
                    open_count=len(t2_exit_manager.open_positions),
                )
                event_recorder.write_event(
                    "strategy_executions",
                    {
                        "tier": StrategyTier.STATISTICAL_ARB.name,
                        "signal_type": "t2_exit",
                        "market_id": "",
                        "status": exit_status,
                        "trade_count": exit_result.triggered,
                        "attempted_count": exit_result.attempted,
                        "partial_count": exit_result.partial,
                        "failed_count": exit_result.failed,
                        "open_positions": telemetry["open_positions"],
                        "exit_decisions": telemetry["decisions"],
                    },
                )
        except Exception as exc:
            LOG.warning("T2 退出评估异常: %s", exc)

        if not config.dry_run:
            _sync_live_order_statuses(
                executor=executor,
                risk_mgr=risk_mgr,
                maker_strategy=maker_strategy,
                event_recorder=event_recorder,
            )
            _cancel_stale_maker_orders(
                config=config,
                executor=executor,
                risk_mgr=risk_mgr,
                event_recorder=event_recorder,
            )

        if config.dry_run:
            # Allow REST fallback so we can sweep maker quotes in markets
            # that aren't on WS (WS_MAX_MARKETS covers only the hot pool).
            # `count_request=False` keeps the cycle stats clean — the
            # quotes that need REST will be served from the analyzer's
            # cache layer the vast majority of the time, since the same
            # token usually had a snapshot fetched earlier in the cycle
            # by scan/signal collection.
            executor.sweep_simulated_maker_fills(
                lambda token_id: ob_analyzer.get_snapshot(
                    token_id, allow_rest_fallback=True, count_request=False
                ),
                fill_latency_sec=config.shadow_maker_fill_latency_sec,
            )

        if (
            portfolio_sync is not None
            and (time.time() - last_portfolio_sync_ts) >= config.portfolio_sync_interval_sec
        ):
            last_portfolio_sync_ts, _ = _refresh_portfolio_snapshot(
                portfolio_sync=portfolio_sync,
                risk_mgr=risk_mgr,
                event_recorder=event_recorder,
                last_portfolio_sync_ts=last_portfolio_sync_ts,
            )

        risk_s = risk_mgr.state
        vol_snap = vol_estimator.snapshot()
        ws_status = _build_ws_status(
            config=config,
            enhanced_store=enhanced_store,
            ws_target_ids=ws_target_ids,
            phase_hint="scan_complete",
        )
        dash_state.update(**_build_dashboard_cycle_payload(
            config=config,
            cycle=cycle,
            scanned_markets=scanned_markets,
            universe_markets=universe_markets,
            cached_universe_markets=cached_universe_markets,
            event_candidates=event_candidates,
            focus_keywords=focus_keywords,
            last_universe_refresh_ts=last_universe_refresh_ts,
            universe_refreshed=universe_refreshed,
            risk_state=risk_s,
            vol_snapshot=vol_snap,
            edge_decision=edge_decision,
            enhanced_store=enhanced_store,
            ws_status=ws_status,
            orchestrator=orchestrator,
            research_report=research_report,
            research_signals=research_signals,
            research_signal_enabled=research_signal_enabled,
            counters={
                "total_theoretical_opportunities": total_theoretical_opportunities,
                "total_t0_opportunities": total_t0_opportunities,
                "total_directional_signals": total_directional_signals,
                "total_live_successes": total_live_successes,
                "total_simulated_successes": total_simulated_successes,
                "total_live_submissions": total_live_submissions,
                "total_simulated_submissions": total_simulated_submissions,
                "total_live_expected_profit": total_live_expected_profit,
                "total_simulated_expected_profit": total_simulated_expected_profit,
            },
        ))
        dash_state.append_pnl_point({
            "timestamp": time.time(),
            "cumulative_pnl": risk_s.total_pnl,
            "realized_daily_pnl": risk_s.daily_pnl,
            "unrealized_pnl": risk_s.unrealized_pnl,
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
            theoretical_opportunities_total=total_theoretical_opportunities,
            live_successes_total=total_live_successes,
            simulated_successes_total=total_simulated_successes,
            live_submissions_total=total_live_submissions,
            simulated_submissions_total=total_simulated_submissions,
            ws_status=ws_status,
            research_count=len(research_signals),
            daily_pnl=risk_s.daily_pnl,
            open_positions=risk_s.open_positions,
            focus_keywords=focus_keywords,
            unrealized_pnl=risk_s.unrealized_pnl,
            total_pnl=risk_s.total_pnl,
            current_position_value=risk_s.current_position_value,
            cycle_status="ok",
            theoretical_opportunities_today=today_theoretical_opportunities,
            live_successes_today=today_live_successes,
            t0_opportunities_total=total_t0_opportunities,
            t0_opportunities_today=today_t0_opportunities,
            directional_signals_total=total_directional_signals,
            directional_signals_today=today_directional_signals,
        )

        now_ts = time.time()
        if event_recorder.is_enabled and (now_ts - last_telemetry_heartbeat_ts) >= _TELEMETRY_HEARTBEAT_SEC:
            event_recorder.write_event("risk_events", cycle_summary_payload)
            last_telemetry_heartbeat_ts = now_ts

        wallet_usdc = executor.get_available_collateral_balance(use_cache=True)
        notifier.observe_cycle(
            daily_pnl=risk_s.daily_pnl,
            open_positions=risk_s.open_positions,
            total_exposure=risk_s.total_exposure,
            is_halted=risk_s.is_halted,
            halt_reason=risk_s.halt_reason,
            wallet_usdc=wallet_usdc,
            now_ts=now_ts,
        )
        if risk_s.is_halted and risk_s.halt_reason:
            halt_context: list[str] = [
                f"连续失败: {risk_s.consecutive_failures}/{config.max_consecutive_failures}",
            ]
            halt_started = risk_mgr.halt_time
            recover_window = float(config.risk_halt_auto_recover_sec)
            if halt_started is not None and recover_window > 0:
                elapsed = max(0.0, now_ts - halt_started)
                remaining = max(0.0, recover_window - elapsed)
                halt_context.append(
                    f"自动恢复: 剩余 {remaining:.0f}s / {recover_window:.0f}s（无新失败即解除）"
                )
            elif recover_window <= 0:
                halt_context.append("自动恢复: 已禁用 — 需手动 reset_circuit_breaker")
            notifier.notify_fatal_error(
                f"风控已熔断\n原因: {risk_s.halt_reason}",
                error_key=f"risk_halt:{risk_s.halt_reason}",
                context_lines=halt_context,
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
                "状态: 已扫描 %d 周期, T0机会 %d, 定向信号 %d, 真实成交 %d, 模拟成交 %d, 挂单提交(真/模)=%d/%d, WS=%s, 本周期 %.1fs",
                cycle,
                total_t0_opportunities,
                total_directional_signals,
                total_live_successes,
                total_simulated_successes,
                total_live_submissions,
                total_simulated_submissions,
                "连接" if ws_status.get("connected") else "未连接",
                elapsed,
            )

        sleep_time = max(0.0, config.scan_interval_sec - elapsed)
        if sleep_time > 0 and not _SHUTDOWN_EVENT.is_set():
            # P0-dirty: replace the unconditional sleep with a wait
            # that breaks early on (a) shutdown or (b) the WS callback
            # marking ≥ `dirty_market_wake_threshold` markets dirty.
            # The 0.5s cap on each inner wait keeps shutdown latency
            # bounded even if the wake event is never set this window.
            sleep_until = time.time() + sleep_time
            while True:
                remaining = sleep_until - time.time()
                if remaining <= 0:
                    break
                if _SHUTDOWN_EVENT.is_set():
                    break
                if dirty_market_tracker.wake_event.wait(
                    timeout=min(0.5, remaining)
                ):
                    dirty_market_tracker.wake_event.clear()
                    break

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
            # `arbs_found_total` historically meant "T0 + directional signals"
            # which was misleading. Keep the legacy field for downstream
            # compatibility but add explicit splits so operators don't have
            # to grep signal NDJSON to know if T0 actually found anything.
            "arbs_found_total": total_theoretical_opportunities,
            "t0_opportunities_total": total_t0_opportunities,
            "directional_signals_total": total_directional_signals,
            "arbs_executed_total": total_live_successes,
            "simulated_successes_total": total_simulated_successes,
            "live_submissions_total": total_live_submissions,
            "simulated_submissions_total": total_simulated_submissions,
        })
    tick_recorder.close()
    event_recorder.close()
    if flow_aggregator is not None:
        flow_aggregator.close()
    dash_state.update(is_running=False)
    LOG.info(
        "机器人已停止: run_id=%s。总计: %d 周期, T0机会=%d, 定向信号=%d, 真实成交=%d, 模拟成交=%d, 挂单提交(真/模)=%d/%d",
        run_id,
        cycle,
        total_t0_opportunities,
        total_directional_signals,
        total_live_successes,
        total_simulated_successes,
        total_live_submissions,
        total_simulated_submissions,
    )
    notifier.notify_shutdown(
        run_id=run_id,
        cycle_count=cycle,
        total_t0_opportunities=total_t0_opportunities,
        total_directional_signals=total_directional_signals,
        total_arbs_executed=total_live_successes,
        simulated_successes=total_simulated_successes,
    )


# Telemetry / dashboard serialisation / pending-signal lookups / AI
# cycle were extracted to `polymarket_arb.main_helpers.{dashboard_serializers,
# cycle_runners, ai_cycle}` and re-imported above under their underscore
# aliases so the call graph here stays unchanged.
