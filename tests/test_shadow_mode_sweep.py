"""Shadow-mode maker-fill sweep tests (roadmap §三-阶段 1 / P1-2)."""

from __future__ import annotations

import time

import pytest

from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.models import (
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    TradeStatus,
)

from tests.conftest import make_test_config


class _NoopClient:
    pass


def _snap(token_id: str, *, bid: float | None, ask: float | None) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=bid,
        best_ask=ask,
        bids=[OrderBookLevel(price=bid, size=100.0)] if bid is not None else [],
        asks=[OrderBookLevel(price=ask, size=100.0)] if ask is not None else [],
    )


def _dry_run_executor() -> ExecutionEngine:
    config = make_test_config(dry_run=True)
    return ExecutionEngine(config, _NoopClient())


def test_sweep_fills_buy_maker_when_ask_crosses_limit() -> None:
    executor = _dry_run_executor()
    trade = executor.submit_limit_order(
        token_id="tok-1",
        condition_id="cid-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.50,
        size=20.0,
        post_only=True,
        order_type_name="GTC",
    )
    assert trade.status == TradeStatus.PENDING

    # Ask drops to 0.48 → BUY @ 0.50 should fill.
    snapshots = {trade.token_id: _snap(trade.token_id, bid=0.46, ask=0.48)}
    flipped = executor.sweep_simulated_maker_fills(
        lambda tid: snapshots.get(tid),
        fill_latency_sec=0.0,
    )

    assert len(flipped) == 1
    assert flipped[0].status == TradeStatus.FILLED
    assert flipped[0].fill_price == pytest.approx(0.50)
    assert flipped[0].fill_size == pytest.approx(20.0)


def test_sweep_fills_sell_maker_when_bid_crosses_limit() -> None:
    executor = _dry_run_executor()
    trade = executor.submit_limit_order(
        token_id="tok-2",
        condition_id="cid-2",
        outcome="Yes",
        side=OrderSide.SELL,
        price=0.55,
        size=10.0,
        post_only=True,
        order_type_name="GTC",
    )
    assert trade.status == TradeStatus.PENDING

    snapshots = {trade.token_id: _snap(trade.token_id, bid=0.57, ask=0.59)}
    flipped = executor.sweep_simulated_maker_fills(
        lambda tid: snapshots.get(tid),
        fill_latency_sec=0.0,
    )

    assert len(flipped) == 1
    assert flipped[0].status == TradeStatus.FILLED


def test_sweep_skips_when_book_has_not_crossed() -> None:
    executor = _dry_run_executor()
    trade = executor.submit_limit_order(
        token_id="tok-3",
        condition_id="cid-3",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.30,
        size=5.0,
        post_only=True,
        order_type_name="GTC",
    )
    snapshots = {trade.token_id: _snap(trade.token_id, bid=0.40, ask=0.42)}
    flipped = executor.sweep_simulated_maker_fills(
        lambda tid: snapshots.get(tid),
        fill_latency_sec=0.0,
    )
    assert flipped == []
    assert trade.status == TradeStatus.PENDING


def test_sweep_respects_fill_latency() -> None:
    executor = _dry_run_executor()
    trade = executor.submit_limit_order(
        token_id="tok-4",
        condition_id="cid-4",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.50,
        size=5.0,
        post_only=True,
        order_type_name="GTC",
    )
    snapshots = {trade.token_id: _snap(trade.token_id, bid=0.46, ask=0.48)}

    flipped = executor.sweep_simulated_maker_fills(
        lambda tid: snapshots.get(tid),
        fill_latency_sec=10.0,
    )
    assert flipped == []

    flipped_after = executor.sweep_simulated_maker_fills(
        lambda tid: snapshots.get(tid),
        fill_latency_sec=10.0,
        now_ts=trade.timestamp + 15.0,
    )
    assert len(flipped_after) == 1


def test_sweep_ignores_taker_pending_orders() -> None:
    executor = _dry_run_executor()
    trade = executor.submit_limit_order(
        token_id="tok-5",
        condition_id="cid-5",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.50,
        size=5.0,
        post_only=False,
        order_type_name="FOK",
    )
    # taker shadow path marks FILLED immediately
    assert trade.status == TradeStatus.FILLED

    flipped = executor.sweep_simulated_maker_fills(
        lambda tid: _snap(tid, bid=0.46, ask=0.48),
        fill_latency_sec=0.0,
    )
    assert flipped == []


def test_sweep_caps_fill_at_opposing_depth_and_marks_partial() -> None:
    """P1-3: sweep must not over-fill past visible opposing depth."""
    executor = _dry_run_executor()
    trade = executor.submit_limit_order(
        token_id="tok-6",
        condition_id="cid-6",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.50,
        size=100.0,
        post_only=True,
        order_type_name="GTC",
    )
    thin_book = OrderBookSnapshot(
        token_id=trade.token_id,
        best_bid=0.46,
        best_ask=0.48,
        bids=[OrderBookLevel(price=0.46, size=100.0)],
        asks=[OrderBookLevel(price=0.48, size=7.0)],
    )
    flipped = executor.sweep_simulated_maker_fills(
        lambda tid: thin_book if tid == trade.token_id else None,
        fill_latency_sec=0.0,
    )
    assert len(flipped) == 1
    assert flipped[0].fill_size == pytest.approx(7.0)
    assert flipped[0].status == TradeStatus.PARTIAL


def test_sweep_updates_trade_timestamp_to_fill_moment() -> None:
    """P1-2: trade.timestamp should advance to the fill moment."""
    executor = _dry_run_executor()
    trade = executor.submit_limit_order(
        token_id="tok-7",
        condition_id="cid-7",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.50,
        size=5.0,
        post_only=True,
        order_type_name="GTC",
    )
    original_ts = trade.timestamp

    later = original_ts + 30.0
    snapshots = {trade.token_id: _snap(trade.token_id, bid=0.46, ask=0.48)}
    flipped = executor.sweep_simulated_maker_fills(
        lambda tid: snapshots.get(tid),
        fill_latency_sec=0.0,
        now_ts=later,
    )
    assert len(flipped) == 1
    assert flipped[0].timestamp == pytest.approx(later)
    assert flipped[0].timestamp > original_ts
