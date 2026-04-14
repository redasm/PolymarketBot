"""Minimal event-driven backtest runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import BacktestReport
from research.backtest.adapters.t0_adapter import T0BacktestAdapter
from research.backtest.adapters.t2_adapter import build_t2_adapter
from research.backtest.data.reader import BacktestDatasetReader
from research.backtest.execution_model.base import (
    DepthVWAPExecutionModel,
    ExecutionModel,
    ExecutionModelConfig,
)
from research.backtest.features import summarize_binary_microstructure
from research.backtest.reports.reporting import save_recommended_params, save_report, save_trade_log


@dataclass
class BacktestRunConfig:
    dataset_name: str
    output_dir: str = "research/backtest/output"
    dotenv_path: str | None = None
    max_open_positions: int | None = None
    max_total_exposure_usdc: float | None = None
    holding_period_ms: int | None = None
    market_cooldown_ms: int | None = None
    t2_min_deviation: float | None = None
    t2_min_confidence: float | None = None
    t2_max_spread_bps: float | None = None
    t2_min_top_depth: float | None = None
    t2_max_complement_error_bps: float | None = None


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
        strategy_name = getattr(strategy, "strategy_name", strategy.__class__.__name__)
        if strategy_name == "t2_statistical_arbitrage":
            return self._run_t2_markout(
                strategy_name=strategy_name,
                market_states=market_states,
                arb_config=arb_config,
                run_cfg=run_cfg,
                execution_model=execution_model,
            )
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
        total_fees_paid = 0.0
        total_notional = 0.0
        total_latency = 0.0
        cumulative_pnl = 0.0
        equity_peak = 0.0
        max_drawdown = 0.0
        signal_edges_bps: list[float] = []
        winning_pnl = 0.0
        losing_pnl = 0.0
        skipped_signals = 0
        open_positions: list[dict[str, float | int | str]] = []
        recent_market_ts: dict[str, int] = {}
        trade_rows: list[dict[str, Any]] = []

        for row in market_states:
            ts_ms = int(row.get("ts_ms", 0))
            open_positions = [position for position in open_positions if int(position["release_ts_ms"]) > ts_ms]
            current_exposure = sum(float(position["exposure"]) for position in open_positions)

            adapter, market = T0BacktestAdapter.from_rows(arb_config, row)
            opportunity = adapter.detect(market)
            if opportunity is None:
                continue
            total_signals += 1
            holding_period_ms = max(0, int(run_cfg.holding_period_ms or 0))
            market_cooldown_ms = max(0, int(run_cfg.market_cooldown_ms or 0))
            if market_cooldown_ms > 0:
                last_market_ts = recent_market_ts.get(market.condition_id)
                if last_market_ts is not None and (ts_ms - last_market_ts) < market_cooldown_ms:
                    skipped_signals += 1
                    continue
            if run_cfg.max_open_positions is not None and len(open_positions) >= run_cfg.max_open_positions:
                skipped_signals += 1
                continue

            order_request = adapter.to_order_request(opportunity)
            requested_notional = opportunity.total_cost * float(order_request.get("size", 0.0))
            if (
                run_cfg.max_total_exposure_usdc is not None
                and requested_notional > 0
                and (current_exposure + requested_notional) > run_cfg.max_total_exposure_usdc
            ):
                skipped_signals += 1
                continue
            execution = exec_model.simulate(order_request, row)
            total_trades += 1
            total_latency += execution.latency_ms
            features = summarize_binary_microstructure(row)
            realized_pnl = 0.0
            trade_row = {
                "ts_ms": int(row.get("ts_ms", 0)),
                "condition_id": row.get("condition_id", ""),
                "event_id": row.get("event_id", ""),
                "question": row.get("question", ""),
                "signal_gross_edge": opportunity.gross_edge,
                "signal_edge": opportunity.net_edge,
                "signal_edge_bps": opportunity.edge_pct * 100.0,
                "requested_size": order_request.get("size", 0.0),
                "filled": execution.filled,
                "filled_size": execution.filled_size,
                "average_price": execution.average_price,
                "fees_paid": execution.fees_paid,
                "slippage_bps": execution.slippage_bps,
                "latency_ms": execution.latency_ms,
                **features,
            }
            signal_edges_bps.append(opportunity.edge_pct * 100.0)
            if execution.filled:
                filled_trades += 1
                filled_notional = (execution.average_price or 0.0) * execution.filled_size
                total_notional += filled_notional
                total_fees_paid += execution.fees_paid
                realized_pnl = opportunity.gross_edge * execution.filled_size - execution.fees_paid
                gross_pnl += realized_pnl
                total_slippage += execution.slippage_bps
                if holding_period_ms > 0 and filled_notional > 0:
                    open_positions.append(
                        {
                            "condition_id": market.condition_id,
                            "exposure": filled_notional,
                            "release_ts_ms": ts_ms + holding_period_ms,
                        }
                    )
                recent_market_ts[market.condition_id] = ts_ms
                if realized_pnl >= 0:
                    winning_pnl += realized_pnl
                else:
                    losing_pnl += abs(realized_pnl)
            cumulative_pnl += realized_pnl
            equity_peak = max(equity_peak, cumulative_pnl)
            max_drawdown = max(max_drawdown, equity_peak - cumulative_pnl)
            trade_row["realized_pnl"] = realized_pnl
            trade_row["cumulative_pnl"] = cumulative_pnl
            if execution.notes:
                trade_row["execution_notes"] = list(execution.notes)
            trade_rows.append(trade_row)

        report = BacktestReport(
            strategy_name=getattr(strategy, "strategy_name", strategy.__class__.__name__),
            dataset_name=dataset,
            total_signals=total_signals,
            total_trades=total_trades,
            filled_trades=filled_trades,
            skipped_signals=skipped_signals,
            execution_model=exec_model.__class__.__name__,
            gross_pnl=gross_pnl,
            net_pnl=gross_pnl,
            max_drawdown=max_drawdown,
            win_rate=(filled_trades / total_trades) if total_trades else 0.0,
            fill_rate=(filled_trades / total_signals) if total_signals else 0.0,
            avg_slippage_bps=(total_slippage / filled_trades) if filled_trades else 0.0,
            avg_latency_ms=(total_latency / total_trades) if total_trades else 0.0,
            avg_signal_edge_bps=(sum(signal_edges_bps) / len(signal_edges_bps)) if signal_edges_bps else 0.0,
            total_fees_paid=total_fees_paid,
            total_notional_usdc=total_notional,
            profit_factor=_compute_profit_factor(winning_pnl, losing_pnl)[0],
            profit_factor_infinite=_compute_profit_factor(winning_pnl, losing_pnl)[1],
            notes=[
                "v2_t0_binary_runner",
                f"execution_model={exec_model.__class__.__name__}",
                "features=microprice,imbalance,spread,complement_error",
                f"skipped_signals={skipped_signals}",
            ],
        )

        output_dir = Path(run_cfg.output_dir)
        save_report(report, output_dir)
        save_trade_log(trade_rows, output_dir, report)
        save_recommended_params(
            {
                "dataset_name": dataset,
                "strategy_name": report.strategy_name,
                "execution_model": exec_model.__class__.__name__,
                "fill_rate": report.fill_rate,
                "avg_signal_edge_bps": report.avg_signal_edge_bps,
                "profit_factor": report.profit_factor,
                "notes": ["manual_review_required", "review_fill_rate_and_profit_factor"],
            },
            output_dir / "recommended_params.json",
        )
        return report

    def _run_t2_markout(
        self,
        *,
        strategy_name: str,
        market_states: list[dict[str, Any]],
        arb_config: ArbConfig,
        run_cfg: BacktestRunConfig,
        execution_model: ExecutionModel | None,
    ) -> BacktestReport:
        exec_model = execution_model or DepthVWAPExecutionModel(
            ExecutionModelConfig(
                fee_rate=arb_config.polymarket_taker_fee_rate,
                latency_ms=25,
                slippage_bps=arb_config.backtest_slippage_bps,
            )
        )
        adapter = build_t2_adapter(
            min_deviation=float(run_cfg.t2_min_deviation if run_cfg.t2_min_deviation is not None else 0.005),
            min_confidence=float(run_cfg.t2_min_confidence if run_cfg.t2_min_confidence is not None else 0.1),
            max_spread_bps=run_cfg.t2_max_spread_bps,
            min_top_depth=run_cfg.t2_min_top_depth,
            max_complement_error_bps=run_cfg.t2_max_complement_error_bps,
        )
        states_by_market: dict[str, list[dict[str, Any]]] = {}
        for row in market_states:
            states_by_market.setdefault(str(row.get("condition_id") or ""), []).append(row)
        for rows in states_by_market.values():
            rows.sort(key=lambda item: int(item.get("ts_ms", 0)))

        filled_trades = 0
        total_trades = 0
        total_signals = 0
        skipped_signals = 0
        gross_pnl = 0.0
        total_slippage = 0.0
        total_fees_paid = 0.0
        total_notional = 0.0
        total_latency = 0.0
        cumulative_pnl = 0.0
        equity_peak = 0.0
        max_drawdown = 0.0
        signal_edges_bps: list[float] = []
        winning_pnl = 0.0
        losing_pnl = 0.0
        open_positions: list[dict[str, float | int | str]] = []
        recent_market_ts: dict[str, int] = {}
        trade_rows: list[dict[str, Any]] = []
        holding_period_ms = max(60_000, int(run_cfg.holding_period_ms or 300_000))
        market_cooldown_ms = max(0, int(run_cfg.market_cooldown_ms or 0))

        for condition_id, rows in states_by_market.items():
            for idx, row in enumerate(rows):
                ts_ms = int(row.get("ts_ms", 0))
                open_positions = [position for position in open_positions if int(position["release_ts_ms"]) > ts_ms]
                current_exposure = sum(float(position["exposure"]) for position in open_positions)

                signal = adapter.detect(row, order_size_usdc=arb_config.default_order_size_usdc)
                if signal is None:
                    continue
                total_signals += 1
                if market_cooldown_ms > 0:
                    last_market_ts = recent_market_ts.get(condition_id)
                    if last_market_ts is not None and (ts_ms - last_market_ts) < market_cooldown_ms:
                        skipped_signals += 1
                        continue
                if run_cfg.max_open_positions is not None and len(open_positions) >= run_cfg.max_open_positions:
                    skipped_signals += 1
                    continue
                if (
                    run_cfg.max_total_exposure_usdc is not None
                    and (current_exposure + signal.entry_notional_usdc) > run_cfg.max_total_exposure_usdc
                ):
                    skipped_signals += 1
                    continue

                order_request = adapter.to_order_request(signal, row)
                execution = exec_model.simulate(order_request, row)
                total_trades += 1
                total_latency += execution.latency_ms
                features = summarize_binary_microstructure(row)
                exit_row = self._find_exit_row(rows, idx, ts_ms + holding_period_ms)
                signal_edges_bps.append(signal.edge_bps)
                trade_row = {
                    "ts_ms": ts_ms,
                    "condition_id": condition_id,
                    "event_id": row.get("event_id", ""),
                    "question": row.get("question", ""),
                    "signal_edge_bps": signal.edge_bps,
                    "signal_confidence": signal.confidence,
                    "signal_action": signal.action,
                    "requested_size": order_request.get("size", 0.0),
                    "filled": execution.filled,
                    "filled_size": execution.filled_size,
                    "average_price": execution.average_price,
                    "fees_paid": execution.fees_paid,
                    "slippage_bps": execution.slippage_bps,
                    "latency_ms": execution.latency_ms,
                    **features,
                }
                realized_pnl = 0.0
                if execution.filled and execution.average_price is not None:
                    filled_trades += 1
                    total_notional += execution.average_price * execution.filled_size
                    total_fees_paid += execution.fees_paid
                    realized_pnl, exit_meta = adapter.realized_pnl(
                        signal,
                        execution_price=execution.average_price,
                        filled_size=execution.filled_size,
                        entry_fees=execution.fees_paid,
                        exit_row=exit_row,
                        exit_fee_rate=arb_config.polymarket_taker_fee_rate,
                    )
                    trade_row.update(exit_meta)
                    gross_pnl += realized_pnl
                    total_slippage += execution.slippage_bps
                    open_positions.append(
                        {
                            "condition_id": condition_id,
                            "exposure": execution.average_price * execution.filled_size,
                            "release_ts_ms": ts_ms + holding_period_ms,
                        }
                    )
                    recent_market_ts[condition_id] = ts_ms
                    if realized_pnl >= 0:
                        winning_pnl += realized_pnl
                    else:
                        losing_pnl += abs(realized_pnl)
                cumulative_pnl += realized_pnl
                equity_peak = max(equity_peak, cumulative_pnl)
                max_drawdown = max(max_drawdown, equity_peak - cumulative_pnl)
                trade_row["realized_pnl"] = realized_pnl
                trade_row["cumulative_pnl"] = cumulative_pnl
                if execution.notes:
                    trade_row["execution_notes"] = list(execution.notes)
                trade_rows.append(trade_row)

        report = BacktestReport(
            strategy_name=strategy_name,
            dataset_name=run_cfg.dataset_name,
            total_signals=total_signals,
            total_trades=total_trades,
            filled_trades=filled_trades,
            skipped_signals=skipped_signals,
            execution_model=exec_model.__class__.__name__,
            gross_pnl=gross_pnl,
            net_pnl=gross_pnl,
            max_drawdown=max_drawdown,
            win_rate=(sum(1 for row in trade_rows if row.get("realized_pnl", 0.0) > 0) / filled_trades) if filled_trades else 0.0,
            fill_rate=(filled_trades / total_signals) if total_signals else 0.0,
            avg_slippage_bps=(total_slippage / filled_trades) if filled_trades else 0.0,
            avg_latency_ms=(total_latency / total_trades) if total_trades else 0.0,
            avg_signal_edge_bps=(sum(signal_edges_bps) / len(signal_edges_bps)) if signal_edges_bps else 0.0,
            total_fees_paid=total_fees_paid,
            total_notional_usdc=total_notional,
            profit_factor=_compute_profit_factor(winning_pnl, losing_pnl)[0],
            profit_factor_infinite=_compute_profit_factor(winning_pnl, losing_pnl)[1],
            notes=[
                "t2_markout_runner",
                f"execution_model={exec_model.__class__.__name__}",
                f"holding_period_ms={holding_period_ms}",
                f"skipped_signals={skipped_signals}",
            ],
        )
        output_dir = Path(run_cfg.output_dir)
        save_report(report, output_dir)
        save_trade_log(trade_rows, output_dir, report)
        save_recommended_params(
            {
                "dataset_name": run_cfg.dataset_name,
                "strategy_name": report.strategy_name,
                "execution_model": exec_model.__class__.__name__,
                "fill_rate": report.fill_rate,
                "avg_signal_edge_bps": report.avg_signal_edge_bps,
                "profit_factor": report.profit_factor,
                "profit_factor_infinite": report.profit_factor_infinite,
                "holding_period_ms": holding_period_ms,
                "t2_min_deviation": run_cfg.t2_min_deviation,
                "t2_min_confidence": run_cfg.t2_min_confidence,
                "t2_max_spread_bps": run_cfg.t2_max_spread_bps,
                "t2_min_top_depth": run_cfg.t2_min_top_depth,
                "t2_max_complement_error_bps": run_cfg.t2_max_complement_error_bps,
                "notes": ["manual_review_required", "review_markout_stability"],
            },
            output_dir / "recommended_params.json",
        )
        return report

    @staticmethod
    def _find_exit_row(rows: list[dict[str, Any]], start_idx: int, target_ts_ms: int) -> dict[str, Any] | None:
        for row in rows[start_idx + 1:]:
            if int(row.get("ts_ms", 0)) >= target_ts_ms:
                return row
        return rows[-1] if rows else None


def _compute_profit_factor(winning_pnl: float, losing_pnl: float) -> tuple[float | None, bool]:
    if losing_pnl > 0:
        return (winning_pnl / losing_pnl, False)
    if winning_pnl > 0:
        return (None, True)
    return (None, False)
