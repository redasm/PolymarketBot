"""Optimal stopping policy tests."""

from __future__ import annotations

import pytest

from polymarket_arb.strategies.optimal_stopping import (
    build_binomial_transition_matrix,
    solve_markov_optimal_stopping,
)


def test_optimal_stopping_stops_above_boundary_and_holds_below():
    policy = solve_markov_optimal_stopping(
        horizon_steps=10,
        terminal_prob=0.60,
        price_grid=[0.0, 0.25, 0.50, 0.60, 0.70, 1.0],
        transition_matrix=[
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.4, 0.6, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.4, 0.6, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.4, 0.6, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.4, 0.6],
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        ],
    )

    low = policy.decide(remaining_steps=5, market_price=0.50)
    high = policy.decide(remaining_steps=5, market_price=1.00)

    assert low.action == "HOLD"
    assert high.action == "STOP"
    assert high.stop_threshold is not None
    assert high.scale_out_fraction == 1.0


def test_scale_out_plan_uses_stop_boundary_as_first_tranche():
    policy = solve_markov_optimal_stopping(
        horizon_steps=3,
        terminal_prob=0.58,
        price_grid=[0.0, 0.5, 0.6, 0.7, 1.0],
        drift_strength=0.0,
    )

    plan = policy.scale_out_plan(remaining_steps=2, tranches=3, threshold_step=0.05)

    assert len(plan) == 3
    assert plan[0].threshold == policy.stop_thresholds[2]
    assert [round(level.fraction, 3) for level in plan] == [0.333, 0.333, 0.333]


def test_transition_matrix_validation_rejects_bad_rows():
    with pytest.raises(ValueError, match="rows must sum"):
        solve_markov_optimal_stopping(
            horizon_steps=1,
            terminal_prob=0.5,
            price_grid=[0.0, 1.0],
            transition_matrix=[[0.8, 0.3], [0.0, 1.0]],
        )


def test_binomial_transition_matrix_rows_sum_to_one():
    matrix = build_binomial_transition_matrix([0.0, 0.5, 1.0], terminal_prob=0.6)

    assert len(matrix) == 3
    assert all(abs(sum(row) - 1.0) < 1e-9 for row in matrix)
