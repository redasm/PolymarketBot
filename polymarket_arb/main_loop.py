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
import logging
import signal
import time
from typing import Any, Optional

from polymarket_arb.ai_advisor import AIAdvisor, create_ai_advisor
from polymarket_arb.ai_context import MarketContextBuilder
from polymarket_arb.arbitrage_detector import (
    ArbitrageDetector,
    format_arb_opportunity_zh,
)
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.client_factory import build_readonly_client, build_trading_client
from polymarket_arb.config import ArbConfig
from polymarket_arb.dashboard_api import DashboardState, start_dashboard_server
from polymarket_arb.edge_engine import EdgeEngine
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.logger_setup import setup_logging
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.models import ArbOpportunity, ArbType, MarketInfo
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

LOG = logging.getLogger("main_loop")

_SHUTDOWN = False


def _signal_handler(sig: int, frame: Any) -> None:
    global _SHUTDOWN
    LOG.info("收到信号 %d，准备优雅退出…", sig)
    _SHUTDOWN = True


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


def _start_ws_feed(
    targets: list[MarketInfo],
    enhanced_store: EnhancedBookStore,
) -> tuple[WebSocketFeed, OrderBookMirror]:
    """为选中的目标市场创建并启动 WebSocket feed.

    将第一个市场绑定到 EnhancedBookStore（用于 EdgeEngine），
    所有市场的 token 都订阅到 OrderBookMirror。
    """
    mirror = OrderBookMirror()

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
    config = ArbConfig.from_env(dotenv_path)
    setup_logging(config.log_level, config.log_file)

    LOG.info("=" * 60)
    LOG.info("Polymarket 套利机器人启动")
    LOG.info("模式: %s", "DRY RUN (仅扫描)" if config.dry_run else "LIVE (实盘交易)")
    LOG.info("配置: %s", config.dump_safe())
    LOG.info("=" * 60)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    ro_client = build_readonly_client(config)
    trading_client = None
    if not config.dry_run:
        trading_client = build_trading_client(config)

    scanner = MarketScanner(config)
    ob_analyzer = OrderBookAnalyzer(ro_client)
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
    )
    tick_recorder = TickRecorder(
        output_dir=config.tick_record_dir,
        enabled=config.tick_record_enabled,
    )
    if config.tick_record_enabled:
        LOG.info("Tick 录制已开启: %s", config.tick_record_dir)

    orchestrator = StrategyOrchestrator(total_bankroll=config.max_total_exposure)
    ctx_builder = MarketContextBuilder()

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

    while not _SHUTDOWN:
        cycle += 1
        cycle_start = time.time()

        try:
            opportunities, scanned_markets = _scan_cycle(
                scanner=scanner,
                detector=detector,
                config=config,
            )
            consecutive_api_errors = 0
        except Exception as e:
            consecutive_api_errors += 1
            LOG.error("扫描周期 #%d 异常: %s", cycle, e, exc_info=True)
            dash_state.append_error({"message": str(e), "timestamp": time.time()})
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
                        ws_feed, ws_mirror = _start_ws_feed(targets, enhanced_store)
                        ws_target_ids = new_ids

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

        for opp in opportunities:
            if _SHUTDOWN:
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
                continue

            can_trade, reason, adj_size = risk_mgr.pre_trade_check(verified, target_size)
            if not can_trade:
                LOG.info("风控拒绝: %s", reason)
                continue

            trades = executor.execute_arbitrage(verified, adj_size)
            risk_mgr.record_execution(verified, trades)
            total_arbs_executed += 1

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
            if filled:
                trade_msg = (
                    f"✅ 套利已执行\n"
                    f"事件: {opp.event_title}\n"
                    f"类型: {opp.arb_type.value}\n"
                    f"腿数: {len(filled)}/{len(opp.legs)}\n"
                    f"预期净利: ${opp.net_edge * adj_size:.4f}"
                )
                notifier.notify_trade(trade_msg)

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
                book_store=enhanced_store,
                vol_estimator=vol_estimator,
                edge_decision=edge_decision,
                risk_mgr=risk_mgr,
                orchestrator=orchestrator,
                dash_state=dash_state,
                config=config,
            )

        risk_s = risk_mgr.state
        vol_snap = vol_estimator.snapshot()
        ws_status = {
            "enabled": config.ws_enabled,
            "connected": enhanced_store._connected,
            "market_id": enhanced_store._market_id,
            "subscribed_tokens": len(ws_target_ids),
        }
        dash_state.update(
            cycle_count=cycle,
            arbs_found=total_arbs_found,
            arbs_executed=total_arbs_executed,
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
        )
        dash_state.append_pnl_point({
            "timestamp": time.time(),
            "cumulative_pnl": risk_s.daily_pnl,
        })

        elapsed = time.time() - cycle_start
        if cycle % 100 == 0:
            LOG.info(
                "状态: 已扫描 %d 周期, 发现 %d 机会, 执行 %d 次, WS=%s, 本周期 %.1fs",
                cycle,
                total_arbs_found,
                total_arbs_executed,
                "连接" if enhanced_store._connected else "未连接",
                elapsed,
            )
            notifier.notify_status(risk_mgr.format_status_zh())

        sleep_time = max(0, config.scan_interval_sec - elapsed)
        if sleep_time > 0 and not _SHUTDOWN:
            time.sleep(sleep_time)

    if ws_feed is not None:
        ws_feed.stop()
        LOG.info("WebSocket feed 已停止")
    tick_recorder.close()
    dash_state.update(is_running=False)
    LOG.info("机器人已停止。总计: %d 周期, %d 机会, %d 执行", cycle, total_arbs_found, total_arbs_executed)
    notifier.send("🛑 套利机器人已停止", category="shutdown", force=True)


