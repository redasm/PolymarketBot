"""共享 fixture：构造 mock 对象、常用测试数据."""

from __future__ import annotations

from pathlib import Path

import pytest

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import OrderBookLevel, OrderBookSnapshot


@pytest.fixture()
def make_snapshot():
    """工厂 fixture：快速构造 OrderBookSnapshot."""

    def _make(
        token_id: str = "0xabc",
        best_bid: float | None = 0.48,
        best_ask: float | None = 0.52,
        bids: list[tuple[float, float]] | None = None,
        asks: list[tuple[float, float]] | None = None,
    ) -> OrderBookSnapshot:
        bid_levels = [OrderBookLevel(p, s) for p, s in (bids or [(best_bid, 100)])] if best_bid is not None else []
        ask_levels = [OrderBookLevel(p, s) for p, s in (asks or [(best_ask, 100)])] if best_ask is not None else []
        return OrderBookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bid_levels,
            asks=ask_levels,
        )

    return _make


class MockOrderBookAnalyzer:
    """用预置快照替代真实 CLOB 调用的 mock."""

    def __init__(self, snapshots: dict[str, OrderBookSnapshot]):
        self._snaps = snapshots

    def get_snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        return self._snaps.get(token_id)

    def get_executable_ask_price(self, token_id: str, target_size: float):
        snap = self._snaps.get(token_id)
        if snap is None or not snap.asks:
            return None
        total_cost = 0.0
        filled = 0.0
        for level in snap.asks:
            take = min(level.size, target_size - filled)
            total_cost += take * level.price
            filled += take
            if filled >= target_size - 1e-9:
                break
        if filled <= 0:
            return None
        return (total_cost / filled, filled)

    def get_executable_bid_price(self, token_id: str, target_size: float):
        snap = self._snaps.get(token_id)
        if snap is None or not snap.bids:
            return None
        total_value = 0.0
        filled = 0.0
        for level in snap.bids:
            take = min(level.size, target_size - filled)
            total_value += take * level.price
            filled += take
            if filled >= target_size - 1e-9:
                break
        if filled <= 0:
            return None
        return (total_value / filled, filled)


def make_test_config(**overrides) -> ArbConfig:
    defaults = dict(
        private_key="0xdead",
        funder_address="0xbeef",
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        gamma_host="https://gamma-api.polymarket.com",
        min_edge_usd=0.001,
        min_edge_pct=0.1,
        max_order_size_usdc=50.0,
        default_order_size_usdc=10.0,
        scan_interval_sec=5.0,
        market_fetch_limit=100,
        market_universe_refresh_sec=600.0,
        hot_market_pool_size=80,
        hot_event_pool_size=30,
        market_focus_keywords="",
        dry_run=True,
        min_liquidity=0.0,
        min_volume_24h=0.0,
        orderbook_snapshot_ttl_sec=0.5,
        orderbook_ws_snapshot_max_age_sec=10.0,
        orderbook_retry_count=2,
        orderbook_retry_delay_sec=0.15,
        orderbook_missing_cooldown_sec=300.0,
        cross_platform_pairs_json="",
        polymarket_taker_fee_rate=0.02,
        kalshi_taker_fee_rate=0.003,
        max_multi_outcome_legs=20,
        max_open_positions=10,
        max_exposure_per_market=100.0,
        max_total_exposure=500.0,
        max_daily_loss=50.0,
        max_consecutive_failures=5,
        risk_event_cooldown_sec=60.0,
        risk_pending_reservation_ttl_sec=30.0,
        telegram_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
        notify_on_arb_found=False,
        notify_on_trade=False,
        notify_on_error=False,
        telegram_cooldown_sec=30.0,
        vol_fast_minutes=60,
        vol_slow_minutes=360,
        vol_min_bars=20,
        edge_min_bps=100.0,
        edge_max_spread_bps=500.0,
        edge_min_confidence=0.4,
        edge_confidence_full_bps=500.0,
        edge_confidence_imbalance_weight=0.1,
        edge_volatility_spike_ratio=2.0,
        edge_volatility_spike_penalty=0.7,
        edge_volatility_calm_ratio=0.8,
        edge_volatility_calm_boost=1.1,
        t2_max_spread_bps=80.0,
        t2_min_top_depth=100.0,
        t2_max_complement_error_bps=150.0,
        tick_record_enabled=False,
        tick_record_dir="data/ticks",
        telemetry_record_enabled=False,
        telemetry_record_dir="data/telemetry",
        data_cleanup_enabled=True,
        data_cleanup_interval_sec=3600.0,
        data_ticks_retention_days=7,
        data_ticks_max_gb=5.0,
        data_telemetry_retention_days=14,
        data_telemetry_max_gb=2.0,
        data_research_cache_retention_days=14,
        data_research_cache_max_gb=1.0,
        data_backtest_retention_days=30,
        data_backtest_max_gb=2.0,
        dashboard_enabled=False,
        dashboard_port=8077,
        log_level="WARNING",
        log_file="",
        ws_enabled=True,
        ws_max_markets=3,
        ws_refresh_cycles=200,
        ws_vol_feed_interval_sec=60.0,
        ai_enabled=False,
        ai_provider="openai",
        ai_api_key="",
        ai_api_base="",
        ai_model="gpt-4o",
        ai_temperature=0.1,
        ai_eval_interval_sec=30.0,
        ai_max_cost_per_day=5.0,
        ai_override_risk=False,
        ai_auto_recover_sec=1800.0,
        research_signal_enabled=False,
        research_signal_window_sec=86400,
        research_signal_max_items=5,
        research_signal_cache_ttl_sec=300,
        research_signal_cache_dir="data/research_signal",
        research_signal_extra_rss_feeds="",
        research_signal_http_json_sources="",
        research_signal_surf_enabled=False,
        research_signal_surf_api_key="",
        research_signal_surf_api_base="https://api.asksurf.ai/surf-ai",
        research_signal_surf_model="surf-1.5-instant",
        research_signal_surf_timeout_sec=8.0,
        research_signal_surf_cache_ttl_sec=1800.0,
        research_signal_knowledge_enabled=False,
        research_signal_knowledge_dir="data/research_signal/knowledge",
        research_signal_knowledge_max_matches=3,
        backtest_enabled=False,
        backtest_data_dir="data/backtest",
        backtest_default_dataset="default",
        backtest_slippage_bps=5.0,
        backtest_reports_dir="research/backtest/output",
    )
    defaults.update(overrides)
    return ArbConfig(**defaults)


