from pathlib import Path

from polymarket_arb.runtime_analysis import summarize_runtime_artifacts


def test_summarize_runtime_artifacts_detects_simulated_success_and_expected_profit(tmp_path: Path):
    log_path = tmp_path / "arb_bot.log"
    telemetry_dir = tmp_path / "telemetry"
    ticks_dir = tmp_path / "ticks"
    telemetry_dir.mkdir()
    ticks_dir.mkdir()

    log_path.write_text(
        "\n".join(
            [
                "2026-04-15 00:51:46 [INFO   ] main_loop                | 模式: DRY RUN (仅扫描)",
                "2026-04-15 00:56:01 [INFO   ] main_loop                | 周期 #1: 发现 1 个套利机会",
            ]
        ),
        encoding="utf-8",
    )
    (telemetry_dir / "2026-04-14.opportunities.ndjson").write_text(
        (
            '{"stage":"detected","event_id":"57225","event_title":"Vermont Governor Election Winner",'
            '"arb_type":"multi_outcome","net_edge":0.031,"max_executable_size":21.05}\n'
            '{"stage":"verified","event_id":"57225","event_title":"Vermont Governor Election Winner",'
            '"arb_type":"multi_outcome","net_edge":0.031,"max_executable_size":5.26}\n'
        ),
        encoding="utf-8",
    )
    (telemetry_dir / "2026-04-14.trades.ndjson").write_text(
        (
            '{"event_id":"57225","event_title":"Vermont Governor Election Winner","arb_success":true,'
            '"live_execution_success":false,"simulated":true,"requested_size":5.0,'
            '"expected_net_edge":0.031,"trade_outcome_estimate":0.155}\n'
        ),
        encoding="utf-8",
    )
    (telemetry_dir / "2026-04-14.strategy_signals.ndjson").write_text(
        (
            '{"tier":"STATISTICAL_ARB","signal_type":"statistical_buy_yes"}\n'
            '{"tier":"MARKET_MAKING","signal_type":"maker_quote"}\n'
        ),
        encoding="utf-8",
    )
    (ticks_dir / "2026-04-14.ndjson").write_text(
        (
            '{"event_id":"57225","condition_id":"cond-1","token_id":"token-1"}\n'
            '{"event_id":"57225","condition_id":"cond-2","token_id":"token-2"}\n'
        ),
        encoding="utf-8",
    )

    summary = summarize_runtime_artifacts(log_path=log_path, telemetry_dir=telemetry_dir, ticks_dir=ticks_dir)

    assert summary["run_mode"] == "dry_run"
    assert summary["arbitrage_available"] is True
    assert summary["opportunities"]["detected"] == 1
    assert summary["opportunities"]["verified"] == 1
    assert summary["trades"]["simulated_successes"] == 1
    assert summary["trades"]["live_successes"] == 0
    assert summary["trades"]["expected_profit_total"] == 0.155
    assert summary["signals"]["by_tier"] == {"MARKET_MAKING": 1, "STATISTICAL_ARB": 1}
    assert summary["ticks"]["records"] == 2
    assert any("dry run" in issue.lower() for issue in summary["issues"])


def test_summarize_runtime_artifacts_recovers_legacy_dry_run_trade_rows(tmp_path: Path):
    log_path = tmp_path / "arb_bot.log"
    telemetry_dir = tmp_path / "telemetry"
    ticks_dir = tmp_path / "ticks"
    telemetry_dir.mkdir()
    ticks_dir.mkdir()

    log_path.write_text(
        "2026-04-15 00:51:46 [INFO   ] main_loop                | 模式: DRY RUN (仅扫描)",
        encoding="utf-8",
    )
    (telemetry_dir / "2026-04-14.trades.ndjson").write_text(
        (
            '{"event_id":"57225","event_title":"Vermont Governor Election Winner","arb_success":false,'
            '"requested_size":5.0,"expected_net_edge":0.031,"trade_outcome_estimate":-5.0,'
            '"trades":[{"status":"filled","order_id":null},{"status":"filled","order_id":null}]}\n'
        ),
        encoding="utf-8",
    )

    summary = summarize_runtime_artifacts(log_path=log_path, telemetry_dir=telemetry_dir, ticks_dir=ticks_dir)

    assert summary["trades"]["reported_successes"] == 1
    assert summary["trades"]["simulated_successes"] == 1
    assert summary["trades"]["expected_profit_total"] == 0.155


def test_summarize_runtime_artifacts_reads_rotated_telemetry_files(tmp_path: Path):
    log_path = tmp_path / "arb_bot.log"
    telemetry_dir = tmp_path / "telemetry"
    ticks_dir = tmp_path / "ticks"
    telemetry_dir.mkdir()
    ticks_dir.mkdir()

    log_path.write_text("2026-04-15 [INFO] main_loop | 模式: LIVE (实盘交易)", encoding="utf-8")
    (telemetry_dir / "2026-04-14.strategy_signals.ndjson").write_text(
        '{"tier":"MARKET_MAKING","signal_type":"maker_quote"}\n',
        encoding="utf-8",
    )
    (telemetry_dir / "2026-04-14.strategy_signals.1.ndjson").write_text(
        '{"tier":"STATISTICAL_ARB","signal_type":"statistical_buy_no"}\n',
        encoding="utf-8",
    )
    (telemetry_dir / "2026-04-14.trades.1.ndjson").write_text(
        '{"arb_success":true,"live_execution_success":true,"trade_outcome_estimate":0.01}\n',
        encoding="utf-8",
    )

    summary = summarize_runtime_artifacts(log_path=log_path, telemetry_dir=telemetry_dir, ticks_dir=ticks_dir)

    assert summary["signals"]["by_tier"] == {"MARKET_MAKING": 1, "STATISTICAL_ARB": 1}
    assert summary["trades"]["live_successes"] == 1
    assert summary["trades"]["expected_profit_total"] == 0.01