def _scan_cycle(
    scanner: MarketScanner,
    detector: ArbitrageDetector,
    config: ArbConfig,
) -> tuple[list[ArbOpportunity], list[MarketInfo]]:
    """单次扫描周期：拉取市场 → 检测套利.

    Returns:
        (套利机会列表, 扫描到的全部市场列表)
    """
    opportunities: list[ArbOpportunity] = []

    markets = scanner.fetch_active_markets(
        min_liquidity=config.min_liquidity,
        min_volume_24h=config.min_volume_24h,
    )

    for market in markets:
        if len(market.tokens) == 2:
            opp = detector.scan_binary_market(market)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)

    events = scanner.fetch_active_events(limit=50)
    for event in events:
        if len(event.markets) >= 2:
            opp = detector.scan_multi_outcome_event(event)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)

    opportunities.sort(key=lambda o: o.net_edge, reverse=True)
    return opportunities, markets


def _run_ai_cycle(
    *,
    ai_advisor: AIAdvisor,
    ctx_builder: MarketContextBuilder,
    book_store: EnhancedBookStore,
    vol_estimator: VolEstimator,
    edge_decision: Any,
    risk_mgr: RiskManager,
    orchestrator: StrategyOrchestrator,
    dash_state: DashboardState,
    config: ArbConfig,
) -> None:
    """在主循环中执行一次 AI 评估（同步包装 async 调用）."""
    context = ctx_builder.build(
        book_store=book_store,
        vol_estimator=vol_estimator,
        edge_signals=[edge_decision.to_dict()] if edge_decision else [],
        risk_state=risk_mgr.state,
    )

    loop = _get_or_create_event_loop()
    try:
        decisions = loop.run_until_complete(ai_advisor.evaluate_markets(context))
    except Exception as e:
        LOG.error("AI 评估异常: %s", e, exc_info=True)
        dash_state.append_error({"message": f"AI error: {e}", "timestamp": time.time()})
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
        )
        orchestrator.submit_signal(signal)

    if config.ai_override_risk:
        try:
            adjustments = loop.run_until_complete(ai_advisor.adjust_risk_params(context))
            if adjustments:
                risk_mgr.apply_ai_adjustment(adjustments)
        except Exception as e:
            LOG.error("AI 风控调整异常: %s", e, exc_info=True)

    dash_state.update(ai_status=ai_advisor.get_status())


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