def write_test_env(tmp_path: Path) -> Path:
    env_path = tmp_path / ".env.test"
    env_path.write_text(
        "\n".join(
            [
                "PRIVATE_KEY=0xdead",
                "POLYMARKET_FUNDER=0xbeef",
                "ARB_DRY_RUN=true",
                "ARB_MARKET_UNIVERSE_REFRESH_SEC=600",
                "ARB_HOT_MARKET_POOL_SIZE=80",
                "ARB_HOT_EVENT_POOL_SIZE=30",
                "ARB_MARKET_FOCUS_KEYWORDS=",
                "RESEARCH_SIGNAL_ENABLED=false",
                "RESEARCH_SIGNAL_CACHE_TTL_SEC=300",
                "RESEARCH_SIGNAL_CACHE_DIR=data/research_signal",
                "RESEARCH_SIGNAL_HTTP_JSON_SOURCES=",
                "RESEARCH_SIGNAL_SURF_ENABLED=false",
                "RESEARCH_SIGNAL_SURF_API_KEY=",
                "RESEARCH_SIGNAL_SURF_API_BASE=https://api.asksurf.ai/surf-ai",
                "RESEARCH_SIGNAL_SURF_MODEL=surf-1.5-instant",
                "RESEARCH_SIGNAL_SURF_TIMEOUT_SEC=8",
                "RESEARCH_SIGNAL_SURF_CACHE_TTL_SEC=1800",
                "TELEMETRY_RECORD_ENABLED=false",
                "TELEMETRY_RECORD_DIR=data/telemetry",
                "DATA_CLEANUP_ENABLED=true",
                "DATA_CLEANUP_INTERVAL_SEC=3600",
                "DATA_TICKS_RETENTION_DAYS=7",
                "DATA_TICKS_MAX_GB=5",
                "DATA_TELEMETRY_RETENTION_DAYS=14",
                "DATA_TELEMETRY_MAX_GB=2",
                "DATA_RESEARCH_CACHE_RETENTION_DAYS=14",
                "DATA_RESEARCH_CACHE_MAX_GB=1",
                "DATA_BACKTEST_RETENTION_DAYS=30",
                "DATA_BACKTEST_MAX_GB=2",
                "ORDERBOOK_RETRY_COUNT=2",
                "ORDERBOOK_RETRY_DELAY_SEC=0.15",
                "ORDERBOOK_MISSING_COOLDOWN_SEC=300",
                "T2_MAX_SPREAD_BPS=80",
                "T2_MIN_TOP_DEPTH=100",
                "T2_MAX_COMPLEMENT_ERROR_BPS=150",
                "CROSS_PLATFORM_PAIRS_JSON=",
                "ARB_MAX_MULTI_OUTCOME_LEGS=20",
                "RISK_EVENT_COOLDOWN_SEC=60",
                "RISK_PENDING_RESERVATION_TTL_SEC=30",
                "BACKTEST_ENABLED=false",
                "BACKTEST_SLIPPAGE_BPS=5",
                "BACKTEST_REPORTS_DIR=research/backtest/output",
            ]
        ),
        encoding="utf-8",
    )
    return env_path
