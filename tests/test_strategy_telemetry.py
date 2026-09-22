from __future__ import annotations

from polymarket_arb.main_helpers.strategy_telemetry import build_run_config_event

from tests.conftest import make_test_config


def test_build_run_config_event_redacts_sensitive_config() -> None:
    cfg = make_test_config(
        private_key="secret-private-key",
        clob_api_key="secret-api-key",
        clob_api_secret="secret-api-secret",
        clob_api_passphrase="secret-passphrase",
        feishu_app_id="secret-feishu-app",
        feishu_app_secret="secret-feishu",
        feishu_open_id="secret-feishu-open",
        max_total_exposure=500.0,
    )

    event = build_run_config_event(cfg, run_id="run-test")

    assert event["event"] == "run_config"
    assert event["run_id"] == "run-test"
    assert event["config"]["max_total_exposure"] == 500.0
    assert "config_hash" in event
    dumped = str(event)
    assert "secret-private-key" not in dumped
    assert "secret-api-key" not in dumped
    assert "secret-api-secret" not in dumped
    assert "secret-passphrase" not in dumped
    assert "secret-feishu-app" not in dumped
    assert "secret-feishu" not in dumped
    assert "secret-feishu-open" not in dumped


def test_structured_skip_reason_parses_event_cooldown() -> None:
    from polymarket_arb.main_helpers.strategy_telemetry import structured_skip_reason

    parsed = structured_skip_reason("事件 16167 180秒内已执行过套利")
    assert parsed["reason_code"] == "event_cooldown"
    assert parsed["reason_context"]["event_id"] == "16167"
    assert parsed["reason_context"]["cooldown_sec"] == 180
