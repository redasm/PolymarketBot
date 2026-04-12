"""Minimal event-driven backtest runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import BacktestReport
from research.backtest.adapters.t0_adapter import T0BacktestAdapter
from research.backtest.data.reader import BacktestDatasetReader
from research.backtest.execution_model.base import (
    DepthVWAPExecutionModel,
    ExecutionModel,
    ExecutionModelConfig,
)
from research.backtest.reports.reporting import save_recommended_params, save_report, save_trade_log


@dataclass
class BacktestRunConfig:
    dataset_name: str
    output_dir: str = "research/backtest/output"
    dotenv_path: str | None = None


class BacktestRunner:
    def __init__(self, data_dir: str):
        self._reader = BacktestDatasetReader(data_dir)

    def run(
        self,
        strategy: Any,
        dataset: str,
        execution_model: ExecutionModel | None = None,
        config: BacktestRunConfig | None = None,
    ) -> BacktestReport:
        run_cfg = config or BacktestRunConfig(dataset_name=dataset)
        market_states = self._reader.load_t0_market_states(dataset)
        arb_config = ArbConfig.from_env(run_cfg.dotenv_path, require_wallet=False)
        exec_model = execution_model or DepthVWAPExecutionModel(
            ExecutionModelConfig(
                fee_rate=0.02,
                latency_ms=25,
                slippage_bps=arb_config.backtest_slippage_bps,
            )
        )

        filled_trades = 0
        total_trades = 0
        total_signals = 0
        gross_pnl = 0.0
        total_slippage = 0.0
        trade_rows: list[dict[str, Any]] = []

        for row in market_states:
            adapter, market = T0BacktestAdapter.from_rows(arb_config, row)
            opportunity = adapter.detect(market)
            if opportunity is None:
                continue
            total_signals += 1
            order_request = adapter.to_order_request(opportunity)
            execution = exec_model.simulate(order_request, row)
            total_trades += 1
            trade_row = {
                "ts_ms": int(row.get("ts_ms", 0)),
                "condition_id": row.get("condition_id", ""),
                "event_id": row.get("event_id", ""),
                "question": row.get("question", ""),
                "signal_edge": opportunity.net_edge,
                "requested_size": order_request.get("size", 0.0),
                "filled": execution.filled,
                "filled_size": execution.filled_size,
                "average_price": execution.average_price,
                "fees_paid": execution.fees_paid,
                "slippage_bps": execution.slippage_bps,
                "latency_ms": execution.latency_ms,
            }
            if execution.filled:
                filled_trades += 1
                gross_pnl += opportunity.net_edge * execution.filled_size - execution.fees_paid
                total_slippage += execution.slippage_bps
                trade_row["realized_pnl"] = opportunity.net_edge * execution.filled_size - execution.fees_paid
            else:
                trade_row["realized_pnl"] = 0.0
            trade_rows.append(trade_row)

        report = BacktestReport(
            strategy_name=getattr(strategy, "strategy_name", strategy.__class__.__name__),
            dataset_name=dataset,
            total_signals=total_signals,
            total_trades=total_trades,
            filled_trades=filled_trades,
            gross_pnl=gross_pnl,
            net_pnl=gross_pnl,
            max_drawdown=0.0,
            win_rate=(filled_trades / total_trades) if total_trades else 0.0,
            avg_slippage_bps=(total_slippage / filled_trades) if filled_trades else 0.0,
            notes=["v1_t0_binary_runner"],
        )

        output_dir = Path(run_cfg.output_dir)
        save_report(report, output_dir)
        save_trade_log(trade_rows, output_dir, report)
        save_recommended_params(
            {
                "dataset_name": dataset,
                "strategy_name": report.strategy_name,
                "notes": ["manual_review_required"],
            },
            output_dir / "recommended_params.json",
        )
        return report
