"""Backtest subsystem tests."""

import json
from pathlib import Path

from polymarket_arb.models import BacktestReport
from research.backtest.execution_model.base import DepthVWAPExecutionModel, ExecutionModelConfig, TopOfBookExecutionModel
from research.backtest.replay.runner import BacktestRunConfig, BacktestRunner
from tests.conftest import write_test_env


class _Strategy:
    strategy_name = "t0_structural_arbitrage"


def test_top_of_book_execution_model_uses_available_size():
    model = TopOfBookExecutionModel(ExecutionModelConfig(fee_rate=0.02, latency_ms=10))
    result = model.simulate(
        {"side": "BUY", "size": 10},
        {"best_ask": 0.42, "available_size": 4},
    )

    assert result.filled is True
    assert result.filled_size == 4
    assert result.average_price == 0.42


def test_backtest_runner_generates_report_and_params(tmp_path: Path):
    dataset_dir = tmp_path / "sample"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.44,"yes_best_ask":0.45,'
            '"yes_bid_size":10,"yes_ask_size":10,"no_best_bid":0.49,"no_best_ask":0.50,'
            '"no_bid_size":10,"no_ask_size":10,"best_ask":0.45,"available_size":2}\n'
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)

    runner = BacktestRunner(tmp_path)
    report = runner.run(
        strategy=_Strategy(),
        dataset="sample",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="sample",
            output_dir=str(tmp_path / "out"),
            dotenv_path=str(dotenv_path),
        ),
    )

    assert isinstance(report, BacktestReport)
    assert report.total_trades == 1
    assert report.total_signals == 1
    assert (tmp_path / "out" / "recommended_params.json").exists()
    assert (tmp_path / "out" / "t0_structural_arbitrage_sample_trades.jsonl").exists()
    report_payload = json.loads((tmp_path / "out" / "t0_structural_arbitrage_sample_report.json").read_text(encoding="utf-8"))
    assert report_payload["strategy_name"] == "t0_structural_arbitrage"


def test_backtest_reader_can_build_states_from_orderbook_events(tmp_path: Path):
    dataset_dir = tmp_path / "eventsample"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "orderbook_events.jsonl").write_text(
        "\n".join(
            [
                '{"ts_ms":1000,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?","token_role":"yes","side":"buy","price":0.44,"size":10}',
                '{"ts_ms":1001,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?","token_role":"yes","side":"sell","price":0.45,"size":10}',
                '{"ts_ms":1002,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?","token_role":"no","side":"buy","price":0.49,"size":10}',
                '{"ts_ms":1003,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?","token_role":"no","side":"sell","price":0.50,"size":10}',
            ]
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)

    runner = BacktestRunner(tmp_path)
    report = runner.run(
        strategy=_Strategy(),
        dataset="eventsample",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="eventsample",
            output_dir=str(tmp_path / "out2"),
            dotenv_path=str(dotenv_path),
        ),
    )

    assert report.total_signals == 1
    assert report.total_trades == 1


def test_backtest_runner_can_run_without_wallet_env(tmp_path: Path):
    dataset_dir = tmp_path / "walletless"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.44,"yes_best_ask":0.45,'
            '"yes_bid_size":10,"yes_ask_size":10,"no_best_bid":0.49,"no_best_ask":0.50,'
            '"no_bid_size":10,"no_ask_size":10,"best_ask":0.45,"available_size":2}\n'
        ),
        encoding="utf-8",
    )
    env_path = tmp_path / ".env.walletless"
    env_path.write_text(
        "\n".join(
            [
                "ARB_DRY_RUN=true",
                "BACKTEST_ENABLED=true",
                "BACKTEST_DATA_DIR=data/backtest",
                "BACKTEST_SLIPPAGE_BPS=5",
                "BACKTEST_REPORTS_DIR=research/backtest/output",
            ]
        ),
        encoding="utf-8",
    )

    runner = BacktestRunner(tmp_path)
    report = runner.run(
        strategy=_Strategy(),
        dataset="walletless",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="walletless",
            output_dir=str(tmp_path / "out-walletless"),
            dotenv_path=str(env_path),
        ),
    )

    assert isinstance(report, BacktestReport)
    assert report.total_trades == 1


def test_depth_vwap_execution_model_applies_slippage():
    model = DepthVWAPExecutionModel(ExecutionModelConfig(fee_rate=0.02, latency_ms=10, slippage_bps=10))
    result = model.simulate(
        {"side": "BUY", "size": 6, "ask_levels": [(0.40, 2), (0.42, 4)]},
        {"ask_levels": [(0.40, 2), (0.42, 4)]},
    )

    assert result.filled is True
    assert result.filled_size == 6
    assert result.average_price > ((0.40 * 2 + 0.42 * 4) / 6)
    assert result.slippage_bps == 10


def test_parameter_scan_results_can_be_saved(tmp_path: Path):
    from research.backtest.reports.reporting import save_best_scan_result, save_scan_results

    path = save_scan_results(
        [{"strategy_name": "t0_structural_arbitrage", "scan_slippage_bps": 5.0, "scan_latency_ms": 25}],
        tmp_path,
        scan_name="sample_scan",
    )
    best_path = save_best_scan_result(
        {"strategy_name": "t0_structural_arbitrage", "scan_slippage_bps": 5.0, "scan_latency_ms": 25},
        tmp_path,
        filename="sample_best.json",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[0]["scan_slippage_bps"] == 5.0
    best_payload = json.loads(best_path.read_text(encoding="utf-8"))
    assert best_payload["scan_latency_ms"] == 25
