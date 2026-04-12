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
