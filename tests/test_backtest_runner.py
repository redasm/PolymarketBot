"""Backtest subsystem tests."""

import json
from pathlib import Path
import sys

import pytest

from polymarket_arb.models import BacktestReport
from research.backtest.datasets import build_binary_snapshots_from_ticks
from research.backtest.features import summarize_binary_microstructure
from research.backtest.adapters.t2_adapter import build_t2_adapter
from research.backtest.reports.stability import build_parameter_stability_summary, build_stability_summary
from research.backtest.execution_model.base import (
    DepthVWAPExecutionModel,
    ExecutionModelConfig,
    QueueAwareExecutionModel,
    TopOfBookExecutionModel,
)
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
    assert report.fill_rate == 1.0
    assert report.total_fees_paid > 0
    assert report.avg_signal_edge_bps > 0
    assert (tmp_path / "out" / "recommended_params.json").exists()
    assert (tmp_path / "out" / "t0_structural_arbitrage_sample_trades.jsonl").exists()
    report_payload = json.loads((tmp_path / "out" / "t0_structural_arbitrage_sample_report.json").read_text(encoding="utf-8"))
    assert report_payload["strategy_name"] == "t0_structural_arbitrage"
    assert report_payload["fill_rate"] == 1.0
    assert report_payload["total_fees_paid"] > 0


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


def test_backtest_reader_accepts_utf8_bom_snapshot_file(tmp_path: Path):
    dataset_dir = tmp_path / "bom-sample"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.44,"yes_best_ask":0.45,'
            '"yes_bid_size":10,"yes_ask_size":10,"no_best_bid":0.49,"no_best_ask":0.50,'
            '"no_bid_size":10,"no_ask_size":10,"best_ask":0.45,"available_size":2}\n'
        ),
        encoding="utf-8-sig",
    )
    dotenv_path = write_test_env(tmp_path)

    runner = BacktestRunner(tmp_path)
    report = runner.run(
        strategy=_Strategy(),
        dataset="bom-sample",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="bom-sample",
            output_dir=str(tmp_path / "out-bom-sample"),
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


def test_queue_aware_execution_model_applies_queue_and_partial_fill():
    model = QueueAwareExecutionModel(
        ExecutionModelConfig(
            fee_rate=0.02,
            latency_ms=10,
            slippage_bps=5,
            queue_ahead_ratio=0.5,
            adverse_selection_bps=3,
            partial_fill_ratio=0.5,
        )
    )
    result = model.simulate(
        {"side": "BUY", "size": 10, "ask_levels": [(0.40, 8), (0.41, 6)]},
        {"ask_levels": [(0.40, 8), (0.41, 6)]},
    )

    assert result.filled is True
    assert result.filled_size < 10
    assert result.average_price > 0.40
    assert result.slippage_bps == 8
    assert "partial_fill" in result.notes


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


def test_binary_microstructure_feature_summary_computes_complement_error():
    features = summarize_binary_microstructure(
        {
            "yes_best_bid": 0.44,
            "yes_best_ask": 0.45,
            "yes_bid_size": 10,
            "yes_ask_size": 12,
            "no_best_bid": 0.54,
            "no_best_ask": 0.55,
            "no_bid_size": 9,
            "no_ask_size": 11,
        }
    )

    assert round(features["yes_mid"], 4) == 0.445
    assert round(features["sum_best_asks"], 4) == 1.0
    assert round(features["complement_error_bps"], 4) == 100.0


def test_backtest_runner_enforces_portfolio_limits(tmp_path: Path):
    dataset_dir = tmp_path / "portfolio"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"ts_ms":1000,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes1","no_token_id":"no1","yes_best_bid":0.44,"yes_best_ask":0.45,'
            '"yes_bid_size":10,"yes_ask_size":10,"no_best_bid":0.49,"no_best_ask":0.50,'
            '"no_bid_size":10,"no_ask_size":10,"best_ask":0.45,"available_size":2}\n'
            '{"ts_ms":1001,"condition_id":"c2","event_id":"e2","question":"Will ETH go up?",'
            '"yes_token_id":"yes2","no_token_id":"no2","yes_best_bid":0.44,"yes_best_ask":0.45,'
            '"yes_bid_size":10,"yes_ask_size":10,"no_best_bid":0.49,"no_best_ask":0.50,'
            '"no_bid_size":10,"no_ask_size":10,"best_ask":0.45,"available_size":2}\n'
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)

    runner = BacktestRunner(tmp_path)
    report = runner.run(
        strategy=_Strategy(),
        dataset="portfolio",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="portfolio",
            output_dir=str(tmp_path / "out-portfolio"),
            dotenv_path=str(dotenv_path),
            max_open_positions=1,
            holding_period_ms=60_000,
        ),
    )

    assert report.total_signals == 2
    assert report.total_trades == 1
    assert report.skipped_signals == 1


