"""Markov optimal-stopping policy for exiting directional Polymarket positions.

The policy solves a finite-horizon Bellman recursion on a discretized token
price grid. It is intentionally small and dependency-free so it can be reused
from live trading, backtests, or notebooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class ExitDecision:
    """Decision for a single position at the current price/time."""

    action: str
    market_price: float
    remaining_steps: int
    value: float
    continuation_value: float
    stop_threshold: float | None
    scale_out_fraction: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScaleOutLevel:
    """One tranche in a scale-out plan."""

    tranche: int
    threshold: float
    fraction: float


@dataclass(frozen=True)
class OptimalStoppingPolicy:
    """Solved value table and stop boundaries."""

    price_grid: tuple[float, ...]
    values: tuple[tuple[float, ...], ...]
    continuation_values: tuple[tuple[float, ...], ...]
    stop_thresholds: tuple[float | None, ...]

    def decide(self, remaining_steps: int, market_price: float) -> ExitDecision:
        """Return HOLD/STOP for a token that can be sold at ``market_price``."""
        tau = _clamp_int(remaining_steps, 0, len(self.values) - 1)
        idx = _nearest_grid_index(self.price_grid, market_price)
        value = self.values[tau][idx]
        continuation = self.continuation_values[tau][idx]
        threshold = self.stop_thresholds[tau]

        should_stop = threshold is not None and market_price >= threshold
        reasons = ["market_price_above_stop_boundary"] if should_stop else ["continuation_value_dominates"]
        return ExitDecision(
            action="STOP" if should_stop else "HOLD",
            market_price=market_price,
            remaining_steps=tau,
            value=value,
            continuation_value=continuation,
            stop_threshold=threshold,
            scale_out_fraction=1.0 if should_stop else 0.0,
            reasons=reasons,
        )

    def scale_out_plan(
        self,
        remaining_steps: int,
        *,
        tranches: int = 3,
        threshold_step: float = 0.04,
    ) -> list[ScaleOutLevel]:
        """Build simple tranche thresholds above the stop boundary."""
        if tranches <= 0:
            return []
        tau = _clamp_int(remaining_steps, 0, len(self.stop_thresholds) - 1)
        base = self.stop_thresholds[tau]
        if base is None:
            return []
        fraction = 1.0 / tranches
        return [
            ScaleOutLevel(
                tranche=i + 1,
                threshold=min(1.0, round(base + threshold_step * i, 4)),
                fraction=fraction,
            )
            for i in range(tranches)
        ]


def solve_markov_optimal_stopping(
    *,
    horizon_steps: int,
    terminal_prob: float,
    price_grid: Sequence[float] | None = None,
    transition_matrix: Sequence[Sequence[float]] | None = None,
    drift_strength: float = 0.10,
    price_step: float = 0.05,
) -> OptimalStoppingPolicy:
    """Solve ``V_tau(m)=max(m, E[V_tau-1(m')])`` by backward induction.

    ``terminal_prob`` is the model's expected terminal payout for the held
    token. For a YES token it is P(YES); for a NO token it is P(NO).
    """
    if horizon_steps < 0:
        raise ValueError("horizon_steps must be non-negative")
    if not 0.0 <= terminal_prob <= 1.0:
        raise ValueError("terminal_prob must be in [0, 1]")

    grid = tuple(float(x) for x in (price_grid or _default_price_grid(price_step)))
    if not grid:
        raise ValueError("price_grid cannot be empty")
    if any(price < 0.0 or price > 1.0 for price in grid):
        raise ValueError("price_grid values must be in [0, 1]")
    if any(grid[i] >= grid[i + 1] for i in range(len(grid) - 1)):
        raise ValueError("price_grid must be strictly increasing")

    matrix = (
        _validate_transition_matrix(transition_matrix, len(grid))
        if transition_matrix is not None
        else build_binomial_transition_matrix(
            grid,
            terminal_prob=terminal_prob,
            drift_strength=drift_strength,
        )
    )

    values: list[list[float]] = [[terminal_prob for _ in grid]]
    continuation_values: list[list[float]] = [[terminal_prob for _ in grid]]
    stop_thresholds: list[float | None] = [_first_stop_threshold(grid, values[0], continuation_values[0])]

    for tau in range(1, horizon_steps + 1):
        prev = values[tau - 1]
        cont_row = [
            sum(prob * prev[j] for j, prob in enumerate(matrix[i]))
            for i in range(len(grid))
        ]
        value_row = [max(grid[i], cont_row[i]) for i in range(len(grid))]
        continuation_values.append(cont_row)
        values.append(value_row)
        stop_thresholds.append(_first_stop_threshold(grid, value_row, cont_row))

    return OptimalStoppingPolicy(
        price_grid=grid,
        values=tuple(tuple(row) for row in values),
        continuation_values=tuple(tuple(row) for row in continuation_values),
        stop_thresholds=tuple(stop_thresholds),
    )


def build_binomial_transition_matrix(
    price_grid: Sequence[float],
    *,
    terminal_prob: float,
    drift_strength: float = 0.10,
) -> tuple[tuple[float, ...], ...]:
    """Build a nearest-neighbor Markov matrix with drift toward ``terminal_prob``."""
    grid = tuple(float(x) for x in price_grid)
    rows: list[tuple[float, ...]] = []
    for i, price in enumerate(grid):
        row = [0.0 for _ in grid]
        if len(grid) == 1:
            row[0] = 1.0
            rows.append(tuple(row))
            continue

        drift = max(-0.45, min(0.45, drift_strength * (terminal_prob - price)))
        up_prob = max(0.0, min(1.0, 0.5 + drift))
        down_prob = 1.0 - up_prob

        down_idx = max(0, i - 1)
        up_idx = min(len(grid) - 1, i + 1)
        row[down_idx] += down_prob
        row[up_idx] += up_prob
        rows.append(tuple(row))
    return tuple(rows)


def _default_price_grid(step: float) -> tuple[float, ...]:
    if step <= 0 or step > 1:
        raise ValueError("price_step must be in (0, 1]")
    count = int(round(1.0 / step))
    return tuple(round(min(1.0, i * step), 6) for i in range(count + 1))


def _validate_transition_matrix(
    matrix: Sequence[Sequence[float]],
    expected_size: int,
) -> tuple[tuple[float, ...], ...]:
    if len(matrix) != expected_size:
        raise ValueError("transition_matrix row count must match price_grid")
    rows: list[tuple[float, ...]] = []
    for row in matrix:
        if len(row) != expected_size:
            raise ValueError("transition_matrix must be square")
        clean = tuple(float(value) for value in row)
        if any(value < 0.0 for value in clean):
            raise ValueError("transition probabilities cannot be negative")
        if abs(sum(clean) - 1.0) > 1e-6:
            raise ValueError("transition_matrix rows must sum to 1")
        rows.append(clean)
    return tuple(rows)


def _first_stop_threshold(
    grid: tuple[float, ...],
    values: Sequence[float],
    continuation: Sequence[float],
) -> float | None:
    for price, value, cont in zip(grid, values, continuation):
        if price >= cont and value == price:
            return price
    return None


def _nearest_grid_index(grid: tuple[float, ...], price: float) -> int:
    bounded = max(grid[0], min(grid[-1], float(price)))
    return min(range(len(grid)), key=lambda idx: abs(grid[idx] - bounded))


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))
