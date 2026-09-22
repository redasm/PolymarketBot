"""CLI entrypoint for the minimal backtest runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.config import ArbConfig
from research.backtest.execution_model.base import (
    DepthVWAPExecutionModel,
    ExecutionModelConfig,
    QueueAwareExecutionModel,
    TopOfBookExecutionModel,
)
from research.backtest.replay.runner import BacktestRunConfig, BacktestRunner
from research.backtest.reports.reporting import save_best_scan_result, save_scan_results

STRATEGY_NAME_MAP = {
    "t0": "t0_structural_arbitrage",
    "t2": "t2_statistical_arbitrage",
    "logical-constraint": "logical_constraint",
    "event-calendar": "event_calendar",
    "wallet-alpha": "wallet_alpha",
}


def _parse_csv_numbers(raw: str | None, cast):
    if not raw:
        return []
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        result.append(cast(part))
    return result


def _build_execution_model(
    name: str,
    *,
    fee_rate: float,
    latency_ms: int,
    slippage_bps: float,
    queue_ahead_ratio: float = 0.35,
    adverse_selection_bps: float | None = None,
    partial_fill_ratio: float = 0.75,
):
    model_name = (name or "depth").strip().lower()
    cfg = ExecutionModelConfig(
        fee_rate=fee_rate,
        latency_ms=latency_ms,
        slippage_bps=slippage_bps,
    )
    if model_name == "top":
        return TopOfBookExecutionModel(cfg)
    if model_name == "queue":
        return QueueAwareExecutionModel(
            ExecutionModelConfig(
                fee_rate=fee_rate,
                latency_ms=latency_ms,
                slippage_bps=slippage_bps,
                queue_ahead_ratio=queue_ahead_ratio,
                adverse_selection_bps=max(1.0, slippage_bps * 0.5) if adverse_selection_bps is None else adverse_selection_bps,
                partial_fill_ratio=partial_fill_ratio,
            )
        )
    return DepthVWAPExecutionModel(cfg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PolymarketBot backtests")
    parser.add_argument("--dataset", default=None, help="Dataset name under BACKTEST_DATA_DIR")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--dotenv-path", default=None, help="Optional .env path for ArbConfig")
    parser.add_argument("--strategy", default="t0", choices=sorted(STRATEGY_NAME_MAP))
    parser.add_argument("--execution-model", default="depth", choices=["depth", "top", "queue"])
    parser.add_argument("--scan-slippage-bps", default=None, help="Comma-separated slippage bps values")
    parser.add_argument("--scan-latency-ms", default=None, help="Comma-separated latency ms values")
    parser.add_argument("--scan-fee-rate", default=None, help="Comma-separated taker fee rates")
    parser.add_argument("--scan-min-deviation", default=None, help="Comma-separated T2 min deviation values")
    parser.add_argument("--scan-min-confidence", default=None, help="Comma-separated T2 min confidence values")
    parser.add_argument("--scan-holding-period-ms", default=None, help="Comma-separated holding periods for markout")
    parser.add_argument("--scan-queue-ahead-ratio", default=None, help="Comma-separated queue ahead ratios for queue model")
    parser.add_argument("--scan-adverse-selection-bps", default=None, help="Comma-separated adverse selection bps for queue model")
    parser.add_argument("--scan-partial-fill-ratio", default=None, help="Comma-separated partial fill ratios for queue model")
    parser.add_argument("--max-open-positions", type=int, default=None)
    parser.add_argument("--max-total-exposure", type=float, default=None)
    parser.add_argument("--holding-period-ms", type=int, default=None)
    parser.add_argument("--market-cooldown-ms", type=int, default=None)
    parser.add_argument("--t2-max-spread-bps", type=float, default=None)
    parser.add_argument("--t2-min-top-depth", type=float, default=None)
    parser.add_argument("--t2-max-complement-error-bps", type=float, default=None)
    args = parser.parse_args()

    config = ArbConfig.from_env(args.dotenv_path, require_wallet=False)
    dataset = args.dataset or config.backtest_default_dataset
    output_dir = args.output_dir or config.backtest_reports_dir
    runner = BacktestRunner(config.backtest_data_dir)
    strategy_name = STRATEGY_NAME_MAP[args.strategy]
    strategy = type("BacktestStrategy", (), {"strategy_name": strategy_name})()

    slippage_values = _parse_csv_numbers(args.scan_slippage_bps, float)
    latency_values = _parse_csv_numbers(args.scan_latency_ms, int)
    fee_values = _parse_csv_numbers(args.scan_fee_rate, float)
    min_deviation_values = _parse_csv_numbers(args.scan_min_deviation, float)
    min_confidence_values = _parse_csv_numbers(args.scan_min_confidence, float)
    holding_period_values = _parse_csv_numbers(args.scan_holding_period_ms, int)
    queue_ahead_values = _parse_csv_numbers(args.scan_queue_ahead_ratio, float)
    adverse_selection_values = _parse_csv_numbers(args.scan_adverse_selection_bps, float)
    partial_fill_values = _parse_csv_numbers(args.scan_partial_fill_ratio, float)
    if (
        slippage_values
        or latency_values
        or fee_values
        or min_deviation_values
        or min_confidence_values
        or holding_period_values
        or queue_ahead_values
        or adverse_selection_values
        or partial_fill_values
    ):
        if not slippage_values:
            slippage_values = [config.backtest_slippage_bps]
        if not latency_values:
            latency_values = [25]
        if not fee_values:
            fee_values = [config.polymarket_taker_fee_rate]
        if not min_deviation_values:
            min_deviation_values = [0.005]
        if not min_confidence_values:
            min_confidence_values = [0.1]
        if not holding_period_values:
            holding_period_values = [args.holding_period_ms or 300_000]
        if not queue_ahead_values:
            queue_ahead_values = [0.35]
        if not adverse_selection_values:
            adverse_selection_values = [max(1.0, config.backtest_slippage_bps * 0.5)]
        if not partial_fill_values:
            partial_fill_values = [0.75]
        results = []
        for slippage_bps in slippage_values:
            for latency_ms in latency_values:
                for fee_rate in fee_values:
                    for queue_ahead_ratio in queue_ahead_values:
                        for adverse_selection_bps in adverse_selection_values:
                            for partial_fill_ratio in partial_fill_values:
                                for min_deviation in min_deviation_values:
                                    for min_confidence in min_confidence_values:
                                        for holding_period_ms in holding_period_values:
                                            execution_model = _build_execution_model(
                                                args.execution_model,
                                                fee_rate=fee_rate,
                                                latency_ms=latency_ms,
                                                slippage_bps=slippage_bps,
                                                queue_ahead_ratio=queue_ahead_ratio,
                                                adverse_selection_bps=adverse_selection_bps,
                                                partial_fill_ratio=partial_fill_ratio,
                                            )
                                            report = runner.run(
                                                strategy=strategy,
                                                dataset=dataset,
                                                execution_model=execution_model,
                                                config=BacktestRunConfig(
                                                    dataset_name=dataset,
                                                    output_dir=output_dir,
                                                    dotenv_path=args.dotenv_path,
                                                    max_open_positions=args.max_open_positions,
                                                    max_total_exposure_usdc=args.max_total_exposure,
                                                    holding_period_ms=holding_period_ms,
                                                    market_cooldown_ms=args.market_cooldown_ms,
                                                    t2_min_deviation=min_deviation if args.strategy == "t2" else None,
                                                    t2_min_confidence=min_confidence if args.strategy == "t2" else None,
                                                    t2_max_spread_bps=args.t2_max_spread_bps if args.strategy == "t2" else None,
                                                    t2_min_top_depth=args.t2_min_top_depth if args.strategy == "t2" else None,
                                                    t2_max_complement_error_bps=args.t2_max_complement_error_bps if args.strategy == "t2" else None,
                                                ),
                                            )
                                            result = report.to_dict()
                                            result["execution_model"] = args.execution_model
                                            result["scan_slippage_bps"] = slippage_bps
                                            result["scan_latency_ms"] = latency_ms
                                            result["scan_fee_rate"] = fee_rate
                                            if args.execution_model == "queue":
                                                result["scan_queue_ahead_ratio"] = queue_ahead_ratio
                                                result["scan_adverse_selection_bps"] = adverse_selection_bps
                                                result["scan_partial_fill_ratio"] = partial_fill_ratio
                                            if args.strategy == "t2":
                                                result["scan_min_deviation"] = min_deviation
                                                result["scan_min_confidence"] = min_confidence
                                                result["scan_holding_period_ms"] = holding_period_ms
                                            results.append(result)
        results.sort(
            key=lambda row: (
                row.get("net_pnl", 0.0),
                1 if row.get("profit_factor_infinite") else 0,
                row.get("profit_factor", 0.0) or 0.0,
                row.get("fill_rate", 0.0),
                row.get("win_rate", 0.0),
                -row.get("avg_slippage_bps", 0.0),
            ),
            reverse=True,
        )
        save_scan_results(results, output_dir, scan_name=f"{dataset}_parameter_scan")
        if results:
            save_best_scan_result(results[0], output_dir, filename=f"{dataset}_best_scan_result.json")
        print(json.dumps(results, ensure_ascii=False))
        return

    execution_model = _build_execution_model(
        args.execution_model,
        fee_rate=config.polymarket_taker_fee_rate,
        latency_ms=25,
        slippage_bps=config.backtest_slippage_bps,
    )
    report = runner.run(
        strategy=strategy,
        dataset=dataset,
        execution_model=execution_model,
        config=BacktestRunConfig(
            dataset_name=dataset,
            output_dir=output_dir,
            dotenv_path=args.dotenv_path,
            max_open_positions=args.max_open_positions,
            max_total_exposure_usdc=args.max_total_exposure,
            holding_period_ms=args.holding_period_ms,
            market_cooldown_ms=args.market_cooldown_ms,
            t2_min_deviation=0.005 if args.strategy == "t2" else None,
            t2_min_confidence=0.1 if args.strategy == "t2" else None,
            t2_max_spread_bps=args.t2_max_spread_bps if args.strategy == "t2" else None,
            t2_min_top_depth=args.t2_min_top_depth if args.strategy == "t2" else None,
            t2_max_complement_error_bps=args.t2_max_complement_error_bps if args.strategy == "t2" else None,
        ),
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
