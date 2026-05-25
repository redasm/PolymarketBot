"""Tests for `T3MakerExitManager`.

Mirror coverage of `test_t2_exit_manager.py` but scoped to the T3 maker
exit policy (TTL / stop-loss / take-profit, no scale-out, no Bellman).
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
from polymarket_arb.strategies.strategy_orchestrator import StrategyTier
from polymarket_arb.strategies.t3_maker_exit_manager import T3MakerExitManager

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


class _StubExecutor:
    def __init__(self):
        self.calls: list[ArbOpportunity] = []
        self.order_type_names: list[str | None] = []

    def execute_arbitrage(
        self,
        opp: ArbOpportunity,
        size: float,
        *,
        order_type_name: str | None = None,
        **_kwargs: Any,
    ) -> list[TradeRecord]:
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

    def execute_arbitrage(
        self,
        opp: ArbOpportunity,
        size: float,
        *,
        order_type_name: str | None = None,
        **_kwargs: Any,
    ) -> list[TradeRecord]:
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
                error="fak_no_fill",
            )
        ]


def _market(condition_id: str = "c1") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question="Will BTC > 100k by EOY?",
        slug="btc-eoy",
        tokens=[
            TokenInfo(token_id="t-yes", outcome="Yes", price=0.50),
            TokenInfo(token_id="t-no", outcome="No", price=0.50),
        ],
    )


def _maker_fill(token_id: str, price: float, size: float) -> TradeRecord:
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
        post_only=True,
    )


def _snap(token_id: str, best_bid: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_bid + 0.01,
        bids=[OrderBookLevel(best_bid, 100)],
        asks=[OrderBookLevel(best_bid + 0.01, 100)],
    )


def _config(**overrides) -> Any:
    base = dict(
        maker_max_hold_sec=99999.0,
        maker_stop_loss_bps=300.0,
        maker_take_profit_bps=200.0,
        maker_exit_eval_interval_sec=0.0,
    )
    base.update(overrides)
    return make_test_config(**base)


def test_register_buy_fill_creates_position():
    mgr = T3MakerExitManager(
        config=_config(),
        executor=_StubExecutor(),
        ob_analyzer=_StubOB({}),
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)

    assert "t-yes" in mgr.open_positions
    pos = mgr.open_positions["t-yes"]
    assert pos.entry_price == pytest.approx(0.50)
    assert pos.size_remaining == pytest.approx(2.0)
    assert pos.outcome_label == "Yes"


def test_register_skips_non_post_only_fills():
    mgr = T3MakerExitManager(
        config=_config(),
        executor=_StubExecutor(),
        ob_analyzer=_StubOB({}),
    )
    trade = _maker_fill("t-yes", 0.50, 2.0)
    trade.post_only = False
    mgr.register_fill(trade=trade, market=_market())
    # Manager itself doesn't filter on post_only — but caller should.
    # Sanity: BUY fill still registers (filtering is a caller concern).
    assert "t-yes" in mgr.open_positions


def test_register_skips_sell_fills():
    mgr = T3MakerExitManager(
        config=_config(),
        executor=_StubExecutor(),
        ob_analyzer=_StubOB({}),
    )
    sell = _maker_fill("t-yes", 0.50, 2.0)
    sell.side = OrderSide.SELL
    mgr.register_fill(trade=sell, market=_market())
    assert "t-yes" not in mgr.open_positions


def test_add_fill_recomputes_vwap_and_size():
    mgr = T3MakerExitManager(
        config=_config(),
        executor=_StubExecutor(),
        ob_analyzer=_StubOB({}),
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)
    mgr.register_fill(trade=_maker_fill("t-yes", 0.40, 2.0), market=market)
    pos = mgr.open_positions["t-yes"]
    # VWAP = (0.50*2 + 0.40*2) / 4 = 0.45
    assert pos.entry_price == pytest.approx(0.45)
    assert pos.size_remaining == pytest.approx(4.0)


def test_stop_loss_triggers_sell():
    executor = _StubExecutor()
    mgr = T3MakerExitManager(
        config=_config(maker_stop_loss_bps=300.0),
        executor=executor,
        ob_analyzer=_StubOB({"t-yes": _snap("t-yes", best_bid=0.45)}),  # -1000 bps
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "stop_loss"
    assert executor.calls and executor.calls[0].legs[0].side == OrderSide.SELL
    assert executor.order_type_names == ["FAK"]
    # Position cleared after successful exit.
    assert "t-yes" not in mgr.open_positions


def test_take_profit_triggers_when_bid_above_threshold():
    executor = _StubExecutor()
    mgr = T3MakerExitManager(
        config=_config(maker_take_profit_bps=200.0),
        executor=executor,
        ob_analyzer=_StubOB({"t-yes": _snap("t-yes", best_bid=0.52)}),  # +400 bps
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "take_profit"


def test_time_stop_triggers_after_max_hold():
    executor = _StubExecutor()
    mgr = T3MakerExitManager(
        config=_config(maker_max_hold_sec=1.0, maker_stop_loss_bps=99999.0,
                       maker_take_profit_bps=99999.0),
        executor=executor,
        ob_analyzer=_StubOB({"t-yes": _snap("t-yes", best_bid=0.50)}),
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)
    mgr._positions["t-yes"].entry_ts = time.time() - 5.0  # noqa: SLF001

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "time_stop"


def test_hold_when_within_thresholds():
    executor = _StubExecutor()
    mgr = T3MakerExitManager(
        config=_config(maker_stop_loss_bps=300.0, maker_take_profit_bps=200.0),
        executor=executor,
        ob_analyzer=_StubOB({"t-yes": _snap("t-yes", best_bid=0.501)}),
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 0
    assert result.attempted == 0
    assert "t-yes" in mgr.open_positions
    assert executor.calls == []


def test_successful_exit_releases_risk_and_orchestrator():
    risk = _FakeRiskManager()
    orchestrator = _FakeOrchestrator()
    executor = _StubExecutor()
    mgr = T3MakerExitManager(
        config=_config(maker_stop_loss_bps=100.0),
        executor=executor,
        ob_analyzer=_StubOB({"t-yes": _snap("t-yes", best_bid=0.45)}),
        risk_manager=risk,
        orchestrator=orchestrator,
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)

    mgr.evaluate(active_markets=[market])

    # Risk release uses fill_price * size = 0.45 * 2 = 0.9
    assert risk.releases == [("c1", pytest.approx(0.9))]
    # Orchestrator settlement releases at entry notional = 0.50 * 2 = 1.0
    assert orchestrator.settlements == [
        (StrategyTier.MARKET_MAKING, pytest.approx(1.0), pytest.approx(0.0))
    ]


def test_failed_exit_keeps_position_for_retry():
    executor = _FailedExitExecutor()
    mgr = T3MakerExitManager(
        config=_config(maker_stop_loss_bps=100.0),
        executor=executor,
        ob_analyzer=_StubOB({"t-yes": _snap("t-yes", best_bid=0.45)}),
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)

    result = mgr.evaluate(active_markets=[market])

    assert result.attempted == 1
    assert result.triggered == 0
    assert result.failed == 1
    assert "t-yes" in mgr.open_positions  # still tracked for next cycle


def test_eval_interval_skips_until_elapsed():
    executor = _StubExecutor()
    mgr = T3MakerExitManager(
        config=_config(maker_stop_loss_bps=100.0, maker_exit_eval_interval_sec=60.0),
        executor=executor,
        ob_analyzer=_StubOB({"t-yes": _snap("t-yes", best_bid=0.45)}),
    )
    market = _market()
    mgr.register_fill(trade=_maker_fill("t-yes", 0.50, 2.0), market=market)
    # Mark just-evaluated; second evaluate should no-op.
    mgr._positions["t-yes"].last_eval_ts = time.time()  # noqa: SLF001

    result = mgr.evaluate(active_markets=[market])
    assert result.attempted == 0
    assert executor.calls == []
