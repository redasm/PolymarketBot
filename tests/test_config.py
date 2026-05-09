"""Config validation tests."""

from __future__ import annotations

import pytest

from polymarket_arb.config import ArbConfig
from tests.conftest import make_test_config, write_test_env


@pytest.mark.parametrize(
    ("field_name", "value", "expected_msg"),
    [
        ("market_fetch_limit", 0, "ARB_MARKET_FETCH_LIMIT"),
        ("clob_client_version", "bad", "POLYMARKET_CLOB_CLIENT_VERSION"),
        ("min_edge_usd", -0.01, "ARB_MIN_EDGE_USD"),
        ("min_edge_pct", -0.01, "ARB_MIN_EDGE_PCT"),
        ("max_order_size_usdc", 0.0, "ARB_MAX_ORDER_SIZE_USDC"),
        ("default_order_size_usdc", 0.0, "ARB_DEFAULT_ORDER_SIZE_USDC"),
        ("live_max_order_size_usdc", 0.0, "LIVE_MAX_ORDER_SIZE_USDC"),
        ("live_max_total_exposure_usdc", 0.0, "LIVE_MAX_TOTAL_EXPOSURE_USDC"),
        ("min_liquidity", -1.0, "ARB_MIN_LIQUIDITY"),
        ("min_volume_24h", -1.0, "ARB_MIN_LIQUIDITY"),
        ("polymarket_taker_fee_rate", 1.0, "POLYMARKET_TAKER_FEE_RATE"),
        ("kalshi_taker_fee_rate", -0.1, "KALSHI_TAKER_FEE_RATE"),
        ("scan_interval_sec", 0.0, "ARB_SCAN_INTERVAL_SEC"),
        ("orderbook_retry_count", -1, "ORDERBOOK_RETRY_COUNT"),
        ("orderbook_ws_snapshot_max_age_sec", -0.1, "ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC"),
        ("orderbook_retry_delay_sec", -0.1, "ORDERBOOK_RETRY_DELAY_SEC"),
        ("orderbook_missing_cooldown_sec", -1.0, "ORDERBOOK_MISSING_COOLDOWN_SEC"),
        ("t2_min_deviation", -0.1, "T2_MIN_DEVIATION"),
        ("t2_max_spread_bps", -1.0, "T2_MAX_SPREAD_BPS"),
        ("t2_min_top_depth", -1.0, "T2_MIN_TOP_DEPTH"),
        ("t2_max_complement_error_bps", -1.0, "T2_MAX_COMPLEMENT_ERROR_BPS"),
        ("max_multi_outcome_legs", 1, "ARB_MAX_MULTI_OUTCOME_LEGS"),
        ("risk_event_cooldown_sec", -1.0, "RISK_EVENT_COOLDOWN_SEC"),
        ("risk_pending_reservation_ttl_sec", -1.0, "RISK_PENDING_RESERVATION_TTL_SEC"),
        ("edge_min_confidence", 1.5, "EDGE_MIN_CONFIDENCE"),
        ("edge_confidence_full_bps", 0.0, "EDGE_CONFIDENCE_FULL_BPS"),
    ],
)
def test_config_validate_rejects_invalid_values(field_name, value, expected_msg):
    kwargs = {field_name: value}

    with pytest.raises(ValueError, match=expected_msg):
        make_test_config(**kwargs)


def test_config_validate_rejects_default_order_above_max_order():
    with pytest.raises(ValueError, match="ARB_DEFAULT_ORDER_SIZE_USDC"):
        make_test_config(default_order_size_usdc=60.0, max_order_size_usdc=50.0)


def test_deposit_wallet_requires_v2_client():
    with pytest.raises(ValueError, match="POLYMARKET_CLOB_CLIENT_VERSION"):
        make_test_config(signature_type=3, clob_client_version="v1")


