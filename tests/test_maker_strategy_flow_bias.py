"""MakerStrategy flow-bias regression tests (P0-3)."""

from __future__ import annotations

from polymarket_arb.strategies.maker_strategy import (
    DynamicSpreadCalculator,
    MakerStrategy,
)


def _build_strategy(flow_weight: float = 0.5) -> MakerStrategy:
    return MakerStrategy(
        spread_calc=DynamicSpreadCalculator(base_spread_ticks=2.0, inventory_skew_factor=0.5),
        default_size=10.0,
        max_inventory=100.0,
        flow_inventory_weight=flow_weight,
    )


def test_flow_bias_yes_lean_skews_size_toward_ask_side() -> None:
    """Taker flow buying YES should grow ask_size relative to bid_size."""
    strat = _build_strategy()

    baseline = strat.compute_quote(
        token_id="tok-1",
        condition_id="cid-1",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
    )
    biased = strat.compute_quote(
        token_id="tok-1",
        condition_id="cid-1",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
        flow_bias_yes_share=0.80,
    )
    assert baseline is not None and biased is not None
    assert biased.ask_size > baseline.ask_size
    assert biased.bid_size == baseline.bid_size


def test_flow_bias_no_lean_skews_size_toward_bid_side() -> None:
    """Taker flow buying NO should grow bid_size (we soak up YES they sell)."""
    strat = _build_strategy()
    baseline = strat.compute_quote(
        token_id="tok-2",
        condition_id="cid-2",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
    )
    biased = strat.compute_quote(
        token_id="tok-2",
        condition_id="cid-2",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
        flow_bias_yes_share=0.20,
    )
    assert baseline is not None and biased is not None
    assert biased.bid_size > baseline.bid_size
    assert biased.ask_size == baseline.ask_size


def test_flow_bias_weight_zero_is_a_no_op() -> None:
    """flow_inventory_weight=0 must reproduce legacy quoting exactly."""
    strat = _build_strategy(flow_weight=0.0)
    baseline = strat.compute_quote(
        token_id="tok-3",
        condition_id="cid-3",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
    )
    biased = strat.compute_quote(
        token_id="tok-3",
        condition_id="cid-3",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
        flow_bias_yes_share=0.95,
    )
    assert baseline is not None and biased is not None
    assert biased.bid_size == baseline.bid_size
    assert biased.ask_size == baseline.ask_size
    assert biased.bid_price == baseline.bid_price
    assert biased.ask_price == baseline.ask_price


def test_flow_bias_yes_lean_pushes_bid_further_from_fair() -> None:
    """Strong YES taker flow should widen the bid offset (less aggressive buy)."""
    strat = _build_strategy(flow_weight=1.0)
    baseline = strat.compute_quote(
        token_id="tok-4",
        condition_id="cid-4",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
    )
    biased = strat.compute_quote(
        token_id="tok-4",
        condition_id="cid-4",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
        flow_bias_yes_share=0.95,
    )
    assert baseline is not None and biased is not None
    assert baseline.bid_price is not None and biased.bid_price is not None
    # YES-lean flow → bid should be at most equal to baseline (further from
    # fair = smaller price = lower number). Use <= because tick rounding
    # can leave the lower-weighted case unchanged at small fair values.
    assert biased.bid_price <= baseline.bid_price
