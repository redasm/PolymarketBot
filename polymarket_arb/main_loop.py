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

import logging
import signal
import time
from typing import Any, Optional

from polymarket_arb.arbitrage_detector import (
    ArbitrageDetector,
    format_arb_opportunity_zh,
)
from polymarket_arb.client_factory import build_readonly_client, build_trading_client
from polymarket_arb.config import ArbConfig
from polymarket_arb.dashboard_api import DashboardState, start_dashboard_server
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.logger_setup import setup_logging
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.models import ArbOpportunity, ArbType
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.telegram_notifier import TelegramNotifier

LOG = logging.getLogger("main_loop")

_SHUTDOWN = False


def _signal_handler(sig: int, frame: Any) -> None:
    global _SHUTDOWN
    LOG.info("收到信号 %d，准备优雅退出…", sig)
    _SHUTDOWN = True


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
        f"扫描间隔: {config.scan_interval_sec}s",
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
            opportunities = _scan_cycle(
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

        risk_s = risk_mgr.state
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
        )
        dash_state.append_pnl_point({
            "timestamp": time.time(),
            "cumulative_pnl": risk_s.daily_pnl,
        })

        elapsed = time.time() - cycle_start
        if cycle % 100 == 0:
            LOG.info(
                "状态: 已扫描 %d 周期, 发现 %d 机会, 执行 %d 次, 本周期 %.1fs",
                cycle,
                total_arbs_found,
                total_arbs_executed,
                elapsed,
            )
            notifier.notify_status(risk_mgr.format_status_zh())

        sleep_time = max(0, config.scan_interval_sec - elapsed)
        if sleep_time > 0 and not _SHUTDOWN:
            time.sleep(sleep_time)

    dash_state.update(is_running=False)
    LOG.info("机器人已停止。总计: %d 周期, %d 机会, %d 执行", cycle, total_arbs_found, total_arbs_executed)
    notifier.send("🛑 套利机器人已停止", category="shutdown", force=True)


def _scan_cycle(
    scanner: MarketScanner,
    detector: ArbitrageDetector,
    config: ArbConfig,
) -> list[ArbOpportunity]:
    """单次扫描周期：拉取市场 → 检测套利."""
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
    return opportunities
