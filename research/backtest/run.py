"""CLI entrypoint for the minimal backtest runner."""

from __future__ import annotations

import argparse
import json

from polymarket_arb.config import ArbConfig
from research.backtest.execution_model.base import DepthVWAPExecutionModel, ExecutionModelConfig
from research.backtest.replay.runner import BacktestRunConfig, BacktestRunner
from research.backtest.reports.reporting import save_best_scan_result, save_scan_results


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PolymarketBot backtests")
    parser.add_argument("--dataset", default=None, help="Dataset name under BACKTEST_DATA_DIR")
    parser.add_argument("--output-dir", default="research/backtest/output")
    parser.add_argument("--dotenv-path", default=None, help="Optional .env path for ArbConfig")
    parser.add_argument("--scan-slippage-bps", default=None, help="Comma-separated slippage bps values")
    parser.add_argument("--scan-latency-ms", default=None, help="Comma-separated latency ms values")
    args = parser.parse_args()

    config = ArbConfig.from_env(args.dotenv_path, require_wallet=False)
    dataset = args.dataset or config.backtest_default_dataset
    output_dir = config.backtest_reports_dir or args.output_dir
    runner = BacktestRunner(config.backtest_data_dir)
    strategy = type("BacktestStrategy", (), {"strategy_name": "t0_structural_arbitrage"})()

    slippage_values = _parse_csv_numbers(args.scan_slippage_bps, float)
    latency_values = _parse_csv_numbers(args.scan_latency_ms, int)
    if slippage_values or latency_values:
        if not slippage_values:
            slippage_values = [config.backtest_slippage_bps]
        if not latency_values:
            latency_values = [25]
        results = []
        for slippage_bps in slippage_values:
            for latency_ms in latency_values:
                execution_model = DepthVWAPExecutionModel(
                    ExecutionModelConfig(
                        fee_rate=0.02,
                        latency_ms=latency_ms,
                        slippage_bps=slippage_bps,
                    )
                )
                report = runner.run(
                    strategy=strategy,
                    dataset=dataset,
                    execution_model=execution_model,
                    config=BacktestRunConfig(dataset_name=dataset, output_dir=output_dir, dotenv_path=args.dotenv_path),
                )
                result = report.to_dict()
                result["scan_slippage_bps"] = slippage_bps
                result["scan_latency_ms"] = latency_ms
                results.append(result)
        results.sort(key=lambda row: (row.get("net_pnl", 0.0), row.get("win_rate", 0.0), -row.get("avg_slippage_bps", 0.0)), reverse=True)
        save_scan_results(results, output_dir, scan_name=f"{dataset}_parameter_scan")
        if results:
            save_best_scan_result(results[0], output_dir, filename=f"{dataset}_best_scan_result.json")
        print(json.dumps(results, ensure_ascii=False))
        return

    execution_model = DepthVWAPExecutionModel(
        ExecutionModelConfig(
            fee_rate=0.02,
            latency_ms=25,
            slippage_bps=config.backtest_slippage_bps,
        )
    )
    report = runner.run(
        strategy=strategy,
        dataset=dataset,
        execution_model=execution_model,
        config=BacktestRunConfig(dataset_name=dataset, output_dir=output_dir, dotenv_path=args.dotenv_path),
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
