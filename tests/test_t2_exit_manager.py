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


@dataclass
class _FakeRiskManager:
    releases: list[tuple[str, float]] = field(default_factory=list)

    def release_market_exposure(self, condition_id: str, exposure: float) -> None:
        self.releases.append((condition_id, exposure))


@dataclass
class _FakeOrchestrator:
    settlements: list[tuple[Any, float, float]] = field(default_factory=list)

    def record_settlement(self, tier: Any, amount: float, pnl: float) -> None:
        self.settlements.append((tier, amount, pnl))


@dataclass
class _FakeNotifier:
    failures: list[dict[str, Any]] = field(default_factory=list)
    fatals: list[dict[str, Any]] = field(default_factory=list)

    def notify_trade_failure(self, **kwargs: Any) -> bool:
        self.failures.append(kwargs)
        return True

    def notify_fatal_error(self, message: str, **kwargs: Any) -> bool:
        self.fatals.append({"message": message, **kwargs})
        return True


class _StubExecutor:
    def __init__(self):
        self.calls: list[ArbOpportunity] = []
        self.order_type_names: list[str | None] = []

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None) -> list[TradeRecord]:
        self.calls.append(opp)
        self.order_type_names.append(order_type_name)
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


class _FailedExitExecutor:
    def __init__(self):
        self.calls: list[ArbOpportunity] = []

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None) -> list[TradeRecord]:
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
                status=TradeStatus.FAILED,
                error="fok_not_filled",
            )
        ]

    def is_successful_execution(self, _opp: ArbOpportunity, _trades: list[TradeRecord]) -> bool:
        return False


class _PartialExitExecutor:
    def __init__(self):
        self.calls: list[ArbOpportunity] = []

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None) -> list[TradeRecord]:
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
                status=TradeStatus.PARTIAL,
                fill_price=leg.price,
                fill_size=size / 2,
            )
        ]

    def is_successful_execution(self, _opp: ArbOpportunity, _trades: list[TradeRecord]) -> bool:
        return False


class _CancelledFilledExitExecutor:
    def __init__(self):
        self.calls: list[ArbOpportunity] = []

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None) -> list[TradeRecord]:
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
                status=TradeStatus.CANCELLED,
                fill_price=leg.price,
                fill_size=size / 2,
            )
        ]

    def is_successful_execution(self, _opp: ArbOpportunity, _trades: list[TradeRecord]) -> bool:
        return False


def _market(condition_id: str = "c1", *, end_date: str = "") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question="Will Bitcoin hit $1m before GTA VI?",
        slug="btc",
        tokens=[
            TokenInfo(token_id="t-yes", outcome="Yes", price=0.49),
            TokenInfo(token_id="t-no", outcome="No", price=0.51),
        ],
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
    assert executor.order_type_names == ["FAK"]


def test_successful_exit_releases_risk_exposure() -> None:
    executor = _StubExecutor()
    risk = _FakeRiskManager()
    orchestrator = _FakeOrchestrator()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=1.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})
    mgr = T2ExitManager(
        config=config,
        executor=executor,
        ob_analyzer=ob,
        risk_manager=risk,
        orchestrator=orchestrator,
    )
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    mgr._positions["t-yes"].entry_ts = time.time() - 5.0  # noqa: SLF001

    mgr.evaluate(active_markets=[market])

    assert risk.releases == [("c1", pytest.approx(1.0))]
    assert orchestrator.settlements[0][1] == pytest.approx(1.0)


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


def test_register_fills_derives_buy_no_from_execution_check_and_token() -> None:
    executor = _StubExecutor()
    config = make_test_config(t2_exit_eval_interval_sec=999.0)
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=_StubOB({}))
    market = _market()

    mgr.register_fills(
        signal_payload={
            "execution_check": {"action": "BUY_NO"},
            "model_prob": 0.47,
            "deviation": -0.02,
        },
        market=market,
        trades=[_fill("t-no", price=0.51, size=3.0)],
    )

    pos = mgr.open_positions["t-no"]
    assert pos.outcome_label == "No"
    assert pos.model_prob_at_entry == pytest.approx(0.53)


def test_register_fills_falls_back_to_signal_type_for_buy_no() -> None:
    executor = _StubExecutor()
    config = make_test_config(t2_exit_eval_interval_sec=999.0)
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=_StubOB({}))

    mgr.register_fills(
        signal_payload={
            "signal_type": "statistical_buy_no",
            "model_prob": 0.47,
            "deviation": -0.02,
        },
        market=MarketInfo(
            condition_id="c2",
            question="Binary market",
            slug="binary",
            tokens=[TokenInfo(token_id="unknown-no", outcome="", price=0.51)],
        ),
        trades=[_fill("unknown-no", price=0.51, size=3.0)],
    )

    pos = mgr.open_positions["unknown-no"]
    assert pos.outcome_label == "No"
    assert pos.model_prob_at_entry == pytest.approx(0.53)


def test_failed_exit_remains_open_and_records_failed_decision() -> None:
    executor = _FailedExitExecutor()
    notifier = _FakeNotifier()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=1.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob, notifier=notifier)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    mgr._positions["t-yes"].entry_ts = time.time() - 5.0  # noqa: SLF001

    result = mgr.evaluate(active_markets=[market])

    assert result.attempted == 1
    assert result.triggered == 0
    assert result.failed == 1
    assert result.decisions[0]["status"] == "exit_failed"
    assert "t-yes" in mgr.open_positions
    assert mgr.open_positions["t-yes"].next_exit_retry_ts > time.time()
    assert len(notifier.failures) == 1


def test_repeated_exit_failures_emit_fatal_after_retry_cap() -> None:
    executor = _FailedExitExecutor()
    notifier = _FakeNotifier()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=1.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob, notifier=notifier)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    mgr._positions["t-yes"].entry_ts = time.time() - 5.0  # noqa: SLF001

    for _ in range(3):
        mgr._positions["t-yes"].next_exit_retry_ts = 0.0  # noqa: SLF001
        mgr.evaluate(active_markets=[market])

    assert len(notifier.failures) == 3
    assert len(notifier.fatals) == 1


def test_partial_exit_reduces_size_but_keeps_position_open() -> None:
    executor = _PartialExitExecutor()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=1.0,
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
    mgr._positions["t-yes"].entry_ts = time.time() - 5.0  # noqa: SLF001

    result = mgr.evaluate(active_markets=[market])

    assert result.attempted == 1
    assert result.partial == 1
    assert result.decisions[0]["status"] == "partial_exit"
    assert mgr.open_positions["t-yes"].size_remaining == pytest.approx(1.0)


def test_cancelled_exit_with_fill_size_still_reduces_position() -> None:
    executor = _CancelledFilledExitExecutor()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=1.0,
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
    mgr._positions["t-yes"].entry_ts = time.time() - 5.0  # noqa: SLF001

    result = mgr.evaluate(active_markets=[market])

    assert result.partial == 1
    assert mgr.open_positions["t-yes"].size_remaining == pytest.approx(1.0)
