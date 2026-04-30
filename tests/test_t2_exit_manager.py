"""Tests for T2 exit manager.

The manager is responsible for:
 - Recording every T2 fill (incl. partials) so size_remaining and avg entry are correct
 - Triggering stop-loss / take-profit / time-stop when conditions are met
 - Submitting a single-leg SELL via ExecutionEngine on trigger
 - Optimal-stopping via the Bellman policy when enabled
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from polymarket_arb.models import (
    ArbOpportunity,
    MarketInfo,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    TokenInfo,
    TradeRecord,
    TradeStatus,
)
from polymarket_arb.strategies.t2_exit_manager import T2ExitManager

from tests.conftest import make_test_config


class _StubOB:
    def __init__(self, snapshots: dict[str, OrderBookSnapshot]):
        self._snaps = snapshots

    def get_snapshot(self, token_id: str):
        return self._snaps.get(token_id)


class _StubExecutor:
    def __init__(self):
        self.calls: list[ArbOpportunity] = []

    def execute_arbitrage(self, opp: ArbOpportunity, size: float) -> list[TradeRecord]:
        self.calls.append(opp)
        leg = opp.legs[0]
        return [
            TradeRecord(
                trade_id="exit",
                arb_id="exit",
                token_id=leg.token_id,
                condition_id=leg.condition_id,
                side=leg.side,
                price=leg.price,
                size=size,
                status=TradeStatus.FILLED,
                fill_price=leg.price,
                fill_size=size,
            )
        ]


def _market(condition_id: str = "c1", *, end_date: str = "") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question="Will Bitcoin hit $1m before GTA VI?",
        slug="btc",
        tokens=[TokenInfo(token_id="t-yes", outcome="Yes", price=0.49)],
        end_date=end_date,
    )


def _fill(token_id: str, price: float, size: float) -> TradeRecord:
    return TradeRecord(
        trade_id="t",
        arb_id="a",
        token_id=token_id,
        condition_id="c1",
        side=OrderSide.BUY,
        price=price,
        size=size,
        status=TradeStatus.FILLED,
        fill_price=price,
        fill_size=size,
    )


def _snap(token_id: str, best_bid: float, best_ask: float | None = None) -> OrderBookSnapshot:
    if best_ask is None:
        best_ask = best_bid + 0.01
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bids=[OrderBookLevel(best_bid, 100)],
        asks=[OrderBookLevel(best_ask, 100)],
    )


def test_register_then_stop_loss_triggers_sell():
    executor = _StubExecutor()
    config = make_test_config(
        t2_stop_loss_bps=300.0,
        t2_take_profit_capture_pct=10.0,  # effectively disabled
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.45)})  # entry 0.50 -> -1000 bps
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )

    assert "t-yes" in mgr.open_positions
    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "stop_loss"
    assert executor.calls and executor.calls[0].legs[0].side == OrderSide.SELL


def test_take_profit_triggers_when_capture_pct_reached():
    executor = _StubExecutor()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=0.5,  # exit when captured >= 50% of dev
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    # Bought at 0.50 with deviation=0.04. TP target = 0.50 + 0.5*0.04 = 0.52.
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.525)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.54, "deviation": 0.04},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "take_profit"


def test_time_stop_triggers_after_max_hold():
    executor = _StubExecutor()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=1.0,  # 1 second
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    # Backdate the entry timestamp so time-stop triggers.
    mgr.open_positions["t-yes"]  # ensure it exists
    pos = mgr._positions["t-yes"]  # noqa: SLF001
    pos.entry_ts = time.time() - 5.0

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "time_stop"


def test_hold_signal_does_not_trigger_sell():
    executor = _StubExecutor()
    config = make_test_config(
        t2_stop_loss_bps=300.0,
        t2_take_profit_capture_pct=0.6,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    # Bought at 0.50, market at 0.495 — small adverse move (100 bps), under stop
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.495)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 0
    assert "t-yes" in mgr.open_positions
    assert executor.calls == []


def test_partial_fills_average_entry():
    executor = _StubExecutor()
    config = make_test_config(t2_exit_eval_interval_sec=999.0)
    ob = _StubOB({})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.52, size=2.0)],
    )
    pos = mgr.open_positions["t-yes"]
    assert pos.size_remaining == pytest.approx(4.0)
    assert pos.entry_price == pytest.approx(0.51)


def test_only_buy_fills_register():
    executor = _StubExecutor()
    config = make_test_config()
    mgr = T2ExitManager(
        config=config, executor=executor, ob_analyzer=_StubOB({})
    )
    sell = TradeRecord(
        trade_id="s",
        arb_id="s",
        token_id="t-yes",
        condition_id="c1",
        side=OrderSide.SELL,
        price=0.5,
        size=1.0,
        status=TradeStatus.FILLED,
        fill_price=0.5,
        fill_size=1.0,
    )
    mgr.register_fills(
        signal_payload={"action": "BUY_YES"},
        market=_market(),
        trades=[sell],
    )
    assert mgr.open_positions == {}