def test_build_binary_snapshots_from_ticks_creates_dataset(tmp_path: Path):
    ticks_path = tmp_path / "ticks.ndjson"
    ticks_path.write_text(
        "\n".join(
            [
                '{"ts_ms":1000,"token_id":"yes-token","best_bid":0.44,"best_ask":0.45,"bids_top3":[[0.44,10]],"asks_top3":[[0.45,12]]}',
                '{"ts_ms":1000,"token_id":"no-token","best_bid":0.54,"best_ask":0.55,"bids_top3":[[0.54,9]],"asks_top3":[[0.55,11]]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out_path = build_binary_snapshots_from_ticks(
        ticks_path,
        tmp_path / "dataset",
        condition_id="cond-1",
        event_id="event-1",
        question="Recovered question",
        slug="recovered-question",
        strict=False,
    )

    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "cond-1"
    assert rows[0]["yes_best_ask"] == 0.45
    assert rows[0]["no_best_ask"] == 0.55
    assert rows[0]["yes_ask_levels"][0] == [0.45, 12.0]
    assert rows[0]["no_ask_levels"][0] == [0.55, 11.0]


def test_build_binary_snapshots_from_ticks_uses_latest_known_state_across_timestamps(tmp_path: Path):
    ticks_path = tmp_path / "ticks-latest.ndjson"
    ticks_path.write_text(
        "\n".join(
            [
                '{"ts_ms":1000,"token_id":"yes-token","best_bid":0.44,"best_ask":0.45,"bids_top3":[[0.44,10]],"asks_top3":[[0.45,12]]}',
                '{"ts_ms":1001,"token_id":"no-token","best_bid":0.54,"best_ask":0.55,"bids_top3":[[0.54,9]],"asks_top3":[[0.55,11]]}',
                '{"ts_ms":1002,"token_id":"yes-token","best_bid":0.43,"best_ask":0.46,"bids_top3":[[0.43,8]],"asks_top3":[[0.46,10]]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out_path = build_binary_snapshots_from_ticks(
        ticks_path,
        tmp_path / "dataset-latest",
        condition_id="cond-latest",
        event_id="event-latest",
        question="Recovered latest-known",
        slug="recovered-latest-known",
        yes_token_id="yes-token",
        no_token_id="no-token",
    )

    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[0]["ts_ms"] == 1001
    assert rows[1]["ts_ms"] == 1002
    assert rows[1]["no_best_ask"] == 0.55


def test_build_binary_snapshots_from_ticks_preserves_explicit_metadata_over_row_values(tmp_path: Path):
    ticks_path = tmp_path / "ticks-explicit-metadata.ndjson"
    ticks_path.write_text(
        "\n".join(
            [
                '{"ts_ms":1000,"condition_id":"row-cond","event_id":"row-event","question":"Row question","slug":"row-slug","token_id":"yes-token","outcome_role":"yes","best_bid":0.44,"best_ask":0.45,"bids_top3":[[0.44,10]],"asks_top3":[[0.45,12]]}',
                '{"ts_ms":1000,"condition_id":"row-cond","event_id":"row-event","question":"Row question","slug":"row-slug","token_id":"no-token","outcome_role":"no","best_bid":0.54,"best_ask":0.55,"bids_top3":[[0.54,9]],"asks_top3":[[0.55,11]]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out_path = build_binary_snapshots_from_ticks(
        ticks_path,
        tmp_path / "dataset-explicit-metadata",
        condition_id="explicit-cond",
        event_id="explicit-event",
        question="Explicit question",
        slug="explicit-slug",
    )

    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "explicit-cond"
    assert rows[0]["event_id"] == "explicit-event"
    assert rows[0]["question"] == "Explicit question"
    assert rows[0]["slug"] == "explicit-slug"


def test_build_binary_snapshots_from_ticks_accepts_utf8_bom_input(tmp_path: Path):
    ticks_path = tmp_path / "ticks-bom.ndjson"
    ticks_path.write_text(
        "\n".join(
            [
                '{"ts_ms":1000,"token_id":"yes-token","outcome_role":"yes","best_bid":0.44,"best_ask":0.45,"bids_top3":[[0.44,10]],"asks_top3":[[0.45,12]]}',
                '{"ts_ms":1000,"token_id":"no-token","outcome_role":"no","best_bid":0.54,"best_ask":0.55,"bids_top3":[[0.54,9]],"asks_top3":[[0.55,11]]}',
            ]
        )
        + "\n",
        encoding="utf-8-sig",
    )

    out_path = build_binary_snapshots_from_ticks(
        ticks_path,
        tmp_path / "dataset-bom",
        strict=False,
    )

    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["yes_best_ask"] == 0.45


def test_build_binary_snapshots_from_ticks_strict_mode_rejects_unlabeled_tokens(tmp_path: Path):
    ticks_path = tmp_path / "ticks-strict.ndjson"
    ticks_path.write_text(
        "\n".join(
            [
                '{"ts_ms":1000,"token_id":"token-a","best_bid":0.44,"best_ask":0.45,"bids_top3":[[0.44,10]],"asks_top3":[[0.45,12]]}',
                '{"ts_ms":1001,"token_id":"token-b","best_bid":0.54,"best_ask":0.55,"bids_top3":[[0.54,9]],"asks_top3":[[0.55,11]]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    raised = False
    try:
        build_binary_snapshots_from_ticks(
            ticks_path,
            tmp_path / "dataset-strict",
            strict=True,
        )
    except ValueError as exc:
        raised = True
        assert "outcome_role" in str(exc)

    assert raised is True


def test_build_binary_snapshots_from_ticks_rejects_multi_condition_without_filter(tmp_path: Path):
    ticks_path = tmp_path / "ticks-multi-condition.ndjson"
    ticks_path.write_text(
        "\n".join(
            [
                '{"ts_ms":1000,"condition_id":"cond-1","token_id":"yes-token","outcome_role":"yes","best_bid":0.44,"best_ask":0.45,"bids_top3":[[0.44,10]],"asks_top3":[[0.45,12]]}',
                '{"ts_ms":1000,"condition_id":"cond-2","token_id":"no-token","outcome_role":"no","best_bid":0.54,"best_ask":0.55,"bids_top3":[[0.54,9]],"asks_top3":[[0.55,11]]}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    raised = False
    try:
        build_binary_snapshots_from_ticks(
            ticks_path,
            tmp_path / "dataset-multi-condition",
        )
    except ValueError as exc:
        raised = True
        assert "condition_id_filter" in str(exc)

    assert raised is True


def _make_cli_dataset(tmp_path: Path) -> Path:
    dataset_dir = tmp_path / "backtest-data" / "default-dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "market_snapshots.jsonl").write_text("", encoding="utf-8")
    return tmp_path / "backtest-data"


def test_backtest_cli_rejects_missing_dataset(tmp_path: Path, monkeypatch):
    from research.backtest import run as backtest_run

    monkeypatch.setattr(
        backtest_run.ArbConfig,
        "from_env",
        lambda dotenv_path=None, require_wallet=False: type(
            "Cfg",
            (),
            {
                "backtest_default_dataset": "default",
                "backtest_reports_dir": str(tmp_path / "out"),
                "backtest_data_dir": str(tmp_path / "empty"),
                "polymarket_taker_fee_rate": 0.02,
                "backtest_slippage_bps": 5.0,
            },
        )(),
    )
    monkeypatch.setattr(sys, "argv", ["run.py"])

    with pytest.raises(SystemExit) as exc:
        backtest_run.main()

    assert exc.value.code == 2


def test_backtest_cli_prefers_explicit_output_dir(tmp_path: Path, monkeypatch):
    from research.backtest import run as backtest_run

    captured = {}
    expected_output_dir = str(tmp_path / "cli-output")

    class _StubRunner:
        def __init__(self, data_dir):
            captured["data_dir"] = data_dir

        def run(self, strategy, dataset, execution_model, config):
            captured["output_dir"] = config.output_dir
            captured["dataset"] = dataset
            return BacktestReport(strategy_name=strategy.strategy_name, dataset_name=dataset)

    monkeypatch.setattr(
        backtest_run,
        "BacktestRunner",
        _StubRunner,
    )
    monkeypatch.setattr(
        backtest_run.ArbConfig,
        "from_env",
        lambda dotenv_path=None, require_wallet=False: type(
            "Cfg",
            (),
            {
                "backtest_default_dataset": "default-dataset",
                "backtest_reports_dir": "config-output-dir",
                "backtest_data_dir": str(_make_cli_dataset(tmp_path)),
                "polymarket_taker_fee_rate": 0.02,
                "backtest_slippage_bps": 5.0,
            },
        )(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--output-dir", expected_output_dir],
    )

    backtest_run.main()

    assert captured["output_dir"] == expected_output_dir


def test_backtest_cli_maps_quant_strategy_choice(tmp_path: Path, monkeypatch):
    from research.backtest import run as backtest_run

    captured = {}

    class _StubRunner:
        def __init__(self, data_dir):
            captured["data_dir"] = data_dir

        def run(self, strategy, dataset, execution_model, config):
            captured["strategy_name"] = strategy.strategy_name
            return BacktestReport(strategy_name=strategy.strategy_name, dataset_name=dataset)

    monkeypatch.setattr(backtest_run, "BacktestRunner", _StubRunner)
    monkeypatch.setattr(
        backtest_run.ArbConfig,
        "from_env",
        lambda dotenv_path=None, require_wallet=False: type(
            "Cfg",
            (),
            {
                "backtest_default_dataset": "default-dataset",
                "backtest_reports_dir": "config-output-dir",
                "backtest_data_dir": str(_make_cli_dataset(tmp_path)),
                "polymarket_taker_fee_rate": 0.02,
                "backtest_slippage_bps": 5.0,
            },
        )(),
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--strategy", "event-calendar"])

    backtest_run.main()

    assert captured["strategy_name"] == "event_calendar"


def test_backtest_runner_can_run_t2_markout_strategy(tmp_path: Path):
    dataset_dir = tmp_path / "t2sample"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"ts_ms":1000,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.40,"yes_best_ask":0.41,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.59,"no_best_ask":0.60,'
            '"no_bid_size":120,"no_ask_size":500,"best_ask":0.41,"available_size":120}\n'
            '{"ts_ms":400000,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.44,"yes_best_ask":0.45,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.55,"no_best_ask":0.56,'
            '"no_bid_size":120,"no_ask_size":500,"best_ask":0.45,"available_size":120}\n'
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)

    runner = BacktestRunner(tmp_path)
    strategy = type("BacktestStrategy", (), {"strategy_name": "t2_statistical_arbitrage"})()
    report = runner.run(
        strategy=strategy,
        dataset="t2sample",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="t2sample",
            output_dir=str(tmp_path / "out-t2"),
            dotenv_path=str(dotenv_path),
            holding_period_ms=60_000,
        ),
    )

    assert report.strategy_name == "t2_statistical_arbitrage"
    assert report.total_signals >= 1
    assert report.total_trades >= 1
    assert report.filled_trades >= 1


def test_backtest_runner_can_run_event_calendar_strategy(tmp_path: Path):
    dataset_dir = tmp_path / "event-calendar"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"ts_ms":1000,"condition_id":"event","event_id":"e1","question":"Will CPI beat?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.40,"yes_best_ask":0.42,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.58,"no_best_ask":0.60,'
            '"no_bid_size":120,"no_ask_size":500,"baseline_probability":0.50,'
            '"confidence":0.80,"time_to_event_sec":1800}\n'
            '{"ts_ms":400000,"condition_id":"event","event_id":"e1","question":"Will CPI beat?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.46,"yes_best_ask":0.48,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.52,"no_best_ask":0.54,'
            '"no_bid_size":120,"no_ask_size":500,"baseline_probability":0.50,'
            '"confidence":0.80,"time_to_event_sec":1200}\n'
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)

    runner = BacktestRunner(tmp_path)
    strategy = type("BacktestStrategy", (), {"strategy_name": "event_calendar"})()
    report = runner.run(
        strategy=strategy,
        dataset="event-calendar",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="event-calendar",
            output_dir=str(tmp_path / "out-event-calendar"),
            dotenv_path=str(dotenv_path),
            holding_period_ms=60_000,
        ),
    )

    assert report.strategy_name == "event_calendar"
    assert report.total_signals >= 1
    assert report.filled_trades >= 1


def test_backtest_runner_can_run_wallet_alpha_strategy(tmp_path: Path):
    dataset_dir = tmp_path / "wallet-alpha"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"ts_ms":1000,"condition_id":"market","event_id":"e1","question":"Will macro event happen?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.46,"yes_best_ask":0.48,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.52,"no_best_ask":0.54,'
            '"no_bid_size":120,"no_ask_size":500,"wallet_address":"0xgood",'
            '"category":"macro","action":"BUY_YES"}\n'
            '{"ts_ms":400000,"condition_id":"market","event_id":"e1","question":"Will macro event happen?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.52,"yes_best_ask":0.54,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.46,"no_best_ask":0.48,'
            '"no_bid_size":120,"no_ask_size":500}\n'
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)
    env_text = dotenv_path.read_text(encoding="utf-8")
    dotenv_path.write_text(
        env_text
        + '\nWALLET_ALPHA_PROFILES_JSON={"0xgood":{"trade_count":40,"realized_roi":0.12,'
        + '"lagged_follow_roi":0.07,"max_drawdown":0.12,"concentration_score":0.2,'
        + '"category_edges":{"macro":0.08}}}\n',
        encoding="utf-8",
    )

    runner = BacktestRunner(tmp_path)
    strategy = type("BacktestStrategy", (), {"strategy_name": "wallet_alpha"})()
    report = runner.run(
        strategy=strategy,
        dataset="wallet-alpha",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="wallet-alpha",
            output_dir=str(tmp_path / "out-wallet-alpha"),
            dotenv_path=str(dotenv_path),
            holding_period_ms=60_000,
        ),
    )

    assert report.strategy_name == "wallet_alpha"
    assert report.total_signals == 1
    assert report.filled_trades == 1


def test_backtest_runner_can_run_logical_constraint_strategy(tmp_path: Path):
    dataset_dir = tmp_path / "logical-constraint"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"ts_ms":1000,"condition_id":"candidate","event_id":"e1","question":"Will candidate win?",'
            '"yes_token_id":"candidate-yes","no_token_id":"candidate-no","yes_best_bid":0.62,"yes_best_ask":0.64,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.36,"no_best_ask":0.38,'
            '"no_bid_size":120,"no_ask_size":500}\n'
            '{"ts_ms":1000,"condition_id":"party","event_id":"e1","question":"Will party win?",'
            '"yes_token_id":"party-yes","no_token_id":"party-no","yes_best_bid":0.54,"yes_best_ask":0.56,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.44,"no_best_ask":0.46,'
            '"no_bid_size":120,"no_ask_size":500}\n'
            '{"ts_ms":400000,"condition_id":"party","event_id":"e1","question":"Will party win?",'
            '"yes_token_id":"party-yes","no_token_id":"party-no","yes_best_bid":0.58,"yes_best_ask":0.60,'
            '"yes_bid_size":500,"yes_ask_size":120,"no_best_bid":0.40,"no_best_ask":0.42,'
            '"no_bid_size":120,"no_ask_size":500}\n'
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)
    dotenv_path.write_text(
        dotenv_path.read_text(encoding="utf-8")
        + '\nLOGICAL_CONSTRAINTS_JSON=[{"subject_market_id":"candidate","bound_market_id":"party",'
        + '"relation_type":"subject_lte_bound","min_violation_bps":200}]\n',
        encoding="utf-8",
    )

    runner = BacktestRunner(tmp_path)
    strategy = type("BacktestStrategy", (), {"strategy_name": "logical_constraint"})()
    report = runner.run(
        strategy=strategy,
        dataset="logical-constraint",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="logical-constraint",
            output_dir=str(tmp_path / "out-logical-constraint"),
            dotenv_path=str(dotenv_path),
            holding_period_ms=60_000,
        ),
    )

    assert report.strategy_name == "logical_constraint"
    assert report.total_signals == 1
    assert report.filled_trades == 1


def test_backtest_runner_marks_profit_factor_infinite_when_no_losses(tmp_path: Path):
    dataset_dir = tmp_path / "t2inf"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "market_snapshots.jsonl").write_text(
        (
            '{"ts_ms":1000,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.40,"yes_best_ask":0.41,'
            '"yes_bid_size":500,"yes_ask_size":120,"yes_ask_levels":[[0.41,120]],'
            '"no_best_bid":0.59,"no_best_ask":0.60,"no_bid_size":120,"no_ask_size":500,"no_ask_levels":[[0.60,500]],'
            '"best_ask":0.41,"available_size":120}\n'
            '{"ts_ms":400000,"condition_id":"c1","event_id":"e1","question":"Will BTC go up?",'
            '"yes_token_id":"yes","no_token_id":"no","yes_best_bid":0.50,"yes_best_ask":0.51,'
            '"yes_bid_size":500,"yes_ask_size":120,"yes_ask_levels":[[0.51,120]],'
            '"no_best_bid":0.49,"no_best_ask":0.50,"no_bid_size":120,"no_ask_size":500,"no_ask_levels":[[0.50,500]],'
            '"best_ask":0.51,"available_size":120}\n'
        ),
        encoding="utf-8",
    )
    dotenv_path = write_test_env(tmp_path)

    runner = BacktestRunner(tmp_path)
    strategy = type("BacktestStrategy", (), {"strategy_name": "t2_statistical_arbitrage"})()
    report = runner.run(
        strategy=strategy,
        dataset="t2inf",
        execution_model=TopOfBookExecutionModel(),
        config=BacktestRunConfig(
            dataset_name="t2inf",
            output_dir=str(tmp_path / "out-t2inf"),
            dotenv_path=str(dotenv_path),
            holding_period_ms=600_000,
            max_open_positions=1,
        ),
    )

    assert report.filled_trades >= 1
    assert report.profit_factor is None
    assert report.profit_factor_infinite is True


def test_t2_adapter_quality_filters_can_reject_wide_spread_rows():
    adapter = build_t2_adapter(
        min_deviation=0.005,
        min_confidence=0.1,
        max_spread_bps=100,
        min_top_depth=50,
        max_complement_error_bps=200,
    )
    row = {
        "condition_id": "c1",
        "yes_best_bid": 0.30,
        "yes_best_ask": 0.40,
        "yes_bid_size": 500,
        "yes_ask_size": 120,
        "no_best_bid": 0.59,
        "no_best_ask": 0.60,
        "no_bid_size": 120,
        "no_ask_size": 500,
    }

    signal = adapter.detect(row, order_size_usdc=10.0)

    assert signal is None


def test_stability_summary_aggregates_best_scan_rows():
    summary = build_stability_summary(
        [
            {
                "dataset_name": "d1",
                "strategy_name": "t2_statistical_arbitrage",
                "net_pnl": 0.05,
                "profit_factor": 1.2,
                "win_rate": 0.8,
                "fill_rate": 1.0,
                "avg_signal_edge_bps": 120.0,
                "execution_model": "queue",
                "total_signals": 10,
            },
            {
                "dataset_name": "d2",
                "strategy_name": "t2_statistical_arbitrage",
                "net_pnl": -0.02,
                "profit_factor": 0.7,
                "win_rate": 0.6,
                "fill_rate": 0.9,
                "avg_signal_edge_bps": 100.0,
                "execution_model": "queue",
                "total_signals": 5,
            },
        ]
    )

    assert summary["dataset_count"] == 2
    assert summary["positive_dataset_count"] == 1
    assert summary["execution_models"]["queue"] == 2
    assert summary["datasets"][0]["dataset_name"] == "d1"


def test_stability_summary_tracks_infinite_profit_factor():
    summary = build_stability_summary(
        [
            {
                "dataset_name": "d1",
                "strategy_name": "t2_statistical_arbitrage",
                "net_pnl": 0.1,
                "profit_factor": None,
                "profit_factor_infinite": True,
                "win_rate": 1.0,
                "fill_rate": 1.0,
                "avg_signal_edge_bps": 100.0,
                "execution_model": "queue",
                "total_signals": 4,
            }
        ]
    )

    assert summary["avg_profit_factor"] is None
    assert summary["infinite_profit_factor_count"] == 1


def test_parameter_stability_summary_ranks_parameter_sets():
    summary = build_parameter_stability_summary(
        [
            {
                "strategy_name": "t2_statistical_arbitrage",
                "dataset_name": "d1",
                "execution_model": "queue",
                "scan_slippage_bps": 5,
                "scan_latency_ms": 25,
                "scan_fee_rate": 0.02,
                "scan_queue_ahead_ratio": 0.1,
                "scan_adverse_selection_bps": 1.0,
                "scan_partial_fill_ratio": 0.75,
                "scan_min_deviation": 0.005,
                "scan_min_confidence": 0.1,
                "scan_holding_period_ms": 300000,
                "net_pnl": 0.6,
                "profit_factor": 2.0,
                "profit_factor_infinite": False,
                "win_rate": 0.95,
                "fill_rate": 1.0,
                "avg_signal_edge_bps": 160.0,
            },
            {
                "strategy_name": "t2_statistical_arbitrage",
                "dataset_name": "d2",
                "execution_model": "queue",
                "scan_slippage_bps": 5,
                "scan_latency_ms": 25,
                "scan_fee_rate": 0.02,
                "scan_queue_ahead_ratio": 0.1,
                "scan_adverse_selection_bps": 1.0,
                "scan_partial_fill_ratio": 0.75,
                "scan_min_deviation": 0.005,
                "scan_min_confidence": 0.1,
                "scan_holding_period_ms": 300000,
                "net_pnl": 0.3,
                "profit_factor": None,
                "profit_factor_infinite": True,
                "win_rate": 1.0,
                "fill_rate": 1.0,
                "avg_signal_edge_bps": 120.0,
            },
        ]
    )

    assert summary["parameter_set_count"] == 1
    assert summary["ranked_parameter_sets"][0]["positive_rate"] == 1.0
