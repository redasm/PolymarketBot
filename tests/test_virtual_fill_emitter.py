"""VirtualFillEmitter contract test — 13 fields required by roadmap §三-阶段 1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.main_helpers.virtual_fill_emitter import VirtualFillEmitter
from polymarket_arb.models import (
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    TradeRecord,
    TradeStatus,
)


def _make_snap(token_id: str, *, bid: float = 0.49, ask: float = 0.51) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=bid,
        best_ask=ask,
        bids=[OrderBookLevel(price=bid, size=100.0)],
        asks=[OrderBookLevel(price=ask, size=100.0)],
    )


def _build_emitter(tmp_path: Path, snap: OrderBookSnapshot | None) -> tuple[VirtualFillEmitter, EventRecorder]:
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=True)
    emitter = VirtualFillEmitter(
        event_recorder=recorder,
        book_snapshot_provider=(lambda _tid: snap),
        taker_fee_rate=0.02,
    )
    return emitter, recorder


def _read_only_row(tmp_path: Path) -> dict:
    matches = list(tmp_path.glob("*.virtual_fills.ndjson"))
    assert len(matches) == 1, f"expected exactly one ndjson file, got {matches}"
    rows = matches[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    return json.loads(rows[0])


def test_record_fill_emits_full_13_field_schema(tmp_path: Path) -> None:
    snap = _make_snap("tok-1", bid=0.49, ask=0.51)
    emitter, recorder = _build_emitter(tmp_path, snap)

    trade = TradeRecord(
        trade_id="tr-1",
        arb_id="arb-1",
        token_id="tok-1",
        condition_id="cid-1",
        side=OrderSide.BUY,
        price=0.51,
        size=10.0,
        status=TradeStatus.FILLED,
        fill_price=0.515,
        fill_size=10.0,
        simulated=True,
        post_only=False,
        order_type_name="FOK",
    )
    trade.signal_id = "sig-1"
    trade.execution_id = "exe-1"
    emitter.record_fill(trade, intended_price=0.51, tier="T0_STRUCTURAL")
    recorder.close()

    row = _read_only_row(tmp_path)
    for field in (
        "fill_timestamp",
        "market_id",
        "side",
        "price",
        "size",
        "order_type",
        "is_maker",
        "fee",
        "slippage",
        "intended_price",
        "decision_context",
        "result",
        "tier",
    ):
        assert field in row, f"missing required field {field!r}"

    assert row["market_id"] == "cid-1"
    assert row["signal_id"] == "sig-1"
    assert row["execution_id"] == "exe-1"
    assert row["side"] == "BUY"
    assert row["is_maker"] is False
    assert row["tier"] == "T0_STRUCTURAL"
    # Polymarket CLOB taker fee shape: fee_rate × price × (1 - price) × filled_size.
    assert row["fee"] == pytest.approx(0.02 * 0.515 * (1 - 0.515) * 10.0, rel=1e-6)
    # Slippage = fill_price - intended_price = 0.005
    assert row["slippage"] == pytest.approx(0.005, abs=1e-6)
    assert row["decision_context"]["best_bid_at_decision"] == 0.49
    assert row["decision_context"]["best_ask_at_decision"] == 0.51
    assert row["result"]["status"] == "filled"
    assert row["result"]["filled_size"] == 10.0


def test_maker_post_only_fill_records_zero_fee(tmp_path: Path) -> None:
    emitter, recorder = _build_emitter(tmp_path, _make_snap("tok-2"))
    trade = TradeRecord(
        trade_id="tr-2",
        arb_id="arb-2",
        token_id="tok-2",
        condition_id="cid-2",
        side=OrderSide.SELL,
        price=0.55,
        size=20.0,
        status=TradeStatus.PENDING,
        fill_price=None,
        fill_size=None,
        simulated=True,
        post_only=True,
        order_type_name="GTC",
    )
    emitter.record_fill(trade, intended_price=0.55, tier="T3_MAKER")
    recorder.close()

    row = _read_only_row(tmp_path)
    assert row["is_maker"] is True
    assert row["fee"] == 0.0
    assert row["result"]["status"] == "pending"
    assert row["result"]["avg_fill_price"] is None


def test_unfilled_taker_order_pays_zero_fee(tmp_path: Path) -> None:
    """P1-1: fee must be charged on actual filled size, not the request size."""
    emitter, recorder = _build_emitter(tmp_path, _make_snap("tok-3"))
    trade = TradeRecord(
        trade_id="tr-3",
        arb_id="arb-3",
        token_id="tok-3",
        condition_id="cid-3",
        side=OrderSide.BUY,
        price=0.51,
        size=10.0,
        status=TradeStatus.FAILED,
        fill_price=None,
        fill_size=None,
        simulated=True,
        post_only=False,
        order_type_name="FOK",
        error="no liquidity",
    )
    emitter.record_fill(trade, intended_price=0.51, tier="T0_STRUCTURAL")
    recorder.close()

    row = _read_only_row(tmp_path)
    assert row["is_maker"] is False
    assert row["fee"] == 0.0
    assert row["result"]["status"] == "failed"
    assert row["result"]["filled_size"] == 0.0


def test_partial_fill_fee_uses_actual_filled_size(tmp_path: Path) -> None:
    """P1-1: a partial fill should pay fee on `fill_size`, not request `size`."""
    emitter, recorder = _build_emitter(tmp_path, _make_snap("tok-4"))
    trade = TradeRecord(
        trade_id="tr-4",
        arb_id="arb-4",
        token_id="tok-4",
        condition_id="cid-4",
        side=OrderSide.BUY,
        price=0.51,
        size=100.0,
        status=TradeStatus.PARTIAL,
        fill_price=0.51,
        fill_size=7.0,
        simulated=True,
        post_only=False,
        order_type_name="FAK",
    )
    emitter.record_fill(trade, intended_price=0.51, tier="T2_STAT")
    recorder.close()

    row = _read_only_row(tmp_path)
    # fee uses the actual filled size, not request size.
    assert row["fee"] == pytest.approx(0.02 * 0.51 * (1 - 0.51) * 7.0, rel=1e-6)
    assert row["result"]["filled_size"] == pytest.approx(7.0)


def test_disabled_recorder_emits_nothing(tmp_path: Path) -> None:
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=False)
    emitter = VirtualFillEmitter(
        event_recorder=recorder,
        book_snapshot_provider=None,
        taker_fee_rate=0.02,
    )
    trade = TradeRecord(
        trade_id="t",
        arb_id="a",
        token_id="x",
        condition_id="c",
        side=OrderSide.BUY,
        price=0.5,
        size=1.0,
        status=TradeStatus.FILLED,
    )
    emitter.record_fill(trade, intended_price=0.5)
    assert list(tmp_path.iterdir()) == []
