"""Minimal event-driven backtest runner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import BacktestReport
from research.backtest.adapters.t0_adapter import T0BacktestAdapter
from research.backtest.adapters.t2_adapter import build_t2_adapter
from research.backtest.adapters.quant_strategy_adapter import (
    EventCalendarBacktestAdapter,
    LogicalConstraintBacktestAdapter,
    WalletAlphaBacktestAdapter,
)
from research.backtest.data.reader import BacktestDatasetReader
from research.backtest.execution_model.base import (
    DepthVWAPExecutionModel,
    ExecutionModel,
    ExecutionModelConfig,
    estimate_binary_clob_fee,
)
from research.backtest.features import summarize_binary_microstructure
from research.backtest.reports.reporting import save_recommended_params, save_report, save_trade_log
from polymarket_arb.strategies.logical_constraints import RelationRule
from polymarket_arb.strategies.wallet_alpha import WalletProfile


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
        if strategy_name in {"logical_constraint", "event_calendar", "wallet_alpha"}:
            return self._run_quant_markout(
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

    def _run_quant_markout(
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
        adapter = _build_quant_adapter(strategy_name, arb_config)
        states_by_market: dict[str, list[dict[str, Any]]] = {}
        rows_by_ts: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for idx, row in enumerate(market_states):
            condition_id = str(row.get("condition_id") or "")
            states_by_market.setdefault(condition_id, []).append(row)
            rows_by_ts.setdefault(int(row.get("ts_ms", 0)), []).append((idx, row))
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

        signal_items = _quant_signal_items(strategy_name, adapter, market_states, rows_by_ts)
        for row_idx, row, signal in signal_items:
            ts_ms = int(row.get("ts_ms", 0))
            condition_id = str(row.get("condition_id") or "")
            open_positions = [position for position in open_positions if int(position["release_ts_ms"]) > ts_ms]
            current_exposure = sum(float(position["exposure"]) for position in open_positions)

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
                and (current_exposure + signal.recommended_size_usdc) > run_cfg.max_total_exposure_usdc
            ):
                skipped_signals += 1
                continue

            order_request = adapter.to_order_request(signal, row)
            execution = exec_model.simulate(order_request, {**row, **order_request})
            total_trades += 1
            total_latency += execution.latency_ms
            features = summarize_binary_microstructure(row)
            rows_for_market = states_by_market.get(condition_id, [])
            local_idx = _row_index_in_market(rows_for_market, row)
            exit_row = self._find_exit_row(rows_for_market, local_idx, ts_ms + holding_period_ms)
            signal_edges_bps.append(signal.expected_edge)
            trade_row = {
                "ts_ms": ts_ms,
                "condition_id": condition_id,
                "event_id": row.get("event_id", ""),
                "question": row.get("question", ""),
                "signal_type": signal.signal_type,
                "signal_edge_bps": signal.expected_edge,
                "signal_confidence": signal.confidence,
                "signal_action": signal.payload.get("action", "BUY_YES"),
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
                filled_notional = execution.average_price * execution.filled_size
                total_notional += filled_notional
                total_fees_paid += execution.fees_paid
                realized_pnl, exit_meta = _directional_realized_pnl(
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
                        "exposure": filled_notional,
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
                "quant_strategy_markout_runner",
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
                "notes": ["manual_review_required", "review_quant_strategy_inputs"],
            },
            output_dir / "recommended_params.json",
        )
        return report


def _compute_profit_factor(winning_pnl: float, losing_pnl: float) -> tuple[float | None, bool]:
    if losing_pnl > 0:
        return (winning_pnl / losing_pnl, False)
    if winning_pnl > 0:
        return (None, True)
    return (None, False)


def _build_quant_adapter(strategy_name: str, arb_config: ArbConfig):
    if strategy_name == "logical_constraint":
        return LogicalConstraintBacktestAdapter(
            rules=_parse_relation_rules(arb_config.logical_constraints_json),
            order_size_usdc=arb_config.default_order_size_usdc,
        )
    if strategy_name == "event_calendar":
        return EventCalendarBacktestAdapter(
            order_size_usdc=arb_config.default_order_size_usdc,
            taker_fee_rate=arb_config.polymarket_taker_fee_rate,
        )
    if strategy_name == "wallet_alpha":
        return WalletAlphaBacktestAdapter(
            profiles=_parse_wallet_profiles(arb_config.wallet_alpha_profiles_json),
            order_size_usdc=arb_config.default_order_size_usdc,
        )
    raise ValueError(f"unsupported quant strategy: {strategy_name}")


def _parse_relation_rules(raw: str) -> list[RelationRule]:
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rules: list[RelationRule] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            rules.append(
                RelationRule(
                    subject_market_id=str(row["subject_market_id"]),
                    bound_market_id=str(row["bound_market_id"]),
                    relation_type=str(row.get("relation_type", "subject_lte_bound")),
                    min_violation_bps=float(row.get("min_violation_bps", 200.0)),
                    max_size_usdc=(
                        float(row["max_size_usdc"])
                        if row.get("max_size_usdc") not in (None, "")
                        else None
                    ),
                    tags=tuple(str(tag) for tag in row.get("tags", []) or []),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rules


def _parse_wallet_profiles(raw: str) -> dict[str, WalletProfile]:
    if not raw:
        return {}
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    profiles: dict[str, WalletProfile] = {}
    for wallet, row in rows.items() if isinstance(rows, dict) else []:
        if not isinstance(row, dict):
            continue
        try:
            profiles[str(wallet)] = WalletProfile(
                wallet_address=str(row.get("wallet_address") or wallet),
                trade_count=int(row.get("trade_count", 0)),
                realized_roi=float(row.get("realized_roi", 0.0)),
                lagged_follow_roi=float(row.get("lagged_follow_roi", 0.0)),
                max_drawdown=float(row.get("max_drawdown", 1.0)),
                concentration_score=float(row.get("concentration_score", 1.0)),
                category_edges={
                    str(key): float(value)
                    for key, value in dict(row.get("category_edges", {}) or {}).items()
                },
            )
        except (TypeError, ValueError):
            continue
    return profiles


def _quant_signal_items(
    strategy_name: str,
    adapter: Any,
    market_states: list[dict[str, Any]],
    rows_by_ts: dict[int, list[tuple[int, dict[str, Any]]]],
) -> list[tuple[int, dict[str, Any], Any]]:
    items: list[tuple[int, dict[str, Any], Any]] = []
    if strategy_name == "logical_constraint":
        for ts_ms in sorted(rows_by_ts):
            indexed_rows = rows_by_ts[ts_ms]
            rows = [row for _, row in indexed_rows]
            row_by_market = {str(row.get("condition_id") or ""): (idx, row) for idx, row in indexed_rows}
            for signal in adapter.detect_many(rows):
                matched = row_by_market.get(signal.market_id)
                if matched is not None:
                    items.append((matched[0], matched[1], signal))
        return items

    for idx, row in enumerate(market_states):
        signal = adapter.detect(row)
        if signal is not None:
            items.append((idx, row, signal))
    return items


def _row_index_in_market(rows: list[dict[str, Any]], target: dict[str, Any]) -> int:
    for idx, row in enumerate(rows):
        if row is target:
            return idx
    target_ts = int(target.get("ts_ms", 0))
    for idx, row in enumerate(rows):
        if int(row.get("ts_ms", 0)) == target_ts:
            return idx
    return 0


def _directional_realized_pnl(
    signal: Any,
    *,
    execution_price: float,
    filled_size: float,
    entry_fees: float,
    exit_row: dict[str, Any] | None,
    exit_fee_rate: float,
) -> tuple[float, dict[str, float | None]]:
    if exit_row is None:
        return 0.0, {"exit_price": None, "markout_bps": None}
    action = str(signal.payload.get("action", "BUY_YES")).upper()
    prefix = "no" if action == "BUY_NO" else "yes"
    exit_price = _executable_bid_price(
        exit_row.get(f"{prefix}_best_bid"),
        exit_row.get(f"{prefix}_bid_size"),
        exit_row.get(f"{prefix}_bid_levels"),
        filled_size,
    )
    if exit_price is None:
        return 0.0, {"exit_price": None, "markout_bps": None}
    gross = (exit_price - execution_price) * filled_size
    exit_fee = estimate_binary_clob_fee(exit_price, filled_size, exit_fee_rate)
    pnl = gross - entry_fees - exit_fee
    markout_bps = ((exit_price - execution_price) / execution_price) * 10_000.0 if execution_price > 0 else None
    return pnl, {"exit_price": exit_price, "markout_bps": markout_bps}


def _mid(bid: Any, ask: Any) -> float | None:
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2.0


def _executable_bid_price(
    best_bid: Any,
    best_size: Any,
    levels: Any,
    target_size: float,
) -> float | None:
    """Return sell VWAP at executable bids; never mark exits at midpoint."""
    try:
        fallback_price = float(best_bid) if best_bid is not None else 0.0
        fallback_size = float(best_size or 0.0)
    except (TypeError, ValueError):
        return None
    parsed: list[tuple[float, float]] = []
    if isinstance(levels, list):
        for item in levels:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    parsed.append((float(item[0]), float(item[1])))
                except (TypeError, ValueError):
                    continue
    if not parsed and fallback_price > 0 and fallback_size > 0:
        parsed = [(fallback_price, fallback_size)]
    remaining = max(0.0, float(target_size))
    if remaining <= 0 or not parsed:
        return None
    value = filled = 0.0
    for price, size in parsed:
        take = min(max(0.0, size), remaining - filled)
        if take <= 0:
            continue
        value += take * price
        filled += take
        if filled >= remaining - 1e-9:
            break
    return value / filled if filled > 0 else None