def test_from_env_accepts_deposit_wallet_alias(tmp_path, monkeypatch):
    env_path = tmp_path / ".env.deposit-wallet"
    env_path.write_text(
        "\n".join(
            [
                "PRIVATE_KEY=0xabc",
                "POLYMARKET_DEPOSIT_WALLET=0xdeposit",
                "POLYMARKET_SIGNATURE_TYPE=3",
                "POLYMARKET_CLOB_CLIENT_VERSION=v2",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)

    cfg = ArbConfig.from_env(env_path)

    assert cfg.funder_address == "0xdeposit"
    assert cfg.signature_type == 3
    assert cfg.clob_client_version == "v2"


def test_from_env_loads_new_edge_and_cooldown_config(tmp_path, monkeypatch):
    env_path = write_test_env(tmp_path)
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        + "\nEDGE_CONFIDENCE_FULL_BPS=650\nEDGE_CONFIDENCE_IMBALANCE_WEIGHT=0.2\nORDERBOOK_MISSING_COOLDOWN_SEC=120\nORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC=15\nT2_MIN_DEVIATION=0.015\nT2_MAX_SPREAD_BPS=90\nT2_MIN_TOP_DEPTH=150\nT2_MAX_COMPLEMENT_ERROR_BPS=120\nCROSS_PLATFORM_PAIRS_JSON=[]\nRISK_PENDING_RESERVATION_TTL_SEC=300\nPORTFOLIO_SYNC_ENABLED=true\nPORTFOLIO_SYNC_INTERVAL_SEC=45\nPORTFOLIO_SYNC_TIMEOUT_SEC=4\nDATA_API_HOST=https://data-api.polymarket.com\nPORTFOLIO_SYNC_USER_ADDRESS=0xabc\n",
        encoding="utf-8",
    )
    for key in (
        "EDGE_CONFIDENCE_FULL_BPS",
        "EDGE_CONFIDENCE_IMBALANCE_WEIGHT",
        "ORDERBOOK_MISSING_COOLDOWN_SEC",
        "ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC",
        "T2_MIN_DEVIATION",
        "T2_MAX_SPREAD_BPS",
        "T2_MIN_TOP_DEPTH",
        "T2_MAX_COMPLEMENT_ERROR_BPS",
        "CROSS_PLATFORM_PAIRS_JSON",
        "RISK_PENDING_RESERVATION_TTL_SEC",
        "PORTFOLIO_SYNC_ENABLED",
        "PORTFOLIO_SYNC_INTERVAL_SEC",
        "PORTFOLIO_SYNC_TIMEOUT_SEC",
        "DATA_API_HOST",
        "PORTFOLIO_SYNC_USER_ADDRESS",
        "LIVE_TRADING_ACK",
        "LIVE_REQUIRE_PORTFOLIO_SYNC",
        "LIVE_ALLOW_ZERO_TAKER_FEE",
        "LIVE_MAX_ORDER_SIZE_USDC",
        "LIVE_MAX_TOTAL_EXPOSURE_USDC",
        "MAKER_STRATEGY_ENABLED",
        "POLYMARKET_CLOB_CLIENT_VERSION",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = ArbConfig.from_env(env_path)

    assert cfg.risk_event_cooldown_sec == 60.0
    assert cfg.orderbook_missing_cooldown_sec == 120.0
    assert cfg.orderbook_ws_snapshot_max_age_sec == 15.0
    assert cfg.t2_min_deviation == 0.015
    assert cfg.t2_max_spread_bps == 90.0
    assert cfg.t2_min_top_depth == 150.0
    assert cfg.t2_max_complement_error_bps == 120.0
    assert cfg.cross_platform_pairs_json == "[]"
    assert cfg.risk_pending_reservation_ttl_sec == 300.0
    assert cfg.portfolio_sync_enabled is True
    assert cfg.portfolio_sync_interval_sec == 45.0
    assert cfg.portfolio_sync_timeout_sec == 4.0
    assert cfg.data_api_host == "https://data-api.polymarket.com"
    assert cfg.portfolio_sync_user_address == "0xabc"
    assert cfg.live_trading_ack is False
    assert cfg.live_require_portfolio_sync is True
    assert cfg.live_allow_zero_taker_fee is False
    assert cfg.live_max_order_size_usdc == 10.0
    assert cfg.live_max_total_exposure_usdc == 100.0
    assert cfg.maker_strategy_enabled is True
    assert cfg.clob_client_version == "auto"
    assert cfg.edge_confidence_full_bps == 650.0
    assert cfg.edge_confidence_imbalance_weight == 0.2
    assert cfg.telemetry_record_enabled is False
    assert cfg.telemetry_record_dir == "data/telemetry"
    assert cfg.data_cleanup_enabled is True
    assert cfg.data_ticks_retention_days == 7
    assert cfg.data_research_cache_max_gb == 1.0


def test_from_env_requires_wallet_when_requested(tmp_path, monkeypatch):
    env_path = tmp_path / ".env.missing-wallet"
    env_path.write_text("ARB_DRY_RUN=true\n", encoding="utf-8")
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)

    with pytest.raises(ValueError, match="PRIVATE_KEY"):
        ArbConfig.from_env(env_path)


def test_live_mode_requires_explicit_ack():
    with pytest.raises(ValueError, match="LIVE_TRADING_ACK"):
        make_test_config(dry_run=False, live_trading_ack=False)


def test_live_mode_rejects_zero_taker_fee_without_override():
    with pytest.raises(ValueError, match="POLYMARKET_TAKER_FEE_RATE"):
        make_test_config(
            dry_run=False,
            live_trading_ack=True,
            portfolio_sync_enabled=True,
            polymarket_taker_fee_rate=0.0,
        )


def test_live_mode_allows_canary_limits_when_acknowledged():
    cfg = make_test_config(
        dry_run=False,
        live_trading_ack=True,
        portfolio_sync_enabled=True,
        polymarket_taker_fee_rate=0.072,
        default_order_size_usdc=1.0,
        max_order_size_usdc=1.5,
        max_total_exposure=3.0,
    )

    assert cfg.dry_run is False


def test_dump_safe_masks_funder_address():
    cfg = make_test_config(funder_address="0xEC2eDFa71610e8D0AF")

    dumped = cfg.dump_safe()

    assert dumped["funder_address"].startswith("0xEC2e")
    assert dumped["funder_address"].endswith("0AF")
    assert "***" in dumped["funder_address"]


def test_from_env_preserves_existing_environment_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env.precedence"
    env_path.write_text(
        "\n".join(
            [
                "PRIVATE_KEY=dotenv-key",
                "POLYMARKET_FUNDER=dotenv-funder",
                "ARB_SCAN_INTERVAL_SEC=7",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRIVATE_KEY", "env-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "env-funder")
    monkeypatch.setenv("ARB_SCAN_INTERVAL_SEC", "9")

    cfg = ArbConfig.from_env(env_path)

    assert cfg.private_key == "dotenv-key"
    assert cfg.funder_address == "dotenv-funder"
    assert cfg.scan_interval_sec == 7.0
