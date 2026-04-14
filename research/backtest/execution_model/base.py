"""Execution model abstractions for backtests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from random import Random
from typing import Any

from polymarket_arb.models import SimulatedExecution


@dataclass(frozen=True)
class ExecutionModelConfig:
    fee_rate: float = 0.02
    latency_ms: int = 0
    latency_jitter_ms: int = 0
    slippage_bps: float = 0.0
    queue_ahead_ratio: float = 0.0
    adverse_selection_bps: float = 0.0
    partial_fill_ratio: float = 1.0


class ExecutionModel(ABC):
    @abstractmethod
    def simulate(self, order_request: dict[str, Any], book_state: dict[str, Any]) -> SimulatedExecution:
        raise NotImplementedError


class TopOfBookExecutionModel(ExecutionModel):
    def __init__(self, config: ExecutionModelConfig | None = None, seed: int = 42):
        self._config = config or ExecutionModelConfig()
        self._rng = Random(seed)

    def simulate(self, order_request: dict[str, Any], book_state: dict[str, Any]) -> SimulatedExecution:
        requested_size = float(order_request.get("size", 0.0))
        best_price = book_state.get("best_ask") if order_request.get("side") == "BUY" else book_state.get("best_bid")
        available_size = float(book_state.get("available_size", 0.0))
        filled_size = min(requested_size, available_size)
        filled = filled_size > 0 and best_price is not None
        latency = self._config.latency_ms
        if self._config.latency_jitter_ms > 0:
            latency += self._rng.randint(0, self._config.latency_jitter_ms)
        fees = (best_price or 0.0) * filled_size * self._config.fee_rate
        return SimulatedExecution(
            filled=filled,
            filled_size=filled_size,
            average_price=best_price if filled else None,
            fees_paid=fees,
            slippage_bps=0.0,
            latency_ms=latency,
            notes=[] if filled else ["insufficient_depth"],
        )


class DepthVWAPExecutionModel(ExecutionModel):
    def __init__(self, config: ExecutionModelConfig | None = None, seed: int = 42):
        self._config = config or ExecutionModelConfig()
        self._rng = Random(seed)

    def simulate(self, order_request: dict[str, Any], book_state: dict[str, Any]) -> SimulatedExecution:
        requested_size = float(order_request.get("size", 0.0))
        side = order_request.get("side", "BUY")
        price_levels = book_state.get("ask_levels") if side == "BUY" else book_state.get("bid_levels")
        levels = list(price_levels or [])
        if not levels:
            best_price = book_state.get("best_ask") if side == "BUY" else book_state.get("best_bid")
            available = float(book_state.get("available_size", 0.0))
            levels = [(best_price, available)] if best_price is not None and available > 0 else []

        total_cost = 0.0
        filled_size = 0.0
        for price, size in levels:
            take = min(float(size), requested_size - filled_size)
            if take <= 0:
                continue
            total_cost += take * float(price)
            filled_size += take
            if filled_size >= requested_size - 1e-9:
                break

        filled = filled_size > 0
        avg_price = (total_cost / filled_size) if filled else None
        latency = self._config.latency_ms
        if self._config.latency_jitter_ms > 0:
            latency += self._rng.randint(0, self._config.latency_jitter_ms)

        slippage_multiplier = 1.0 + (self._config.slippage_bps / 10_000.0) if side == "BUY" else 1.0 - (self._config.slippage_bps / 10_000.0)
        if avg_price is not None:
            avg_price *= slippage_multiplier

        fees = (avg_price or 0.0) * filled_size * self._config.fee_rate
        return SimulatedExecution(
            filled=filled,
            filled_size=filled_size,
            average_price=avg_price,
            fees_paid=fees,
            slippage_bps=self._config.slippage_bps if filled else 0.0,
            latency_ms=latency,
            notes=[] if filled else ["insufficient_depth"],
        )


class QueueAwareExecutionModel(ExecutionModel):
    """Execution model with queue positioning and adverse selection.

    This is still simplified, but more realistic than pure top-of-book or static VWAP:
    - a configurable fraction of visible size is assumed to be ahead in the queue
    - only a configurable fraction of reachable size fills within the latency window
    - adverse selection can move the achieved price further against us
    """

    def __init__(self, config: ExecutionModelConfig | None = None, seed: int = 42):
        self._config = config or ExecutionModelConfig()
        self._rng = Random(seed)

    def simulate(self, order_request: dict[str, Any], book_state: dict[str, Any]) -> SimulatedExecution:
        requested_size = float(order_request.get("size", 0.0))
        side = order_request.get("side", "BUY")
        price_levels = book_state.get("ask_levels") if side == "BUY" else book_state.get("bid_levels")
        levels = list(price_levels or [])
        if not levels:
            best_price = book_state.get("best_ask") if side == "BUY" else book_state.get("best_bid")
            available = float(book_state.get("available_size", 0.0))
            levels = [(best_price, available)] if best_price is not None and available > 0 else []

        effective_levels: list[tuple[float, float]] = []
        queue_ratio = max(0.0, min(0.99, self._config.queue_ahead_ratio))
        for idx, (price, size) in enumerate(levels):
            visible = max(0.0, float(size))
            reachable = visible * (1.0 - queue_ratio) if idx == 0 else visible
            if reachable > 0:
                effective_levels.append((float(price), reachable))

        partial_fill_ratio = max(0.0, min(1.0, self._config.partial_fill_ratio))
        total_cost = 0.0
        filled_size = 0.0
        remaining_target = requested_size * partial_fill_ratio
        for price, size in effective_levels:
            take = min(size, remaining_target - filled_size)
            if take <= 0:
                continue
            total_cost += take * price
            filled_size += take
            if filled_size >= remaining_target - 1e-9:
                break

        filled = filled_size > 0.0
        avg_price = (total_cost / filled_size) if filled else None
        latency = self._config.latency_ms
        if self._config.latency_jitter_ms > 0:
            latency += self._rng.randint(0, self._config.latency_jitter_ms)

        total_slippage_bps = self._config.slippage_bps + self._config.adverse_selection_bps
        if avg_price is not None and total_slippage_bps:
            multiplier = 1.0 + (total_slippage_bps / 10_000.0) if side == "BUY" else 1.0 - (total_slippage_bps / 10_000.0)
            avg_price *= multiplier

        fees = (avg_price or 0.0) * filled_size * self._config.fee_rate
        notes: list[str] = []
        if not filled:
            notes.append("insufficient_depth_after_queue")
        elif filled_size + 1e-9 < requested_size:
            notes.append("partial_fill")
        if queue_ratio > 0:
            notes.append(f"queue_ahead_ratio={queue_ratio:.2f}")
        if self._config.adverse_selection_bps > 0:
            notes.append(f"adverse_selection_bps={self._config.adverse_selection_bps:.1f}")

        return SimulatedExecution(
            filled=filled,
            filled_size=filled_size,
            average_price=avg_price,
            fees_paid=fees,
            slippage_bps=total_slippage_bps if filled else 0.0,
            latency_ms=latency,
            notes=notes,
        )
