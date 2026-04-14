"""配置管理：从环境变量读取所有参数，提供类型安全的访问."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

LOG = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float = 0.0) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOG.warning("env %s=%r 无法解析为 float，使用默认值 %s", key, raw, default)
        return default


def _env_int(key: str, default: int = 0) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOG.warning("env %s=%r 无法解析为 int，使用默认值 %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class ArbConfig:
    """套利机器人完整配置，所有字段从环境变量读取."""

    # 钱包与认证
    private_key: str
    funder_address: str
    signature_type: int
    chain_id: int

    # 端点
    clob_host: str
    gamma_host: str

    # 套利参数
    min_edge_usd: float
    min_edge_pct: float
    max_order_size_usdc: float
    default_order_size_usdc: float
    scan_interval_sec: float
    market_fetch_limit: int
    market_universe_refresh_sec: float
    hot_market_pool_size: int
    hot_event_pool_size: int
    market_focus_keywords: str
    dry_run: bool
    min_liquidity: float
    min_volume_24h: float
    orderbook_snapshot_ttl_sec: float
    orderbook_ws_snapshot_max_age_sec: float
    orderbook_retry_count: int
    orderbook_retry_delay_sec: float
    orderbook_missing_cooldown_sec: float
    cross_platform_pairs_json: str
    polymarket_taker_fee_rate: float
    kalshi_taker_fee_rate: float
    max_multi_outcome_legs: int

    # 风险管理
    max_open_positions: int
    max_exposure_per_market: float
    max_total_exposure: float
    max_daily_loss: float
    max_consecutive_failures: int
    risk_event_cooldown_sec: float
    risk_pending_reservation_ttl_sec: float

    # Telegram
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    notify_on_arb_found: bool
    notify_on_trade: bool
    notify_on_error: bool
    telegram_cooldown_sec: float

    # 波动率
    vol_fast_minutes: int
    vol_slow_minutes: int
    vol_min_bars: int

    # Edge 引擎
    edge_min_bps: float
    edge_max_spread_bps: float
    edge_min_confidence: float
    edge_confidence_full_bps: float
    edge_confidence_imbalance_weight: float
    edge_volatility_spike_ratio: float
    edge_volatility_spike_penalty: float
    edge_volatility_calm_ratio: float
    edge_volatility_calm_boost: float
    t2_min_deviation: float
    t2_max_spread_bps: float
    t2_min_top_depth: float
    t2_max_complement_error_bps: float

    # Tick 录制
    tick_record_enabled: bool
    tick_record_dir: str
    telemetry_record_enabled: bool
    telemetry_record_dir: str

    # Data 清理
    data_cleanup_enabled: bool
    data_cleanup_interval_sec: float
    data_ticks_retention_days: int
    data_ticks_max_gb: float
    data_telemetry_retention_days: int
    data_telemetry_max_gb: float
    data_research_cache_retention_days: int
    data_research_cache_max_gb: float
    data_backtest_retention_days: int
    data_backtest_max_gb: float

    # Dashboard
    dashboard_enabled: bool
    dashboard_port: int

    # 日志
    log_level: str
    log_file: str

    # WebSocket 实时数据
    ws_enabled: bool
    ws_max_markets: int
    ws_refresh_cycles: int
    ws_vol_feed_interval_sec: float

    # AI 决策引擎
    ai_enabled: bool
    ai_provider: str
    ai_api_key: str
    ai_api_base: str
    ai_model: str
    ai_temperature: float
    ai_eval_interval_sec: float
    ai_max_cost_per_day: float
    ai_override_risk: bool
    ai_auto_recover_sec: float

    # Research Signal
    research_signal_enabled: bool
    research_signal_window_sec: int
    research_signal_max_items: int
    research_signal_cache_ttl_sec: int
    research_signal_cache_dir: str
    research_signal_extra_rss_feeds: str
    research_signal_http_json_sources: str
    research_signal_surf_enabled: bool
    research_signal_surf_api_key: str
    research_signal_surf_api_base: str
    research_signal_surf_model: str
    research_signal_surf_timeout_sec: float
    research_signal_surf_cache_ttl_sec: float
    research_signal_knowledge_enabled: bool
    research_signal_knowledge_dir: str
    research_signal_knowledge_max_matches: int

    # Backtest
    backtest_enabled: bool
    backtest_data_dir: str
    backtest_default_dataset: str
    backtest_slippage_bps: float
    backtest_reports_dir: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.clob_host or not self.gamma_host:
            raise ValueError("CLOB_HOST 和 GAMMA_HOST 不能为空")
        if self.market_fetch_limit <= 0:
            raise ValueError("ARB_MARKET_FETCH_LIMIT 必须大于 0")
        if self.market_universe_refresh_sec <= 0:
            raise ValueError("ARB_MARKET_UNIVERSE_REFRESH_SEC 必须大于 0")
        if self.hot_market_pool_size <= 0:
            raise ValueError("ARB_HOT_MARKET_POOL_SIZE 必须大于 0")
        if self.hot_event_pool_size <= 0:
            raise ValueError("ARB_HOT_EVENT_POOL_SIZE 必须大于 0")
        if self.scan_interval_sec <= 0:
            raise ValueError("ARB_SCAN_INTERVAL_SEC 必须大于 0")
        if self.orderbook_retry_count < 0:
            raise ValueError("ORDERBOOK_RETRY_COUNT 不能为负数")
        if self.orderbook_ws_snapshot_max_age_sec < 0:
            raise ValueError("ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC 不能为负数")
        if self.orderbook_retry_delay_sec < 0:
            raise ValueError("ORDERBOOK_RETRY_DELAY_SEC 不能为负数")
        if self.orderbook_missing_cooldown_sec < 0:
            raise ValueError("ORDERBOOK_MISSING_COOLDOWN_SEC 不能为负数")
        if self.max_multi_outcome_legs < 2:
            raise ValueError("ARB_MAX_MULTI_OUTCOME_LEGS 必须至少为 2")
        if self.max_open_positions <= 0:
            raise ValueError("RISK_MAX_OPEN_POSITIONS 必须大于 0")
        if self.max_exposure_per_market <= 0 or self.max_total_exposure <= 0:
            raise ValueError("风险敞口上限必须大于 0")
        if self.max_daily_loss < 0 or self.ai_max_cost_per_day < 0:
            raise ValueError("RISK_MAX_DAILY_LOSS 和 AI_MAX_COST_PER_DAY 不能为负数")
        if self.risk_event_cooldown_sec < 0:
            raise ValueError("RISK_EVENT_COOLDOWN_SEC 不能为负数")
        if self.risk_pending_reservation_ttl_sec < 0:
            raise ValueError("RISK_PENDING_RESERVATION_TTL_SEC 不能为负数")
        if self.data_cleanup_enabled and self.data_cleanup_interval_sec <= 0:
            raise ValueError("DATA_CLEANUP_INTERVAL_SEC 必须大于 0")
        if min(
            self.data_ticks_retention_days,
            self.data_telemetry_retention_days,
            self.data_research_cache_retention_days,
            self.data_backtest_retention_days,
        ) < 0:
            raise ValueError("数据保留天数不能为负数")
        if min(
            self.data_ticks_max_gb,
            self.data_telemetry_max_gb,
            self.data_research_cache_max_gb,
            self.data_backtest_max_gb,
        ) < 0:
            raise ValueError("数据目录容量上限不能为负数")
        if not 0 <= self.edge_min_confidence <= 1:
            raise ValueError("EDGE_MIN_CONFIDENCE 必须在 [0, 1] 区间")
        if self.edge_confidence_full_bps <= 0:
            raise ValueError("EDGE_CONFIDENCE_FULL_BPS 必须大于 0")
        if self.edge_confidence_imbalance_weight < 0:
            raise ValueError("EDGE_CONFIDENCE_IMBALANCE_WEIGHT 不能为负数")
        if self.edge_volatility_spike_ratio <= 0 or self.edge_volatility_calm_ratio <= 0:
            raise ValueError("波动率比率阈值必须大于 0")
        if self.edge_volatility_spike_penalty <= 0 or self.edge_volatility_calm_boost <= 0:
            raise ValueError("波动率置信度调整因子必须大于 0")
        if self.t2_min_deviation < 0:
            raise ValueError("T2_MIN_DEVIATION 不能为负数")
        if self.t2_max_spread_bps < 0:
            raise ValueError("T2_MAX_SPREAD_BPS 不能为负数")
        if self.t2_min_top_depth < 0:
            raise ValueError("T2_MIN_TOP_DEPTH 不能为负数")
        if self.t2_max_complement_error_bps < 0:
            raise ValueError("T2_MAX_COMPLEMENT_ERROR_BPS 不能为负数")

    @classmethod
    def from_env(
        cls,
        dotenv_path: str | Path | None = None,
        *,
        require_wallet: bool = True,
    ) -> ArbConfig:
        """从 .env 文件和环境变量构建配置."""
        if dotenv_path:
            load_dotenv(dotenv_path)
        else:
            load_dotenv()

        private_key = _env("PRIVATE_KEY") or _env("POLYMARKET_PRIVATE_KEY")
        funder = _env("POLYMARKET_FUNDER")
        if require_wallet:
            if not private_key:
                raise ValueError("必须设置 PRIVATE_KEY 或 POLYMARKET_PRIVATE_KEY")
            if not funder:
                raise ValueError("必须设置 POLYMARKET_FUNDER（代理钱包地址）")
        else:
            private_key = private_key or "research-mode"
            funder = funder or "research-mode"

        cfg = cls(
            private_key=private_key,
            funder_address=funder,
            signature_type=_env_int("POLYMARKET_SIGNATURE_TYPE", 2),
            chain_id=_env_int("CHAIN_ID", 137),
            clob_host=_env("CLOB_HOST", "https://clob.polymarket.com"),
            gamma_host=_env("GAMMA_HOST", "https://gamma-api.polymarket.com"),
            min_edge_usd=_env_float("ARB_MIN_EDGE_USD", 0.005),
            min_edge_pct=_env_float("ARB_MIN_EDGE_PCT", 0.3),
            max_order_size_usdc=_env_float("ARB_MAX_ORDER_SIZE_USDC", 50.0),
            default_order_size_usdc=_env_float("ARB_DEFAULT_ORDER_SIZE_USDC", 10.0),
            scan_interval_sec=_env_float("ARB_SCAN_INTERVAL_SEC", 5.0),
            market_fetch_limit=_env_int("ARB_MARKET_FETCH_LIMIT", 100),
            market_universe_refresh_sec=_env_float("ARB_MARKET_UNIVERSE_REFRESH_SEC", 600.0),
            hot_market_pool_size=_env_int("ARB_HOT_MARKET_POOL_SIZE", 80),
            hot_event_pool_size=_env_int("ARB_HOT_EVENT_POOL_SIZE", 30),
            market_focus_keywords=_env("ARB_MARKET_FOCUS_KEYWORDS", ""),
            dry_run=_env_bool("ARB_DRY_RUN", True),
            min_liquidity=_env_float("ARB_MIN_LIQUIDITY", 1000.0),
            min_volume_24h=_env_float("ARB_MIN_VOLUME_24H", 500.0),
            orderbook_snapshot_ttl_sec=_env_float("ORDERBOOK_SNAPSHOT_TTL_SEC", 0.5),
            orderbook_ws_snapshot_max_age_sec=_env_float("ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC", 10.0),
            orderbook_retry_count=_env_int("ORDERBOOK_RETRY_COUNT", 2),
            orderbook_retry_delay_sec=_env_float("ORDERBOOK_RETRY_DELAY_SEC", 0.15),
            orderbook_missing_cooldown_sec=_env_float("ORDERBOOK_MISSING_COOLDOWN_SEC", 300.0),
            cross_platform_pairs_json=_env("CROSS_PLATFORM_PAIRS_JSON", ""),
            polymarket_taker_fee_rate=_env_float("POLYMARKET_TAKER_FEE_RATE", 0.02),
            kalshi_taker_fee_rate=_env_float("KALSHI_TAKER_FEE_RATE", 0.003),
            max_multi_outcome_legs=_env_int("ARB_MAX_MULTI_OUTCOME_LEGS", 20),
            max_open_positions=_env_int("RISK_MAX_OPEN_POSITIONS", 10),
            max_exposure_per_market=_env_float("RISK_MAX_EXPOSURE_PER_MARKET", 100.0),
            max_total_exposure=_env_float("RISK_MAX_TOTAL_EXPOSURE", 500.0),
            max_daily_loss=_env_float("RISK_MAX_DAILY_LOSS", 50.0),
            max_consecutive_failures=_env_int("RISK_MAX_CONSECUTIVE_FAILURES", 5),
            risk_event_cooldown_sec=_env_float("RISK_EVENT_COOLDOWN_SEC", 60.0),
            risk_pending_reservation_ttl_sec=_env_float("RISK_PENDING_RESERVATION_TTL_SEC", 30.0),
            telegram_enabled=_env_bool("TELEGRAM_ENABLED", False),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            notify_on_arb_found=_env_bool("TELEGRAM_NOTIFY_ON_ARB_FOUND", True),
            notify_on_trade=_env_bool("TELEGRAM_NOTIFY_ON_TRADE", True),
            notify_on_error=_env_bool("TELEGRAM_NOTIFY_ON_ERROR", True),
            telegram_cooldown_sec=_env_float("TELEGRAM_NOTIFY_COOLDOWN_SEC", 30.0),
            vol_fast_minutes=_env_int("VOL_FAST_MINUTES", 60),
            vol_slow_minutes=_env_int("VOL_SLOW_MINUTES", 360),
            vol_min_bars=_env_int("VOL_MIN_BARS", 20),
            edge_min_bps=_env_float("EDGE_MIN_BPS", 100.0),
            edge_max_spread_bps=_env_float("EDGE_MAX_SPREAD_BPS", 500.0),
            edge_min_confidence=_env_float("EDGE_MIN_CONFIDENCE", 0.4),
            edge_confidence_full_bps=_env_float("EDGE_CONFIDENCE_FULL_BPS", 500.0),
            edge_confidence_imbalance_weight=_env_float("EDGE_CONFIDENCE_IMBALANCE_WEIGHT", 0.1),
            edge_volatility_spike_ratio=_env_float("EDGE_VOLATILITY_SPIKE_RATIO", 2.0),
            edge_volatility_spike_penalty=_env_float("EDGE_VOLATILITY_SPIKE_PENALTY", 0.7),
            edge_volatility_calm_ratio=_env_float("EDGE_VOLATILITY_CALM_RATIO", 0.8),
            edge_volatility_calm_boost=_env_float("EDGE_VOLATILITY_CALM_BOOST", 1.1),
            t2_min_deviation=_env_float("T2_MIN_DEVIATION", 0.02),
            t2_max_spread_bps=_env_float("T2_MAX_SPREAD_BPS", 80.0),
            t2_min_top_depth=_env_float("T2_MIN_TOP_DEPTH", 100.0),
            t2_max_complement_error_bps=_env_float("T2_MAX_COMPLEMENT_ERROR_BPS", 150.0),
            tick_record_enabled=_env_bool("TICK_RECORD_ENABLED", False),
            tick_record_dir=_env("TICK_RECORD_DIR", "data/ticks"),
            telemetry_record_enabled=_env_bool("TELEMETRY_RECORD_ENABLED", False),
            telemetry_record_dir=_env("TELEMETRY_RECORD_DIR", "data/telemetry"),
            data_cleanup_enabled=_env_bool("DATA_CLEANUP_ENABLED", True),
            data_cleanup_interval_sec=_env_float("DATA_CLEANUP_INTERVAL_SEC", 3600.0),
            data_ticks_retention_days=_env_int("DATA_TICKS_RETENTION_DAYS", 7),
            data_ticks_max_gb=_env_float("DATA_TICKS_MAX_GB", 5.0),
            data_telemetry_retention_days=_env_int("DATA_TELEMETRY_RETENTION_DAYS", 14),
            data_telemetry_max_gb=_env_float("DATA_TELEMETRY_MAX_GB", 2.0),
            data_research_cache_retention_days=_env_int("DATA_RESEARCH_CACHE_RETENTION_DAYS", 14),
            data_research_cache_max_gb=_env_float("DATA_RESEARCH_CACHE_MAX_GB", 1.0),
            data_backtest_retention_days=_env_int("DATA_BACKTEST_RETENTION_DAYS", 30),
            data_backtest_max_gb=_env_float("DATA_BACKTEST_MAX_GB", 2.0),
            dashboard_enabled=_env_bool("DASHBOARD_ENABLED", True),
            dashboard_port=_env_int("DASHBOARD_PORT", 8077),
            log_level=_env("LOG_LEVEL", "INFO"),
            log_file=_env("LOG_FILE", "arb_bot.log"),
            ws_enabled=_env_bool("WS_ENABLED", True),
            ws_max_markets=_env_int("WS_MAX_MARKETS", 3),
            ws_refresh_cycles=_env_int("WS_REFRESH_CYCLES", 200),
            ws_vol_feed_interval_sec=_env_float("WS_VOL_FEED_INTERVAL_SEC", 60.0),
            ai_enabled=_env_bool("AI_ENABLED", False),
            ai_provider=_env("AI_PROVIDER", "openai"),
            ai_api_key=_env("AI_API_KEY") or _env("OPENAI_API_KEY"),
            ai_api_base=_env("AI_API_BASE"),
            ai_model=_env("AI_MODEL", "gpt-4o"),
            ai_temperature=_env_float("AI_TEMPERATURE", 0.1),
            ai_eval_interval_sec=_env_float("AI_EVAL_INTERVAL_SEC", 30.0),
            ai_max_cost_per_day=_env_float("AI_MAX_COST_PER_DAY", 5.0),
            ai_override_risk=_env_bool("AI_OVERRIDE_RISK", False),
            ai_auto_recover_sec=_env_float("AI_AUTO_RECOVER_SEC", 1800.0),
            research_signal_enabled=_env_bool("RESEARCH_SIGNAL_ENABLED", False),
            research_signal_window_sec=_env_int("RESEARCH_SIGNAL_WINDOW_SEC", 86400),
            research_signal_max_items=_env_int("RESEARCH_SIGNAL_MAX_ITEMS", 5),
            research_signal_cache_ttl_sec=_env_int("RESEARCH_SIGNAL_CACHE_TTL_SEC", 300),
            research_signal_cache_dir=_env("RESEARCH_SIGNAL_CACHE_DIR", "data/research_signal"),
            research_signal_extra_rss_feeds=_env("RESEARCH_SIGNAL_EXTRA_RSS_FEEDS", ""),
            research_signal_http_json_sources=_env("RESEARCH_SIGNAL_HTTP_JSON_SOURCES", ""),
            research_signal_surf_enabled=_env_bool("RESEARCH_SIGNAL_SURF_ENABLED", False),
            research_signal_surf_api_key=_env("RESEARCH_SIGNAL_SURF_API_KEY", ""),
            research_signal_surf_api_base=_env("RESEARCH_SIGNAL_SURF_API_BASE", "https://api.asksurf.ai/surf-ai"),
            research_signal_surf_model=_env("RESEARCH_SIGNAL_SURF_MODEL", "surf-1.5-instant"),
            research_signal_surf_timeout_sec=_env_float("RESEARCH_SIGNAL_SURF_TIMEOUT_SEC", 8.0),
            research_signal_surf_cache_ttl_sec=_env_float("RESEARCH_SIGNAL_SURF_CACHE_TTL_SEC", 1800.0),
            research_signal_knowledge_enabled=_env_bool("RESEARCH_SIGNAL_KNOWLEDGE_ENABLED", False),
            research_signal_knowledge_dir=_env("RESEARCH_SIGNAL_KNOWLEDGE_DIR", "data/research_signal/knowledge"),
            research_signal_knowledge_max_matches=_env_int("RESEARCH_SIGNAL_KNOWLEDGE_MAX_MATCHES", 3),
            backtest_enabled=_env_bool("BACKTEST_ENABLED", False),
            backtest_data_dir=_env("BACKTEST_DATA_DIR", "data/backtest"),
            backtest_default_dataset=_env("BACKTEST_DEFAULT_DATASET", "default"),
            backtest_slippage_bps=_env_float("BACKTEST_SLIPPAGE_BPS", 5.0),
            backtest_reports_dir=_env("BACKTEST_REPORTS_DIR", "research/backtest/output"),
        )

        LOG.info(
            "配置加载完成: dry_run=%s, min_edge=%.4f USD / %.2f%%, scan_interval=%.1fs",
            cfg.dry_run,
            cfg.min_edge_usd,
            cfg.min_edge_pct,
            cfg.scan_interval_sec,
        )
        return cfg

    def dump_safe(self) -> dict:
        """输出不含敏感信息的配置摘要."""
        d = {}
        for k, v in self.__dict__.items():
            key_lower = k.lower()
            if "key" in key_lower or "token" in key_lower or "secret" in key_lower:
                d[k] = "***"
            elif k == "funder_address" and isinstance(v, str) and v:
                d[k] = _mask_sensitive_value(v)
            else:
                d[k] = v
        return d


def _mask_sensitive_value(value: str, *, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return f"{value[:prefix]}***{value[-suffix:]}"
