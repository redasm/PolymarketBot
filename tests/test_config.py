"""Config validation tests."""

from __future__ import annotations

import pytest

from polymarket_arb.config import ArbConfig
from tests.conftest import make_test_config, write_test_env


@pytest.mark.parametrize(
    ("field_name", "value", "expected_msg"),
    [
        ("market_fetch_limit", 0, "ARB_MARKET_FETCH_LIMIT"),
        ("scan_interval_sec", 0.0, "ARB_SCAN_INTERVAL_SEC"),
        ("orderbook_retry_count", -1, "ORDERBOOK_RETRY_COUNT"),
        ("orderbook_retry_delay_sec", -0.1, "ORDERBOOK_RETRY_DELAY_SEC"),
        ("max_multi_outcome_legs", 1, "ARB_MAX_MULTI_OUTCOME_LEGS"),
        ("risk_event_cooldown_sec", -1.0, "RISK_EVENT_COOLDOWN_SEC"),
        ("edge_min_confidence", 1.5, "EDGE_MIN_CONFIDENCE"),
        ("edge_confidence_full_bps", 0.0, "EDGE_CONFIDENCE_FULL_BPS"),
    ],
)
def test_config_validate_rejects_invalid_values(field_name, value, expected_msg):
    kwargs = {field_name: value}

    with pytest.raises(ValueError, match=expected_msg):
        make_test_config(**kwargs)


def test_from_env_loads_new_edge_and_cooldown_config(tmp_path):
    env_path = write_test_env(tmp_path)
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        + "\nEDGE_CONFIDENCE_FULL_BPS=650\nEDGE_CONFIDENCE_IMBALANCE_WEIGHT=0.2\n",
        encoding="utf-8",
    )

    cfg = ArbConfig.from_env(env_path)

    assert cfg.risk_event_cooldown_sec == 60.0
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


def test_dump_safe_masks_funder_address():
    cfg = make_test_config(funder_address="0xEC2eDFa71610e8D0AF")

    dumped = cfg.dump_safe()

    assert dumped["funder_address"].startswith("0xEC2e")
    assert dumped["funder_address"].endswith("0AF")
    assert "***" in dumped["funder_address"]
