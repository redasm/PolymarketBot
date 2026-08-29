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
from collections import deque
from dataclasses import replace
import json
import logging
import os
import signal
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

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
from polymarket_arb.main_helpers.cycle_runners import (
    find_pending_signal as _find_pending_signal,
    find_pending_signal_overlay as _find_pending_signal_overlay,
    refresh_market_universe as _refresh_market_universe,
    refresh_ws_subscription as _refresh_ws_subscription,
    scan_cycle as _scan_cycle,
)
from polymarket_arb.main_helpers.flow_aggregator import FlowAggregator, FlowIngest
from polymarket_arb.main_helpers.order_sync import (
    cancel_stale_maker_orders as _cancel_stale_maker_orders,
    sync_live_order_statuses as _sync_live_order_statuses,
    sync_user_channel_fills as _sync_user_channel_fills,
)
from polymarket_arb.main_helpers.maker_fill_notifications import (
    handle_observed_maker_fills as _handle_observed_maker_fills,
)
from polymarket_arb.main_helpers.strategy_telemetry import (
    build_run_config_event as _build_run_config_event,
    ensure_signal_id as _ensure_signal_id,
    normalize_skip_reason_counts as _normalize_skip_reason_counts,
    structured_skip_reason as _structured_skip_reason,
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
    load_last_backtest_report as _load_last_backtest_report,
    log_startup_summary as _log_startup_summary,
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
    collect_event_calendar_strategy_signals as _collect_event_calendar_strategy_signals,
    collect_logical_constraint_strategy_signals as _collect_logical_constraint_strategy_signals,
    collect_maker_strategy_signals as _collect_maker_strategy_signals,
    collect_statistical_strategy_signals as _collect_statistical_strategy_signals,
    collect_wallet_alpha_strategy_signals as _collect_wallet_alpha_strategy_signals,
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
from polymarket_arb.main_helpers.maker_scoring_audit import (
    MakerScoringAuditState as _MakerScoringAuditState,
    audit_maker_order_scoring as _audit_maker_order_scoring,
)
from polymarket_arb.main_helpers.signal_telemetry import StrategySignalTelemetryCompressor
from polymarket_arb.main_helpers.virtual_fill_emitter import (
    VirtualFillEmitter as _VirtualFillEmitter,
)
from polymarket_arb.main_helpers.shadow_position_lifecycle import (
    ShadowPositionLifecycle as _ShadowPositionLifecycle,
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
    estimate_trade_outcome as _estimate_trade_outcome,
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
from polymarket_arb.rewards_client import RewardsClient
from polymarket_arb.user_feed import UserChannelFeed
from polymarket_arb.models import (
    MarketInfo,
    ResearchSignalReport,
    TradeRecord,
)
from polymarket_arb.notifier import NotificationManager
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.portfolio_sync import PortfolioSync
from polymarket_arb.quant_input_store import QuantInputStore
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.maker_strategy import DynamicSpreadCalculator, MakerStrategy
from polymarket_arb.strategies.recent_exit_cooldown import make_recent_exit_cooldown_store
from polymarket_arb.strategies.t2_exit_manager import T2ExitManager, t2_exit_telemetry
from polymarket_arb.strategies.t3_maker_exit_manager import T3MakerExitManager
from polymarket_arb.main_helpers.t2_model_prob import build_t2_model_prob_provider
from polymarket_arb.strategies.signal_policies import (
    BarbellPolicy,
    NearCertaintyClassifier,
)
from polymarket_arb.strategies.statistical_model import StatisticalMispricingDetector
from polymarket_arb.strategies.sniper_gate import SniperGate, SniperGateConfig
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)
from polymarket_arb.tick_recorder import TickRecorder
from polymarket_arb.volatility_estimator import VolEstimator
from polymarket_arb.websocket_feed import OrderBookMirror, WebSocketFeed
from polymarket_arb.spot_feed import BinanceSpotFeed, parse_spot_pairs
from polymarket_arb.strategies.updown_pricer import UpdownPricer
from polymarket_arb.strategies.weather_strategy import (
    OpenMeteoEnsembleProvider,
    collect_weather_strategy_signals as _collect_weather_strategy_signals,
)

if TYPE_CHECKING:
    from research_signal.service import ResearchSignalService

LOG = logging.getLogger("main_loop")

_SHUTDOWN_EVENT = threading.Event()
_TELEMETRY_HEARTBEAT_SEC = 60.0
_LIFETIME_DEDUPE_MAX_KEYS = 100_000


def _signal_handler(sig: int, frame: Any) -> None:
    LOG.info("收到信号 %d，准备优雅退出…", sig)
    _SHUTDOWN_EVENT.set()


class _BoundedDedupeSet:
    """Small FIFO-capped membership set for run-level opportunity counters."""

    def __init__(self, max_keys: int) -> None:
        self._max_keys = max(1, int(max_keys))
        self._keys: set[tuple[Any, ...]] = set()
        self._order: deque[tuple[Any, ...]] = deque()

    def add_new(self, key: tuple[Any, ...]) -> bool:
        if key in self._keys:
            return False
        self._keys.add(key)
        self._order.append(key)
        while len(self._keys) > self._max_keys:
            oldest = self._order.popleft()
            self._keys.discard(oldest)
        return True


def _refresh_portfolio_snapshot(
    *,
    portfolio_sync: PortfolioSync | None,
    risk_mgr: RiskManager,
    event_recorder: EventRecorder,
    last_portfolio_sync_ts: float,
    forced: bool = False,
    t2_exit_manager: "T2ExitManager | None" = None,
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
        readopted = 0
        if t2_exit_manager is not None:
            try:
                readopted = t2_exit_manager.reconcile_with_chain(snapshot.positions)
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("T2 放弃仓位对账失败: %s", exc)
        if event_recorder.is_enabled:
            event_recorder.write_event("risk_events", {
                "event": "portfolio_sync",
                "forced": forced,
                "source_address": snapshot.source_address,
                "positions": len(snapshot.positions),
                "realized_daily_pnl": snapshot.realized_daily_pnl,
                "synced_at": snapshot.synced_at,
                "t2_readopted": readopted,
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
    t2_exit_manager: "T2ExitManager | None" = None,
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
        t2_exit_manager=t2_exit_manager,
    )


def _opportunity_dedupe_key(opp: Any) -> tuple[Any, ...]:
    markets = tuple(sorted(str(getattr(m, "condition_id", "") or "") for m in getattr(opp, "markets", []) or []))
    legs = tuple(
        sorted(
            (
                str(getattr(leg, "condition_id", "") or ""),
                str(getattr(leg, "outcome", "") or ""),
                str(getattr(getattr(leg, "side", ""), "value", getattr(leg, "side", "")) or ""),
            )
            for leg in getattr(opp, "legs", []) or []
        )
    )
    return (
        str(getattr(opp, "arb_type", "") and getattr(opp.arb_type, "value", opp.arb_type)),
        str(getattr(opp, "event_id", "") or ""),
        markets,
        legs,
    )


def _signal_dedupe_key(signal: StrategySignal) -> tuple[Any, ...]:
    payload = signal.payload if isinstance(signal.payload, dict) else {}
    return (
        signal.tier.name,
        signal.signal_type,
        signal.market_id,
        str(payload.get("category", "") or ""),
        str(payload.get("direction", "") or ""),
    )


# Startup / parsing / boot helpers (build_run_instance_id, log_startup_summary,
# parse_http_json_sources, create_research_signal_service,
# create_cross_platform_scanner, round_timing,
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


def _resolve_user_ws_credentials(config, trading_client) -> tuple[str, str, str]:
    """user 频道订阅需要 L2 凭证 (apiKey / secret / passphrase).

    优先用配置里显式给的；没配就从交易客户端上取 —— `build_trading_client`
    在未配置时会 create_or_derive 一套并挂在 client.creds 上，那套才是
    实际在用的凭证。取不到就返回空串，调用方据此退回纯轮询。
    """
    api_key = (config.clob_api_key or "").strip()
    api_secret = (config.clob_api_secret or "").strip()
    api_passphrase = (config.clob_api_passphrase or "").strip()
    if api_key and api_secret and api_passphrase:
        return api_key, api_secret, api_passphrase

    creds = getattr(trading_client, "creds", None)
    if creds is None:
        return api_key, api_secret, api_passphrase
    return (
        api_key or str(getattr(creds, "api_key", "") or ""),
        api_secret or str(getattr(creds, "api_secret", "") or ""),
        api_passphrase or str(getattr(creds, "api_passphrase", "") or ""),
    )


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
    shadow_validation_enabled = bool(
        not config.dry_run
        and getattr(config, "wallet_alpha_shadow_validation_enabled", True)
    )
    shadow_config = replace(
        config,
        dry_run=True,
        wallet_alpha_candidate_shadow_enabled=True,
    ) if shadow_validation_enabled else config
    shadow_executor = ExecutionEngine(shadow_config, ro_client) if shadow_validation_enabled else None
    shadow_risk_mgr = RiskManager(shadow_config) if shadow_validation_enabled else None

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
    # T2 UPDOWN Phase 2: Binance 现货 feed + spot-anchored pricer。
    # 仅 t2_updown_enabled 且 spot_feed_enabled 时启动;否则 pricer=None,
    # UPDOWN 分支不触发 (退化为 Phase 1 行为)。feed 是 daemon 线程,绝不阻塞主循环。
    spot_feed: Optional[BinanceSpotFeed] = None
    updown_pricer: Optional[UpdownPricer] = None
    if getattr(config, "t2_updown_enabled", False) and getattr(config, "t2_updown_spot_feed_enabled", False):
        try:
            _ud_symbols = [s.strip() for s in (config.t2_updown_symbols or "").split(",") if s.strip()]
            _ud_windows = [int(w.strip()) * 60 for w in (config.t2_updown_window_minutes or "15").split(",") if w.strip()]
            _ud_pairs = parse_spot_pairs(_ud_symbols, config.t2_updown_spot_pairs)
            spot_feed = BinanceSpotFeed(
                pairs=_ud_pairs,
                window_secs=_ud_windows or [900],
                fast_minutes=config.vol_fast_minutes,
                slow_minutes=config.vol_slow_minutes,
                min_bars=config.vol_min_bars,
            )
            spot_feed.start()
            updown_pricer = UpdownPricer(spot_feed)
            LOG.info("T2 UPDOWN Phase 2 已启用: 现货源 %s", ",".join(sorted(_ud_pairs.values())))
        except Exception as e:  # noqa: BLE001 - feed 启动失败不能拖垮主循环
            LOG.warning("T2 UPDOWN 现货 feed 启动失败,回退 Phase 1: %s", e)
            spot_feed = None
            updown_pricer = None
    weather_provider: Optional[OpenMeteoEnsembleProvider] = None
    if getattr(config, "weather_strategy_enabled", False):
        weather_provider = OpenMeteoEnsembleProvider(
            ttl_sec=config.weather_forecast_ttl_sec,
            timeout_sec=config.weather_request_timeout_sec,
        )
        LOG.info(
            "天气 T2 已启用（Open-Meteo GFS ensemble，min_edge=%.1f%%，min_confidence=%.2f）",
            config.weather_min_edge * 100.0,
            config.weather_min_confidence,
        )
    maker_strategy = MakerStrategy(
        spread_calc=DynamicSpreadCalculator(vol_estimator=vol_estimator),
        default_size=config.default_order_size_usdc,
        max_inventory=max(config.max_exposure_per_market, config.default_order_size_usdc),
        flow_inventory_weight=config.t3_flow_bias_inventory_weight,
    )
    # T3 流动性奖励带。缓存 + 后台预热，扫描热路径只读缓存，拿不到
    # 就退回 δ=0（历史行为）。maker 关闭时不建客户端。
    rewards_client = RewardsClient(
        config.clob_host,
        timeout_sec=config.maker_rewards_timeout_sec,
        ttl_sec=config.maker_rewards_ttl_sec,
        negative_ttl_sec=config.maker_rewards_negative_ttl_sec,
        enabled=config.maker_rewards_enabled and config.maker_strategy_enabled,
    )
    # 挂在带内 != 真计分。这个 state 跨周期记住每个挂单连续未计分多久。
    maker_scoring_state = _MakerScoringAuditState()
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
    if config.dry_run or shadow_validation_enabled:
        shadow_lifecycle = _ShadowPositionLifecycle(
            event_recorder=event_recorder,
            book_snapshot_provider=lambda token_id: ob_analyzer.get_snapshot(
                token_id, allow_rest_fallback=False, count_request=False
            ),
        )
        virtual_fill_emitter = _VirtualFillEmitter(
            event_recorder=event_recorder,
            book_snapshot_provider=lambda token_id: ob_analyzer.get_snapshot(
                token_id, allow_rest_fallback=False, count_request=False
            ),
            taker_fee_rate=config.polymarket_taker_fee_rate,
            lifecycle=shadow_lifecycle,
        )
        if config.dry_run:
            executor.set_virtual_fill_emitter(virtual_fill_emitter)
        elif shadow_executor is not None:
            shadow_executor.set_virtual_fill_emitter(virtual_fill_emitter)
    else:
        shadow_lifecycle = None
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
        backtest_data_dir=config.backtest_data_dir,
        backtest_retention_days=config.data_backtest_retention_days,
        backtest_max_gb=config.data_backtest_max_gb,
    )
    if config.tick_record_enabled:
        LOG.info("Tick 录制已开启: %s", config.tick_record_dir)
    focus_keywords = _focus_keywords(config.market_focus_keywords)
    if config.telemetry_record_enabled:
        LOG.info("Telemetry 录制已开启: %s", config.telemetry_record_dir)
        event_recorder.write_event("risk_events", _build_run_config_event(config, run_id=run_id))
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
            "maker_enabled": config.maker_strategy_enabled,
        })
    if config.data_cleanup_enabled:
        LOG.info("Data 定期清理已开启: interval=%.0fs", config.data_cleanup_interval_sec)
    quant_input_store = QuantInputStore.from_config(config)

    sniper_gate = (
        SniperGate(
            SniperGateConfig(
                min_net_edge_bps=config.sniper_min_net_edge_bps,
                min_confidence=config.sniper_min_confidence,
                min_liquidity=config.sniper_min_liquidity,
                min_volume_24h=config.sniper_min_volume_24h,
                max_correlation_score=config.sniper_max_correlation_score,
            )
        )
        if config.sniper_gate_enabled
        else None
    )
    near_certainty_classifier = NearCertaintyClassifier(
        high_threshold=config.t2_near_certainty_high_threshold,
        low_threshold=config.t2_near_certainty_low_threshold,
        size_multiplier=config.t2_near_certainty_size_multiplier,
        confidence_delta=config.t2_near_certainty_confidence_delta,
        shadow_mode=config.t2_near_certainty_shadow_mode,
    )
    # Barbell pool budget: tail bucket cap is a slice of the
    # STATISTICAL_ARB allocation. Default 15% of T2's bankroll allocation.
    t2_allocation_pct = StrategyOrchestrator.DEFAULT_ALLOCATIONS.get(
        StrategyTier.STATISTICAL_ARB, 0.35
    )
    barbell_tail_budget = (
        float(config.max_total_exposure)
        * t2_allocation_pct
        * float(config.t2_barbell_tail_budget_pct)
    )
    barbell_policy = BarbellPolicy(
        enabled=config.t2_barbell_enabled,
        tail_budget_usdc=barbell_tail_budget,
        tail_relaxed_multiplier=config.t2_barbell_tail_relaxed_multiplier,
    )
    orchestrator = StrategyOrchestrator(
        total_bankroll=config.max_total_exposure,
        max_signals_per_market_per_hour=config.t2_max_signals_per_market_per_hour,
        near_certainty_classifier=near_certainty_classifier,
        barbell_policy=barbell_policy,
        sniper_gate=sniper_gate,
    )
    shadow_orchestrator = (
        StrategyOrchestrator(
            total_bankroll=shadow_config.max_total_exposure,
            max_signals_per_market_per_hour=shadow_config.t2_max_signals_per_market_per_hour,
            near_certainty_classifier=NearCertaintyClassifier(
                high_threshold=shadow_config.t2_near_certainty_high_threshold,
                low_threshold=shadow_config.t2_near_certainty_low_threshold,
                size_multiplier=shadow_config.t2_near_certainty_size_multiplier,
                confidence_delta=shadow_config.t2_near_certainty_confidence_delta,
                shadow_mode=shadow_config.t2_near_certainty_shadow_mode,
            ),
            barbell_policy=BarbellPolicy(
                enabled=shadow_config.t2_barbell_enabled,
                tail_budget_usdc=(
                    float(shadow_config.max_total_exposure)
                    * t2_allocation_pct
                    * float(shadow_config.t2_barbell_tail_budget_pct)
                ),
                tail_relaxed_multiplier=shadow_config.t2_barbell_tail_relaxed_multiplier,
            ),
        )
        if shadow_validation_enabled
        else None
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
    t2_model_prob_provider = build_t2_model_prob_provider(
        statistical_detector=statistical_detector,
        ob_analyzer=ob_analyzer,
        event_baselines_provider=lambda: quant_input_store.snapshot().event_baselines_json,
    )
    t2_exit_manager = T2ExitManager(
        config=config,
        executor=executor,
        ob_analyzer=ob_analyzer,
        risk_manager=risk_mgr,
        notifier=notifier,
        cooldown_store=cooldown_store,
        orchestrator=orchestrator,
        model_prob_provider=t2_model_prob_provider,
    )
    shadow_t2_exit_manager = (
        T2ExitManager(
            config=shadow_config,
            executor=shadow_executor,
            ob_analyzer=ob_analyzer,
            risk_manager=shadow_risk_mgr,
            orchestrator=shadow_orchestrator,
            model_prob_provider=t2_model_prob_provider,
        )
        if shadow_validation_enabled and shadow_executor is not None and shadow_risk_mgr is not None
        else None
    )
    LOG.info(
        "T2 退出策略: stop_loss=%.0fbps tp_capture=%.0f%% max_hold=%.0fs eval=%.0fs optimal_stopping=%s",
        config.t2_stop_loss_bps,
        config.t2_take_profit_capture_pct * 100,
        config.t2_max_hold_sec,
        config.t2_exit_eval_interval_sec,
        config.t2_optimal_stopping_enabled,
    )
    t3_maker_exit_manager = T3MakerExitManager(
        config=config,
        executor=executor,
        ob_analyzer=ob_analyzer,
        risk_manager=risk_mgr,
        notifier=notifier,
        orchestrator=orchestrator,
    )
    LOG.info(
        "T3 maker 退出策略: stop_loss=%.0fbps take_profit=%.0fbps max_hold=%.0fs eval=%.0fs",
        config.maker_stop_loss_bps,
        config.maker_take_profit_bps,
        config.maker_max_hold_sec,
        config.maker_exit_eval_interval_sec,
    )
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
    # user 频道：成交推送。后台线程只入队，主循环每周期排干后在主线程
    # 落地（见 _sync_user_channel_fills）。dry-run 没有真实挂单，不启。
    user_ws_markets: set[str] = set()
    user_feed: Optional[UserChannelFeed] = None
    if config.user_ws_enabled and not config.dry_run and trading_client is not None:
        api_key, api_secret, api_passphrase = _resolve_user_ws_credentials(
            config, trading_client
        )
        user_feed = UserChannelFeed(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            market_provider=lambda: sorted(user_ws_markets),
            wake_event=dirty_market_tracker.wake_event,
            queue_size=config.user_ws_queue_size,
        )
        if not user_feed.start():
            # 没有凭证就退回纯轮询，不影响下单，只是成交观测慢一个周期。
            user_feed = None
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

    dashboard_enabled = bool(config.dashboard_enabled)
    dash_state = DashboardState(enabled=dashboard_enabled)
    if dashboard_enabled:
        dash_state.update(
            is_dry_run=config.dry_run,
            scan_interval=config.scan_interval_sec,
        )
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
    seen_theoretical_keys = _BoundedDedupeSet(_LIFETIME_DEDUPE_MAX_KEYS)
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
    seen_t0_keys = _BoundedDedupeSet(_LIFETIME_DEDUPE_MAX_KEYS)
    seen_directional_keys = _BoundedDedupeSet(_LIFETIME_DEDUPE_MAX_KEYS)
    consecutive_api_errors = 0
    # Daily counters that roll over at UTC midnight. Lifetime totals above
    # are useful for run-level summaries but operators reading hourly
    # telemetry want to know how many opportunities materialised *today*.
    today_theoretical_opportunities = 0
    today_live_successes = 0
    today_t0_opportunities = 0
    today_directional_signals = 0
    today_seen_theoretical_keys: set[tuple[Any, ...]] = set()
    today_seen_t0_keys: set[tuple[Any, ...]] = set()
    today_seen_directional_keys: set[tuple[Any, ...]] = set()
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
            today_seen_theoretical_keys.clear()
            today_seen_t0_keys.clear()
            today_seen_directional_keys.clear()
            # NOTE: shadow_lifecycle 的日内已实现盈亏由其自身 UTC 日切时钟
            # (_maybe_roll_daily, 挂在每 cycle 必经的 snapshot()) 归零,
            # 这里无需也不应调用 reset()——那会清掉累计 realized 与开仓敞口。
        cycle_timing: dict[str, float] = {
            "universe_refresh_sec": 0.0,
            "candidate_select_sec": 0.0,
            "ws_refresh_sec": 0.0,
            "prewarm_sec": 0.0,
            "scan_cycle_sec": 0.0,
            "research_sec": 0.0,
            "strategy_sec": 0.0,
            "execution_sec": 0.0,
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

        if dashboard_enabled:
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
            updown_boost = (
                config.t2_updown_priority_boost if config.t2_updown_enabled else 0.0
            )
            weather_boost = 2.0 if getattr(config, "weather_strategy_enabled", False) else 0.0
            if universe_refreshed or not cached_scanned_markets:
                scanned_markets = _select_scan_candidates(
                    cached_universe_markets,
                    config.hot_market_pool_size,
                    focus_keywords=focus_keywords,
                    updown_boost=updown_boost,
                    weather_boost=weather_boost,
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
                    updown_boost=updown_boost,
                    weather_boost=weather_boost,
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
                    targets = _select_ws_targets(
                        scanned_markets,
                        config.ws_max_markets,
                        updown_boost=updown_boost,
                        weather_boost=weather_boost,
                    )
                    if targets:
                        new_ids = sorted(t.token_id for m in targets for t in m.tokens)
                        if new_ids != ws_target_ids:
                            ws_feed, ws_mirror = _refresh_ws_subscription(
                                feed=ws_feed,
                                mirror=ws_mirror,
                                targets=targets,
                                enhanced_store=enhanced_store,
                                tick_recorder=tick_recorder,
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
                progress_cb=(
                    (lambda **kwargs: dash_state.update(
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
                    ))
                    if dashboard_enabled
                    else None
                ),
            )
            cycle_timing["scan_cycle_sec"] += time.perf_counter() - phase_start
            consecutive_api_errors = 0
            if dashboard_enabled:
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
            if dashboard_enabled:
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
            wallet_usdc = executor.get_available_collateral_balance(use_cache=True)
            LOG.info(
                "钱包余额观测: %s",
                f"{wallet_usdc:.6f} USDC" if wallet_usdc is not None else "N/A",
            )
            notifier.observe_cycle(
                daily_pnl=risk_mgr.state.daily_pnl,
                open_positions=risk_mgr.state.open_positions,
                total_exposure=risk_mgr.state.total_exposure,
                is_halted=risk_mgr.state.is_halted,
                halt_reason=risk_mgr.state.halt_reason,
                wallet_usdc=wallet_usdc,
            )
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
            for opp in opportunities:
                opp_key = ("t0",) + _opportunity_dedupe_key(opp)
                if seen_t0_keys.add_new(opp_key):
                    total_t0_opportunities += 1
                if opp_key not in today_seen_t0_keys:
                    today_seen_t0_keys.add(opp_key)
                    today_t0_opportunities += 1
                if seen_theoretical_keys.add_new(opp_key):
                    total_theoretical_opportunities += 1
                if opp_key not in today_seen_theoretical_keys:
                    today_seen_theoretical_keys.add(opp_key)
                    today_theoretical_opportunities += 1
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
        quant_inputs = quant_input_store.snapshot()
        statistical_signals = _collect_statistical_strategy_signals(
            config=config,
            candidate_markets=scanned_markets,
            ob_analyzer=ob_analyzer,
            detector=statistical_detector,
            event_baselines=quant_inputs.event_baselines_json,
            updown_pricer=updown_pricer,
        )
        strategy_signals.extend(statistical_signals)
        if weather_provider is not None:
            strategy_signals.extend(
                _collect_weather_strategy_signals(
                    config=config,
                    candidate_markets=scanned_markets,
                    ob_analyzer=ob_analyzer,
                    provider=weather_provider,
                )
            )
        strategy_signals.extend(
            _collect_logical_constraint_strategy_signals(
                config=config,
                candidate_markets=scanned_markets,
                ob_analyzer=ob_analyzer,
                rules=quant_inputs.logical_constraints_json,
                input_metadata=quant_inputs.input_metadata("logical_constraints"),
            )
        )
        strategy_signals.extend(
            _collect_event_calendar_strategy_signals(
                config=config,
                candidate_markets=scanned_markets,
                ob_analyzer=ob_analyzer,
                baselines=quant_inputs.event_baselines_json,
                input_metadata=quant_inputs.input_metadata("event_baselines"),
            )
        )
        strategy_signals.extend(
            _collect_wallet_alpha_strategy_signals(
                config=config,
                candidate_markets=scanned_markets,
                profiles=quant_inputs.wallet_alpha_profiles_json,
                observations=quant_inputs.wallet_alpha_observations_json,
                input_metadata={
                    "profiles": quant_inputs.input_metadata("wallet_alpha_profiles"),
                    "observations": quant_inputs.input_metadata("wallet_alpha_observations"),
                },
            )
        )
        shadow_candidate_signals = []
        if shadow_validation_enabled:
            shadow_candidate_signals = [
                signal for signal in _collect_wallet_alpha_strategy_signals(
                    config=shadow_config,
                    candidate_markets=scanned_markets,
                    profiles=quant_inputs.wallet_alpha_profiles_json,
                    observations=quant_inputs.wallet_alpha_observations_json,
                    input_metadata={
                        "profiles": quant_inputs.input_metadata("wallet_alpha_profiles"),
                        "observations": quant_inputs.input_metadata("wallet_alpha_observations"),
                    },
                )
                if signal.signal_type.startswith("wallet_alpha_candidate_")
            ]
            max_shadow_signals = int(getattr(config, "wallet_alpha_shadow_max_signals_per_cycle", 5) or 0)
            if max_shadow_signals > 0:
                shadow_candidate_signals = shadow_candidate_signals[:max_shadow_signals]
            else:
                shadow_candidate_signals = []
        fair_values_by_market = {
            signal.market_id: float(signal.payload.get("model_prob"))
            for signal in statistical_signals
            if signal.payload.get("model_prob") is not None
        }
        if rewards_client.enabled:
            # 只排队，不阻塞：本周期用得上的是已缓存的那些，新市场的
            # 奖励参数下个周期才生效（δ=0 期间等价于旧行为）。
            rewards_client.request(
                [market.condition_id for market in scanned_markets],
                max_pending=max(1, config.maker_rewards_prefetch_per_cycle * 10),
            )
        maker_signals = (
            _collect_maker_strategy_signals(
                candidate_markets=scanned_markets,
                ob_analyzer=ob_analyzer,
                maker_strategy=maker_strategy,
                fair_values_by_market=fair_values_by_market,
                detector=statistical_detector,
                flow_aggregator=flow_aggregator,
                event_baselines=quant_inputs.event_baselines_json,
                reward_config_provider=rewards_client.cached,
                rewards_only=config.maker_rewards_only,
            )
            if config.maker_strategy_enabled
            else []
        )
        strategy_signals.extend(maker_signals)

        active_markets_for_overlay = universe_markets if universe_markets else scanned_markets
        for strategy_signal in strategy_signals:
            _ensure_signal_id(strategy_signal)
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

        if shadow_orchestrator is not None:
            for candidate_signal in shadow_candidate_signals:
                _ensure_signal_id(candidate_signal)
                submitted = shadow_orchestrator.submit_signal(
                    candidate_signal,
                    active_markets=active_markets_for_overlay,
                    research_report=research_report,
                    research_signals=research_signals,
                )
                if event_recorder.is_enabled:
                    signal_payload = _serialize_strategy_signal(
                        _find_pending_signal(shadow_orchestrator, candidate_signal) or candidate_signal,
                        submitted=submitted,
                        research_overlay=_find_pending_signal_overlay(shadow_orchestrator, candidate_signal),
                    )
                    for compressed_payload in signal_telemetry.consume(signal_payload):
                        event_recorder.write_event("strategy_signals", compressed_payload)

        cycle_timing["strategy_sec"] += time.perf_counter() - phase_start
        t2_collector_skips = dict(getattr(_collect_statistical_strategy_signals, "last_skip_summary", {}) or {})
        t3_collector_skips = dict(getattr(_collect_maker_strategy_signals, "last_skip_summary", {}) or {})
        skip_reason_counts = _normalize_skip_reason_counts(
            t2_collector_skips,
            t3_collector_skips,
        )
        if event_recorder.is_enabled and (
            int(t2_collector_skips.get("total", 0) or 0) > 0
            or int(t3_collector_skips.get("total", 0) or 0) > 0
        ):
            event_recorder.write_event("strategy_executions", {
                "execution_id": "",
                "signal_id": "",
                "tier": "AGGREGATE",
                "signal_type": "collector_skipped",
                "market_id": "",
                "status": "skipped",
                "reason": "collector_filter",
                "skip_reasons": {
                    "T2_STATISTICAL": t2_collector_skips,
                    "T3_MARKET_MAKING": t3_collector_skips,
                },
                "skip_reason_counts": skip_reason_counts,
                "total": int(t2_collector_skips.get("total", 0) or 0)
                + int(t3_collector_skips.get("total", 0) or 0),
            })

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
                    t2_exit_manager=t2_exit_manager,
                )
                if not forced_sync_ok:
                    execution_blocked_by_forced_sync = True
                    break
        cycle_timing["execution_sec"] += time.perf_counter() - phase_start

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
        process_skip_summary = orchestrator.get_last_skip_reasons()
        skip_reason_counts = _normalize_skip_reason_counts(process_skip_summary, skip_reason_counts)
        if process_skip_summary.get("total") and event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "execution_id": "",
                "signal_id": "",
                "tier": "AGGREGATE",
                "signal_type": "orchestrator_skipped",
                "market_id": "",
                "status": "skipped",
                "reason": "orchestrator_process_filter",
                "skip_reasons": process_skip_summary.get("reasons", {}),
                "count_by_tier": process_skip_summary.get("by_tier", {}),
                "skip_reason_counts": _normalize_skip_reason_counts(process_skip_summary),
                "total": process_skip_summary.get("total", 0),
            })
        for processed_signal in processed_signals:
            sig_key = ("signal",) + _signal_dedupe_key(processed_signal)
            if seen_directional_keys.add_new(sig_key):
                total_directional_signals += 1
            if sig_key not in today_seen_directional_keys:
                today_seen_directional_keys.add(sig_key)
                today_directional_signals += 1
            if seen_theoretical_keys.add_new(sig_key):
                total_theoretical_opportunities += 1
            if sig_key not in today_seen_theoretical_keys:
                today_seen_theoretical_keys.add(sig_key)
                today_theoretical_opportunities += 1
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
                    t2_exit_manager=t2_exit_manager,
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
                    "execution_id": "",
                    "signal_id": getattr(processed_signal, "signal_id", ""),
                    "tier": processed_signal.tier.name,
                    "signal_type": processed_signal.signal_type,
                    "market_id": processed_signal.market_id,
                    "status": "skipped",
                    "reason": reason,
                    **_structured_skip_reason(reason),
                    "execution_check": dict(processed_signal.payload.get("execution_check") or {}),
                })
        if insufficient_balance_skips and event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "execution_id": "",
                "signal_id": "",
                "tier": "AGGREGATE",
                "signal_type": "insufficient_balance_skipped",
                "market_id": "",
                "status": "skipped",
                "reason": insufficient_balance_last_reason,
                "count_by_tier": insufficient_balance_skips,
                "skip_reason_counts": {
                    "insufficient_balance": sum(insufficient_balance_skips.values()),
                },
                "total": sum(insufficient_balance_skips.values()),
            })
        if (
            shadow_orchestrator is not None
            and shadow_executor is not None
            and shadow_risk_mgr is not None
        ):
            shadow_exec_start = time.perf_counter()
            shadow_exec_budget_sec = (
                float(getattr(config, "wallet_alpha_shadow_max_exec_ms_per_cycle", 250.0) or 0.0)
                / 1000.0
            )
            for shadow_signal in shadow_orchestrator.process_signals():
                if shadow_exec_budget_sec > 0 and (time.perf_counter() - shadow_exec_start) >= shadow_exec_budget_sec:
                    shadow_orchestrator.record_processed(shadow_signal)
                    if event_recorder.is_enabled:
                        event_recorder.write_event("strategy_executions", {
                            "execution_id": "",
                            "signal_id": getattr(shadow_signal, "signal_id", ""),
                            "tier": shadow_signal.tier.name,
                            "signal_type": shadow_signal.signal_type,
                            "market_id": shadow_signal.market_id,
                            "status": "skipped",
                            "mode": "shadow_validation",
                            "reason": "shadow_validation_budget_exhausted",
                        })
                    continue
                executed, reason, delta = _execute_strategy_signal(
                    signal=shadow_signal,
                    config=shadow_config,
                    active_markets=execution_markets,
                    ob_analyzer=ob_analyzer,
                    executor=shadow_executor,
                    risk_mgr=shadow_risk_mgr,
                    orchestrator=shadow_orchestrator,
                    dash_state=dash_state,
                    event_recorder=event_recorder,
                    maker_strategy=maker_strategy,
                    notifier=notifier,
                    t2_exit_manager=shadow_t2_exit_manager,
                    cooldown_store=None,
                )
                total_simulated_successes += delta.simulated_successes
                total_simulated_submissions += delta.simulated_submissions
                total_simulated_expected_profit += delta.simulated_profit_total
                if executed:
                    continue
                shadow_orchestrator.record_processed(shadow_signal)
                if event_recorder.is_enabled:
                    event_recorder.write_event("strategy_executions", {
                        "execution_id": "",
                        "signal_id": getattr(shadow_signal, "signal_id", ""),
                        "tier": shadow_signal.tier.name,
                        "signal_type": shadow_signal.signal_type,
                        "market_id": shadow_signal.market_id,
                        "status": "skipped",
                        "mode": "shadow_validation",
                        "reason": reason,
                        **_structured_skip_reason(reason),
                        "execution_check": dict(shadow_signal.payload.get("execution_check") or {}),
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

        # T3 maker exit evaluation: TTL / stop-loss / take-profit.
        # Pre-fix, maker fills (including maker_crossed taker fills)
        # had no exit path and rode the position to settlement.
        try:
            t3_exit_result = t3_maker_exit_manager.evaluate(active_markets=execution_markets)
            if t3_exit_result.attempted > 0 and event_recorder.is_enabled:
                if t3_exit_result.failed and not t3_exit_result.triggered and not t3_exit_result.partial:
                    t3_exit_status = "exit_failed"
                elif t3_exit_result.partial and not t3_exit_result.triggered:
                    t3_exit_status = "partial_exit"
                elif t3_exit_result.triggered and (t3_exit_result.failed or t3_exit_result.partial):
                    t3_exit_status = "mixed"
                else:
                    t3_exit_status = "exited"
                event_recorder.write_event(
                    "strategy_executions",
                    {
                        "tier": StrategyTier.MARKET_MAKING.name,
                        "signal_type": "t3_maker_exit",
                        "market_id": "",
                        "status": t3_exit_status,
                        "trade_count": t3_exit_result.triggered,
                        "attempted_count": t3_exit_result.attempted,
                        "partial_count": t3_exit_result.partial,
                        "failed_count": t3_exit_result.failed,
                        "open_positions": len(t3_maker_exit_manager.open_positions),
                        "exit_decisions": t3_exit_result.decisions,
                    },
                )
        except Exception as exc:
            LOG.warning("T3 maker 退出评估异常: %s", exc)

        if shadow_t2_exit_manager is not None:
            try:
                shadow_exit_result = shadow_t2_exit_manager.evaluate(active_markets=execution_markets)
                if shadow_exit_result.attempted > 0 and event_recorder.is_enabled:
                    event_recorder.write_event(
                        "strategy_executions",
                        {
                            "tier": StrategyTier.STATISTICAL_ARB.name,
                            "signal_type": "wallet_alpha_candidate_shadow_exit",
                            "market_id": "",
                            "status": "shadow_exit_attempted",
                            "trade_count": shadow_exit_result.triggered,
                            "attempted_count": shadow_exit_result.attempted,
                            "partial_count": shadow_exit_result.partial,
                            "failed_count": shadow_exit_result.failed,
                            "open_positions": len(shadow_t2_exit_manager.open_positions),
                        },
                    )
            except Exception as exc:
                LOG.warning("wallet alpha shadow 退出评估异常: %s", exc)

        if not config.dry_run:
            live_markets_by_cid = {m.condition_id: m for m in execution_markets}

            def _register_live_maker_fill(trade: TradeRecord, _fill_delta: float) -> None:
                t3_maker_exit_manager.register_fill(
                    trade=trade,
                    market=live_markets_by_cid.get(trade.condition_id),
                )

            user_ws_markets.update(live_markets_by_cid)
            _sync_user_channel_fills(
                user_feed=user_feed,
                executor=executor,
                risk_mgr=risk_mgr,
                maker_strategy=maker_strategy,
                event_recorder=event_recorder,
                notifier=notifier,
                orchestrator=orchestrator,
                on_observed_maker_fill=_register_live_maker_fill,
                max_events=config.user_ws_max_events_per_cycle,
            )
            _sync_live_order_statuses(
                executor=executor,
                risk_mgr=risk_mgr,
                maker_strategy=maker_strategy,
                event_recorder=event_recorder,
                notifier=notifier,
                orchestrator=orchestrator,
                on_observed_maker_fill=_register_live_maker_fill,
            )
            _cancel_stale_maker_orders(
                config=config,
                executor=executor,
                risk_mgr=risk_mgr,
                event_recorder=event_recorder,
                orchestrator=orchestrator,
            )
            _audit_maker_order_scoring(
                config=config,
                executor=executor,
                risk_mgr=risk_mgr,
                event_recorder=event_recorder,
                state=maker_scoring_state,
                orchestrator=orchestrator,
            )

        if config.dry_run:
            # Allow REST fallback so we can sweep maker quotes in markets
            # that aren't on WS (WS_MAX_MARKETS covers only the hot pool).
            # `count_request=False` keeps the cycle stats clean — the
            # quotes that need REST will be served from the analyzer's
            # cache layer the vast majority of the time, since the same
            # token usually had a snapshot fetched earlier in the cycle
            # by scan/signal collection.
            shadow_fill_slots_used = 0
            # Per-market exposure already booked from prior cycles, used
            # to enforce RISK_MAX_EXPOSURE_PER_MARKET on shadow fills.
            # Pre-fix, the guard only checked total position count, so
            # five $20 fills on the same Iran market booked $100 against
            # a $25 cap (2026-05-22 run).
            shadow_market_exposure_base: dict[str, float] = (
                shadow_lifecycle.exposure_by_market_usdc()
                if shadow_lifecycle is not None
                else {}
            )
            shadow_market_exposure_cycle: dict[str, float] = {}

            def _shadow_maker_fill_guard(trade: TradeRecord, fill_size: float, cross_price: float):
                nonlocal shadow_fill_slots_used
                if str(getattr(trade.side, "value", trade.side)).upper() != "BUY":
                    return True, ""
                if risk_mgr.state.open_positions + shadow_fill_slots_used >= config.max_open_positions:
                    return False, "shadow_open_position_cap"
                cid = str(getattr(trade, "condition_id", "") or "")
                notional = float(fill_size) * float(cross_price)
                if cid:
                    projected = (
                        shadow_market_exposure_base.get(cid, 0.0)
                        + shadow_market_exposure_cycle.get(cid, 0.0)
                        + notional
                    )
                    if projected > config.max_exposure_per_market:
                        return False, "shadow_per_market_exposure_cap"
                    shadow_market_exposure_cycle[cid] = (
                        shadow_market_exposure_cycle.get(cid, 0.0) + notional
                    )
                shadow_fill_slots_used += 1
                return True, ""

            swept_maker_fills = executor.sweep_simulated_maker_fills(
                lambda token_id: ob_analyzer.get_snapshot(
                    token_id, allow_rest_fallback=True, count_request=False
                ),
                fill_latency_sec=config.shadow_maker_fill_latency_sec,
                fill_guard=_shadow_maker_fill_guard,
            )
            shadow_markets_by_cid = {m.condition_id: m for m in execution_markets}

            def _register_shadow_maker_fill(trade: TradeRecord, _fill_delta: float) -> None:
                t3_maker_exit_manager.register_fill(
                    trade=trade,
                    market=shadow_markets_by_cid.get(trade.condition_id),
                )

            observed_maker_fills = _handle_observed_maker_fills(
                trades=swept_maker_fills,
                maker_strategy=maker_strategy,
                notifier=notifier,
                event_recorder=event_recorder,
                simulated=True,
                event_name="shadow_maker_fill_observed",
                on_observed=_register_shadow_maker_fill,
            )
            total_simulated_successes += observed_maker_fills

        if (
            portfolio_sync is not None
            and (time.time() - last_portfolio_sync_ts) >= config.portfolio_sync_interval_sec
        ):
            last_portfolio_sync_ts, _ = _refresh_portfolio_snapshot(
                portfolio_sync=portfolio_sync,
                risk_mgr=risk_mgr,
                event_recorder=event_recorder,
                last_portfolio_sync_ts=last_portfolio_sync_ts,
                t2_exit_manager=t2_exit_manager,
            )

        shadow_snapshot: dict[str, Any] = {}
        if shadow_lifecycle is not None:
            shadow_snapshot = shadow_lifecycle.snapshot()
            if config.dry_run:
                risk_mgr.update_shadow_snapshot(shadow_snapshot)
            elif shadow_risk_mgr is not None:
                shadow_risk_mgr.update_shadow_snapshot(shadow_snapshot)
        telemetry_risk_state = risk_mgr.state

        vol_snap = vol_estimator.snapshot()
        ws_status = _build_ws_status(
            config=config,
            enhanced_store=enhanced_store,
            ws_target_ids=ws_target_ids,
            phase_hint="scan_complete",
        )
        if dashboard_enabled:
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
                risk_state=telemetry_risk_state,
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
                "cumulative_pnl": telemetry_risk_state.total_pnl,
                "realized_daily_pnl": telemetry_risk_state.daily_pnl,
                "unrealized_pnl": telemetry_risk_state.unrealized_pnl,
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
            daily_pnl=telemetry_risk_state.daily_pnl,
            open_positions=telemetry_risk_state.open_positions,
            focus_keywords=focus_keywords,
            unrealized_pnl=telemetry_risk_state.unrealized_pnl,
            total_pnl=telemetry_risk_state.total_pnl,
            current_position_value=telemetry_risk_state.current_position_value,
            cycle_status="ok",
            theoretical_opportunities_today=today_theoretical_opportunities,
            live_successes_today=today_live_successes,
            t0_opportunities_total=total_t0_opportunities,
            t0_opportunities_today=today_t0_opportunities,
            directional_signals_total=total_directional_signals,
            directional_signals_today=today_directional_signals,
            skip_reason_counts=skip_reason_counts,
        )

        now_ts = time.time()
        if event_recorder.is_enabled and (now_ts - last_telemetry_heartbeat_ts) >= _TELEMETRY_HEARTBEAT_SEC:
            event_recorder.write_event("risk_events", cycle_summary_payload)
            last_telemetry_heartbeat_ts = now_ts

        wallet_usdc = executor.get_available_collateral_balance(use_cache=True)
        LOG.info(
            "钱包余额观测: %s",
            f"{wallet_usdc:.6f} USDC" if wallet_usdc is not None else "N/A",
        )
        notifier.observe_cycle(
            daily_pnl=telemetry_risk_state.daily_pnl,
            open_positions=telemetry_risk_state.open_positions,
            total_exposure=telemetry_risk_state.total_exposure,
            is_halted=telemetry_risk_state.is_halted,
            halt_reason=telemetry_risk_state.halt_reason,
            wallet_usdc=wallet_usdc,
            now_ts=now_ts,
        )
        if telemetry_risk_state.is_halted and telemetry_risk_state.halt_reason:
            halt_context: list[str] = [
                f"连续失败: {telemetry_risk_state.consecutive_failures}/{config.max_consecutive_failures}",
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
                f"风控已熔断\n原因: {telemetry_risk_state.halt_reason}",
                error_key=f"risk_halt:{telemetry_risk_state.halt_reason}",
                context_lines=halt_context,
                now_ts=now_ts,
            )
        notifier.maybe_notify_pnl_alert(
            daily_pnl=telemetry_risk_state.daily_pnl,
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
    if spot_feed is not None:
        spot_feed.stop()
        LOG.info("Binance 现货 feed 已停止")
    rewards_client.close()
    if user_feed is not None:
        user_feed.stop()
        LOG.info("user 频道 WebSocket 已停止")
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
    if dashboard_enabled:
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


# Telemetry / dashboard serialisation / pending-signal lookups were
# extracted to `polymarket_arb.main_helpers.{dashboard_serializers,
# cycle_runners}` and re-imported above under their underscore aliases
# so the call graph here stays unchanged.
