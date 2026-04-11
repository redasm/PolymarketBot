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
    dry_run: bool
    min_liquidity: float
    min_volume_24h: float

    # 风险管理
    max_open_positions: int
    max_exposure_per_market: float
    max_total_exposure: float
    max_daily_loss: float
    max_consecutive_failures: int

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

    # Tick 录制
    tick_record_enabled: bool
    tick_record_dir: str

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

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> ArbConfig:
        """从 .env 文件和环境变量构建配置."""
        if dotenv_path:
            load_dotenv(dotenv_path)
        else:
            load_dotenv()

        private_key = _env("PRIVATE_KEY") or _env("POLYMARKET_PRIVATE_KEY")
        assert private_key, "必须设置 PRIVATE_KEY 或 POLYMARKET_PRIVATE_KEY"

        funder = _env("POLYMARKET_FUNDER")
        assert funder, "必须设置 POLYMARKET_FUNDER（代理钱包地址）"

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
            dry_run=_env_bool("ARB_DRY_RUN", True),
            min_liquidity=_env_float("ARB_MIN_LIQUIDITY", 1000.0),
            min_volume_24h=_env_float("ARB_MIN_VOLUME_24H", 500.0),
            max_open_positions=_env_int("RISK_MAX_OPEN_POSITIONS", 10),
            max_exposure_per_market=_env_float("RISK_MAX_EXPOSURE_PER_MARKET", 100.0),
            max_total_exposure=_env_float("RISK_MAX_TOTAL_EXPOSURE", 500.0),
            max_daily_loss=_env_float("RISK_MAX_DAILY_LOSS", 50.0),
            max_consecutive_failures=_env_int("RISK_MAX_CONSECUTIVE_FAILURES", 5),
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
            tick_record_enabled=_env_bool("TICK_RECORD_ENABLED", False),
            tick_record_dir=_env("TICK_RECORD_DIR", "data/ticks"),
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
            if "key" in k.lower() or "token" in k.lower() or "secret" in k.lower():
                d[k] = "***"
            else:
                d[k] = v
        return d
