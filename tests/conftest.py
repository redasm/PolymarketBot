"""共享 fixture：构造 mock 对象、常用测试数据."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import OrderBookLevel, OrderBookSnapshot


@pytest.fixture(autouse=True)
def _isolate_env_pollution():
    """Snapshot `os.environ` so dotenv writes in one test cannot leak into the next.

    `ArbConfig.from_env` calls `load_dotenv(override=True)`, which mutates
    `os.environ` directly and bypasses pytest's `monkeypatch` rollback. Without
    this fixture, e.g. `test_from_env_accepts_deposit_wallet_alias` writes
    `POLYMARKET_DEPOSIT_WALLET=0xdeposit` into the global env, and a later test
    that calls `from_env` in research mode silently picks it up.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        for key in list(os.environ.keys()):
            if key not in snapshot:
                del os.environ[key]
        for key, value in snapshot.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


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
        clob_client_version="v1",
        clob_api_key="",
        clob_api_secret="",
        clob_api_passphrase="",
        min_edge_usd=0.001,
        min_edge_pct=0.1,
        max_order_size_usdc=50.0,
        default_order_size_usdc=10.0,
        scan_interval_sec=5.0,
        dirty_market_wake_threshold=1,
        telemetry_async_write=False,
        telemetry_async_queue_size=1000,
        market_fetch_limit=100,
        market_universe_refresh_sec=600.0,
        hot_market_pool_size=80,
        hot_event_pool_size=30,
        market_focus_keywords="",
        dry_run=True,
        live_trading_ack=True,
        live_require_portfolio_sync=False,
        live_allow_zero_taker_fee=False,
        live_max_order_size_usdc=50.0,
        live_max_total_exposure_usdc=500.0,
        live_min_net_edge_bps=25.0,
        live_min_net_edge_usd=0.0025,
        live_max_orderbook_snapshot_age_sec=1.0,
        live_min_ws_hit_ratio=0.25,
        maker_strategy_enabled=True,
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
        t0_min_multi_outcome_median_leg_price=0.05,
        t0_require_complete_partition=True,
        max_open_positions=10,
        max_exposure_per_market=100.0,
        max_total_exposure=500.0,
        max_daily_loss=50.0,
        max_consecutive_failures=5,
        risk_event_cooldown_sec=60.0,
        risk_pending_reservation_ttl_sec=30.0,
        risk_halt_auto_recover_sec=3600.0,
        maker_stale_order_ttl_sec=60.0,
        maker_max_hold_sec=21600.0,
        maker_stop_loss_bps=300.0,
        maker_take_profit_bps=200.0,
        maker_exit_eval_interval_sec=30.0,
        t2_max_horizon_days=90.0,
        t2_max_signals_per_cycle=30,
        t2_updown_enabled=False,
        t2_updown_priority_boost=1.0,
        t2_updown_symbols="btc,eth",
        t2_updown_window_minutes="15",
        t2_updown_slots_ahead=4,
        t2_updown_max_spread_bps=500.0,
        t2_updown_spot_feed_enabled=False,
        t2_updown_spot_pairs="",
        portfolio_sync_enabled=False,
        portfolio_sync_interval_sec=60.0,
        portfolio_sync_timeout_sec=5.0,
        portfolio_sync_max_consecutive_failures=3,
        data_api_host="https://data-api.polymarket.com",
        portfolio_sync_user_address="",
        feishu_app_id="",
        feishu_app_secret="",
        feishu_open_id="",
        feishu_api_base="https://open.feishu.cn/open-apis",
        notification_cooldown_sec=30.0,
        notify_on_arb_found=False,
        notify_arb_found_in_shadow=False,
        notify_on_trade_success=True,
        notify_on_trade_failure=True,
        notify_on_fatal_error=True,
        notify_on_pnl_alert=True,
        notify_on_daily_summary=True,
        pnl_profit_alert_usdc=20.0,
        pnl_loss_alert_usdc=10.0,
        fatal_error_cooldown_sec=300.0,
        daily_summary_time_hhmm="08:05",
        daily_summary_timezone="Asia/Shanghai",
        notification_state_file="data/telemetry/notification_state.test.json",
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
        t2_min_deviation=0.02,
        t2_max_spread_bps=80.0,
        t2_min_top_depth=100.0,
        t2_max_complement_error_bps=150.0,
        t2_max_signals_per_market_per_hour=2,
        t2_stop_loss_bps=300.0,
        t2_take_profit_capture_pct=0.6,
        t2_max_hold_sec=21600.0,
        t2_exit_eval_interval_sec=30.0,
        t2_optimal_stopping_enabled=True,
        t2_scale_out_tranches=1,
        t2_stop_loss_dynamic_enabled=False,
        t2_stop_loss_dynamic_k=2.0,
        t2_stop_loss_min_bps=100.0,
        t2_stop_loss_max_bps=1000.0,
        t2_stop_loss_dynamic_warmup=5,
        t2_long_horizon_days=30.0,
        t2_long_horizon_min_net_edge_bps=200.0,
        t2_post_exit_cooldown_sec=0.0,
        t2_recent_exits_state_file="",
        t2_reject_price_below=0.10,
        t2_reject_price_above=0.90,
        t2_near_efficient_min_net_edge_bps=300.0,
        t2_near_certainty_shadow_mode=True,
        t2_near_certainty_high_threshold=0.92,
        t2_near_certainty_low_threshold=0.08,
        t2_near_certainty_size_multiplier=0.60,
        t2_near_certainty_confidence_delta=-0.08,
        t2_barbell_enabled=False,
        t2_barbell_tail_budget_pct=0.15,
        t2_barbell_tail_relaxed_multiplier=0.85,
        t3_flow_bias_enabled=False,
        t3_flow_bias_window_sec=3600.0,
        t3_flow_bias_min_trades=20,
        t3_flow_bias_strong_threshold=0.55,
        t3_flow_bias_inventory_weight=0.5,
        t3_flow_state_file="",
        shadow_maker_fill_latency_sec=2.0,
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
        ai_provider="openai",
        ai_api_key="",
        ai_api_base="",
        ai_model="gpt-4o",
        ai_temperature=0.1,
        research_signal_enabled=False,
        research_signal_window_sec=86400,
        research_signal_max_items=5,
        research_signal_cache_ttl_sec=300,
        research_signal_cache_dir="data/research_signal",
        research_signal_feeds_file="data/quant_inputs/research_feeds.json",
        research_signal_http_json_sources="",
        research_signal_crypto_macro_enabled=False,
        research_signal_coingecko_enabled=False,
        research_signal_funding_rate_enabled=False,
        research_signal_econ_calendar_enabled=False,
        research_signal_defillama_enabled=False,
        research_signal_polymarket_activity_enabled=False,
        research_signal_manifold_enabled=False,
        backtest_enabled=False,
        backtest_data_dir="data/backtest",
        backtest_default_dataset="default",
        backtest_slippage_bps=5.0,
        backtest_reports_dir="research/backtest/output",
        wallet_alpha_candidate_shadow_enabled=False,
        wallet_alpha_shadow_validation_enabled=True,
        wallet_alpha_shadow_max_signals_per_cycle=5,
        wallet_alpha_shadow_max_exec_ms_per_cycle=250.0,
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
                "T2_MIN_DEVIATION=0.02",
                "T2_MAX_SPREAD_BPS=80",
                "T2_MIN_TOP_DEPTH=100",
                "T2_MAX_COMPLEMENT_ERROR_BPS=150",
                "CROSS_PLATFORM_PAIRS_JSON=",
                "ARB_MAX_MULTI_OUTCOME_LEGS=20",
                "RISK_EVENT_COOLDOWN_SEC=60",
                "RISK_PENDING_RESERVATION_TTL_SEC=30",
                "RISK_HALT_AUTO_RECOVER_SEC=3600",
                "PORTFOLIO_SYNC_ENABLED=false",
                "PORTFOLIO_SYNC_INTERVAL_SEC=60",
                "PORTFOLIO_SYNC_TIMEOUT_SEC=5",
                "DATA_API_HOST=https://data-api.polymarket.com",
                "PORTFOLIO_SYNC_USER_ADDRESS=",
                "FEISHU_APP_ID=",
                "FEISHU_APP_SECRET=",
                "FEISHU_OPEN_ID=",
                "FEISHU_API_BASE=https://open.feishu.cn/open-apis",
                "NOTIFICATION_COOLDOWN_SEC=30",
                "NOTIFY_ON_ARB_FOUND=false",
                "NOTIFY_ON_TRADE_SUCCESS=true",
                "NOTIFY_ON_TRADE_FAILURE=true",
                "NOTIFY_ON_FATAL_ERROR=true",
                "NOTIFY_ON_PNL_ALERT=true",
                "NOTIFY_ON_DAILY_SUMMARY=true",
                "PNL_PROFIT_ALERT_USDC=20",
                "PNL_LOSS_ALERT_USDC=10",
                "FATAL_ERROR_COOLDOWN_SEC=300",
                "DAILY_SUMMARY_TIME_HHMM=08:05",
                "DAILY_SUMMARY_TIMEZONE=Asia/Shanghai",
                "NOTIFICATION_STATE_FILE=data/telemetry/notification_state.test.json",
                "BACKTEST_ENABLED=false",
                "BACKTEST_SLIPPAGE_BPS=5",
                "BACKTEST_REPORTS_DIR=research/backtest/output",
            ]
        ),
        encoding="utf-8",
    )
    return env_path
