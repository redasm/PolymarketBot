"""Tests for `ShadowPositionLifecycle.exposure_by_market_usdc`.

This helper is consumed by the shadow-mode maker-fill guard to enforce
`RISK_MAX_EXPOSURE_PER_MARKET`. It must:
 - Sum across multiple lots on the same condition_id.
 - Multiply remaining_size by open_price (entry-cost basis, matches risk).
 - Drop fully-closed lots (remaining_size <= 1e-9).
 - Skip lots whose condition_id is empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.main_helpers.shadow_position_lifecycle import ShadowPositionLifecycle
from polymarket_arb.models import OrderSide, TradeRecord, TradeStatus


def _ledger(tmp_path: Path) -> ShadowPositionLifecycle:
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=False)
    return ShadowPositionLifecycle(event_recorder=recorder)


def _buy(token_id: str, condition_id: str, price: float, size: float, *, trade_id: str = "t") -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        arb_id="a",
        token_id=token_id,
        condition_id=condition_id,
        side=OrderSide.BUY,
        price=price,
        size=size,
        status=TradeStatus.FILLED,
        fill_price=price,
        fill_size=size,
    )


def _sell(token_id: str, condition_id: str, price: float, size: float, *, trade_id: str = "s") -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        arb_id="a",
        token_id=token_id,
        condition_id=condition_id,
        side=OrderSide.SELL,
        price=price,
        size=size,
        status=TradeStatus.FILLED,
        fill_price=price,
        fill_size=size,
    )


def test_empty_when_no_fills(tmp_path: Path) -> None:
    assert _ledger(tmp_path).exposure_by_market_usdc() == {}


def test_single_lot_returns_notional(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0))
    expo = led.exposure_by_market_usdc()
    assert list(expo.keys()) == ["c1"]
    assert expo["c1"] == pytest.approx(2.0)


def test_multiple_lots_same_market_aggregate(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record_fill(_buy("t-yes", "c1", price=0.50, size=4.0, trade_id="b1"))
    led.record_fill(_buy("t-yes", "c1", price=0.40, size=2.0, trade_id="b2"))
    led.record_fill(_buy("t-no", "c1", price=0.30, size=3.0, trade_id="b3"))
    # 0.50*4 + 0.40*2 + 0.30*3 = 2.0 + 0.8 + 0.9 = 3.7
    expo = led.exposure_by_market_usdc()
    assert list(expo.keys()) == ["c1"]
    assert expo["c1"] == pytest.approx(3.7)


def test_separate_markets_keyed_by_condition_id(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0))
    led.record_fill(_buy("t2", "c2", price=0.20, size=10.0))
    expo = led.exposure_by_market_usdc()
    assert expo["c1"] == pytest.approx(2.0)
    assert expo["c2"] == pytest.approx(2.0)


def test_partial_close_reduces_exposure(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0, trade_id="b1"))
    led.record_fill(_sell("t1", "c1", price=0.60, size=1.0, trade_id="s1"))
    # Remaining 3 @ 0.50 entry = 1.5
    expo = led.exposure_by_market_usdc()
    assert expo["c1"] == pytest.approx(1.5)


def test_full_close_drops_market(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0, trade_id="b1"))
    led.record_fill(_sell("t1", "c1", price=0.60, size=4.0, trade_id="s1"))
    assert led.exposure_by_market_usdc() == {}


def test_missing_condition_id_is_skipped(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "", price=0.50, size=4.0))
    assert led.exposure_by_market_usdc() == {}


def test_snapshot_values_open_lot_at_cost_when_mark_is_missing(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.75, size=20.0), fee=0.12)

    snap = led.snapshot()

    assert snap["open_lots"] == 1
    assert snap["open_cost"] == pytest.approx(15.12)
    assert snap["current_position_value"] == pytest.approx(15.0)
    assert snap["unrealized_pnl"] == pytest.approx(-0.12)
    assert snap["unmarked_open_lots"] == 1
    assert snap["unmarked_open_cost"] == pytest.approx(15.12)
