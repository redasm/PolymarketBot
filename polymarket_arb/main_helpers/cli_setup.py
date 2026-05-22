"""Startup / parsing helpers used to bootstrap `main_loop.main()`.

These are the side-effect-light glue functions that sit between
`ArbConfig` and the wired-up subsystems (research signal service,
cross-platform scanner, backtest report loader, …). Extracted so the
`main()` body can stay closer to a wiring + run-loop sketch.

Everything here either reads `ArbConfig` and returns an instance, or
performs a small text/JSON parse with explicit logging on failure. No
function in this module mutates global state, so they can each be
covered with a focused unit test.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from polymarket_arb.config import ArbConfig
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.strategies.cross_platform import CrossPlatformScanner, KalshiClient

if TYPE_CHECKING:
    from research_signal.service import ResearchSignalService

LOG = logging.getLogger("main_loop")


def build_run_instance_id(*, now_ts: float | None = None) -> str:
    """Stable per-run identifier embedded in dashboard rows and event logs."""
    ts = time.gmtime(now_ts if now_ts is not None else time.time())
    return f"run-{os.getpid()}-{time.strftime('%Y%m%dT%H%M%SZ', ts)}"


def log_startup_summary(config: ArbConfig, run_id: str) -> None:
    """Dump the operator-facing startup banner to the main log.

    Pulled out of `main()` so the launch-time summary can be reproduced
    in tests or CLI tooling without booting the whole loop.
    """
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
        "策略: maker=%s | 研究: research=%s feeds_file=%s | LLM配置: provider=%s model=%s",
        config.maker_strategy_enabled,
        config.research_signal_enabled,
        config.research_signal_feeds_file,
        config.ai_provider,
        config.ai_model,
    )
    LOG.info("=" * 60)


def parse_http_json_sources(raw: str) -> list[dict]:
    """Parse the JSON-list env var that configures HTTP JSON collectors.

    Returns an empty list (and logs at ERROR) on malformed input rather
    than raising, so an unparseable env var degrades to "no extra
    sources" rather than crashing the bot at startup.
    """
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


def create_research_signal_service(config: ArbConfig) -> Optional["ResearchSignalService"]:
    """Lazily import + construct the optional `ResearchSignalService`.

    The `research_signal` sub-package is optional (see
    `pyproject.toml` extras); if it can't be imported or fails to
    initialise we log loudly and return `None` so the rest of the bot
    keeps booting in research-disabled mode.
    """
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
            feeds_file=config.research_signal_feeds_file,
            http_json_sources=parse_http_json_sources(config.research_signal_http_json_sources),
            crypto_macro_enabled=config.research_signal_crypto_macro_enabled,
        )
    except Exception as e:
        LOG.error("Research Signal 功能已禁用: 初始化失败: %s", e, exc_info=True)
        return None


def create_cross_platform_scanner(
    config: ArbConfig,
    ob_analyzer: OrderBookAnalyzer,
) -> CrossPlatformScanner | None:
    """Construct the optional Polymarket↔Kalshi cross-platform scanner.

    Returns None when no pair list is configured or the JSON is invalid;
    the orchestrator then skips the T1 cross-platform tier entirely.
    """
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


def round_timing(value: float) -> float:
    """Clamp negative noise + round to 4 decimals for telemetry payloads."""
    return round(max(0.0, float(value or 0.0)), 4)


def load_last_backtest_report(backtest_reports_dir: str) -> dict:
    """Surface the most recent backtest report to the dashboard.

    Returns a `disabled`-shaped payload (rather than raising) when no
    reports exist or parsing fails, so the dashboard can render an empty
    state without special-casing exceptions.
    """
    output_dir = Path(backtest_reports_dir)
    if not output_dir.exists():
        return {"enabled": False, "reports_dir": backtest_reports_dir}

    report_files = sorted(output_dir.glob("*_report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not report_files:
        return {"enabled": False, "reports_dir": backtest_reports_dir}

    try:
        data: dict[str, Any] = json.loads(report_files[0].read_text(encoding="utf-8"))
        data["enabled"] = True
        data["path"] = str(report_files[0])
        trades_path = report_files[0].with_name(report_files[0].name.replace("_report.json", "_trades.jsonl"))
        if trades_path.exists():
            preview_rows: list[dict] = []
            for line in trades_path.read_text(encoding="utf-8").splitlines()[-5:]:
                if not line.strip():
                    continue
                preview_rows.append(json.loads(line))
            data["trades_path"] = str(trades_path)
            data["recent_trade_rows"] = preview_rows
        return data
    except Exception:
        return {"enabled": True, "reports_dir": backtest_reports_dir, "error": "report_parse_failed"}
