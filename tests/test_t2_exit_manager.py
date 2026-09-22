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

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None, **_kwargs: Any) -> list[TradeRecord]:
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

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None, **_kwargs: Any) -> list[TradeRecord]:
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

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None, **_kwargs: Any) -> list[TradeRecord]:
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

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None, **_kwargs: Any) -> list[TradeRecord]:
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


def test_model_prob_provider_overrides_entry_prob_for_optimal_stopping():
    """The Bellman policy should use the latest provider estimate, not entry prob.

    Setup: entry prob = 0.95 (so the entry-frozen policy says HOLD
    forever at any price < ~0.95). The provider returns 0.05 (model
    now thinks the position is nearly worthless). With rolling p_t the
    Bellman threshold collapses to ~0.05 and any market price triggers
    STOP. The horizon is short (~1h) so the value function is close to
    the terminal payoff and the test isn't sensitive to option-value
    accretion at higher τ.
    """
    from datetime import datetime, timedelta, timezone

    executor = _StubExecutor()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=True,
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=1,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})

    def provider(market: MarketInfo, token_id: str) -> float:
        return 0.05  # Catastrophic update vs entry's 0.95

    mgr = T2ExitManager(
        config=config,
        executor=executor,
        ob_analyzer=ob,
        model_prob_provider=provider,
    )
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1, minutes=2)).isoformat()
    market = _market(end_date=deadline)
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.95, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    # Sanity check: without the provider, entry-frozen policy holds at p=0.95
    assert mgr.open_positions["t-yes"].model_prob_at_entry == pytest.approx(0.95)

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "optimal_stopping"
    assert result.decisions[0]["model_prob_entry"] == 0.95
    assert result.decisions[0]["model_prob_current"] == 0.05


def test_model_prob_provider_falls_back_when_returns_none():
    """Provider returning None must not corrupt position state."""
    executor = _StubExecutor()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,  # focus on prob storage, not policy
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=1,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})

    calls: list[tuple[str, str]] = []

    def provider(market: MarketInfo, token_id: str) -> float | None:
        calls.append((market.condition_id, token_id))
        return None

    mgr = T2ExitManager(
        config=config,
        executor=executor,
        ob_analyzer=ob,
        model_prob_provider=provider,
    )
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )

    mgr.evaluate(active_markets=[market])

    assert calls == [("c1", "t-yes")]
    pos = mgr.open_positions["t-yes"]
    assert pos.current_model_prob is None
    assert pos.model_prob_at_entry == pytest.approx(0.55)


def test_dynamic_stop_warms_up_then_widens_with_volatility():
    """Verify dynamic stop falls back to static while warming, then expands."""
    # Use static stop = 50 bps (tight), dynamic k=2.0, warmup=5, min=20, max=1000
    config = make_test_config(
        t2_stop_loss_bps=50.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=1,
        t2_stop_loss_dynamic_enabled=True,
        t2_stop_loss_dynamic_k=2.0,
        t2_stop_loss_min_bps=20.0,
        t2_stop_loss_max_bps=1000.0,
        t2_stop_loss_dynamic_warmup=5,
    )

    # Mutable snapshot — we'll feed the price history one tick at a time.
    snapshots: dict[str, OrderBookSnapshot] = {
        "t-yes": _snap("t-yes", best_bid=0.50, best_ask=0.52)
    }
    ob = _StubOB(snapshots)
    executor = _StubExecutor()
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.51, size=2.0)],
    )
    pos = mgr.open_positions["t-yes"]

    # Feed 3 ticks (below warmup=5). Price oscillates within static-stop
    # range so stop_loss must NOT fire — uses the fallback static 50bps.
    # Entry 0.51, bid 0.508 → 39 bps adverse, under 50 → HOLD.
    for px in [0.510, 0.509, 0.508]:
        snapshots["t-yes"] = _snap("t-yes", best_bid=px, best_ask=px + 0.02)
        result = mgr.evaluate(active_markets=[market])
        assert result.triggered == 0
    # During warmup the effective stop should be the static 50.
    assert pos.last_effective_stop_bps == 50.0

    # Feed enough additional ticks with realised vol to wake the
    # dynamic estimator. Modest oscillations (~25 bps per tick).
    for px in [0.515, 0.508, 0.516, 0.507]:
        snapshots["t-yes"] = _snap("t-yes", best_bid=px, best_ask=px + 0.02)
        mgr.evaluate(active_markets=[market])

    # Dynamic estimator now active. Effective stop should be != 50
    # (almost surely larger, because realised vol of the 7-tick window
    # × k=2.0 should exceed 50 bps).
    assert pos.last_effective_stop_bps is not None
    assert pos.last_effective_stop_bps >= 20.0  # min floor
    assert pos.last_effective_stop_bps <= 1000.0  # max ceiling


def test_dynamic_stop_disabled_uses_static_threshold():
    """With T2_STOP_LOSS_DYNAMIC_ENABLED=false, effective_stop_bps == static."""
    config = make_test_config(
        t2_stop_loss_bps=300.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=1,
        t2_stop_loss_dynamic_enabled=False,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.495, best_ask=0.505)})
    mgr = T2ExitManager(config=config, executor=_StubExecutor(), ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    mgr.evaluate(active_markets=[market])
    assert mgr.open_positions["t-yes"].last_effective_stop_bps == 300.0


def test_model_prob_provider_exception_is_swallowed():
    """A throwing provider must not break the evaluate() pass."""
    executor = _StubExecutor()
    config = make_test_config(
        t2_stop_loss_bps=300.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=True,
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=1,
    )
    # entry 0.50, bid 0.45 → stop_loss fires regardless of provider
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.45)})

    def provider(_market: MarketInfo, _token_id: str) -> float:
        raise RuntimeError("simulated detector failure")

    mgr = T2ExitManager(
        config=config,
        executor=executor,
        ob_analyzer=ob,
        model_prob_provider=provider,
    )
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )

    # Should not raise; stop_loss still fires.
    result = mgr.evaluate(active_markets=[market])
    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "stop_loss"


class _SizeTrackingExecutor:
    """Records the per-call size and fills exactly the requested size."""

    def __init__(self):
        self.calls: list[float] = []
        self.order_type_names: list[str | None] = []

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None, **_kwargs: Any) -> list[TradeRecord]:
        self.calls.append(float(size))
        self.order_type_names.append(order_type_name)
        leg = opp.legs[0]
        return [
            TradeRecord(
                trade_id=f"exit-{len(self.calls)}",
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


def test_scale_out_take_profit_sells_one_third_per_trigger():
    """With tranches=3, take_profit should sell ~1/3 per trigger over 3 cycles."""
    executor = _SizeTrackingExecutor()
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=0.5,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=3,
    )
    # Bought at 0.50 with dev=0.04 → TP target 0.52; market at 0.525 keeps firing
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.525)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.54, "deviation": 0.04},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=9.0)],
    )

    # Cycle 1: 1/3 of 9.0 = 3.0
    r1 = mgr.evaluate(active_markets=[market])
    assert r1.tranche_exits == 1
    assert r1.triggered == 0
    assert executor.calls[-1] == pytest.approx(3.0)
    assert mgr.open_positions["t-yes"].size_remaining == pytest.approx(6.0)
    assert mgr.open_positions["t-yes"].tranches_executed == 1

    # Cycle 2: 1/2 of 6.0 = 3.0
    r2 = mgr.evaluate(active_markets=[market])
    assert r2.tranche_exits == 1
    assert executor.calls[-1] == pytest.approx(3.0)
    assert mgr.open_positions["t-yes"].size_remaining == pytest.approx(3.0)
    assert mgr.open_positions["t-yes"].tranches_executed == 2

    # Cycle 3: final tranche zeroes size_remaining → status "exited"
    # (and not "tranche_exited", since the position is fully closed).
    r3 = mgr.evaluate(active_markets=[market])
    assert r3.triggered == 1
    assert r3.tranche_exits == 0
    assert executor.calls[-1] == pytest.approx(3.0)
    assert "t-yes" not in mgr.open_positions


def test_scale_out_stop_loss_still_full_exit():
    """stop_loss must bypass scale-out and dump the full remaining position."""
    executor = _SizeTrackingExecutor()
    config = make_test_config(
        t2_stop_loss_bps=300.0,
        t2_take_profit_capture_pct=10.0,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=3,  # tranches set, but stop_loss should override
    )
    # entry 0.50, mid 0.45 → -1000 bps adverse
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.45)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=9.0)],
    )

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.tranche_exits == 0
    assert executor.calls == [pytest.approx(9.0)]
    assert result.decisions[0]["reason"] == "stop_loss"
    assert result.decisions[0]["fraction"] == 1.0


def test_scale_out_partial_fill_does_not_advance_counter():
    """A partial fill at a tranche slice should retry next cycle, not skip."""
    executor = _PartialExitExecutor()  # fills 50% of requested
    config = make_test_config(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=0.5,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
        t2_scale_out_tranches=3,
    )
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.525)})
    mgr = T2ExitManager(config=config, executor=executor, ob_analyzer=ob)
    market = _market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.54, "deviation": 0.04},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=9.0)],
    )

    result = mgr.evaluate(active_markets=[market])

    # Asked for 3.0, only got 1.5. Counter did NOT advance — next cycle
    # will retry the same tranche on the now-7.5 remaining position.
    assert result.partial == 1
    assert result.tranche_exits == 0
    assert mgr.open_positions["t-yes"].tranches_executed == 0
    assert mgr.open_positions["t-yes"].size_remaining == pytest.approx(7.5)


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


# ---------- _sell_fill_size fallback (BUG-A) ---------------------------------


def _sell_trade(token_id: str, *, status: TradeStatus, size: float, fill_size):
    return TradeRecord(
        trade_id="x",
        arb_id="a",
        token_id=token_id,
        condition_id="c",
        side=OrderSide.SELL,
        price=0.5,
        size=size,
        status=status,
        fill_size=fill_size,
    )


def test_sell_fill_size_filled_with_known_fill():
    from polymarket_arb.strategies.t2_exit_manager import _sell_fill_size

    trades = [_sell_trade("tok", status=TradeStatus.FILLED, size=100.0, fill_size=100.0)]
    assert _sell_fill_size(trades, "tok") == pytest.approx(100.0)


def test_sell_fill_size_filled_without_fill_falls_back_to_full():
    # FILLED + unknown fill_size: the order fully matched, so crediting the
    # full requested size is correct.
    from polymarket_arb.strategies.t2_exit_manager import _sell_fill_size

    trades = [_sell_trade("tok", status=TradeStatus.FILLED, size=100.0, fill_size=None)]
    assert _sell_fill_size(trades, "tok") == pytest.approx(100.0)


def test_sell_fill_size_partial_with_known_fill_uses_actual():
    from polymarket_arb.strategies.t2_exit_manager import _sell_fill_size

    trades = [_sell_trade("tok", status=TradeStatus.PARTIAL, size=100.0, fill_size=30.0)]
    assert _sell_fill_size(trades, "tok") == pytest.approx(30.0)


def test_sell_fill_size_partial_without_fill_is_zero_not_full():
    # BUG-A regression: PARTIAL + unknown fill_size must NOT credit the full
    # request (that would orphan the on-chain remainder). Conservatively 0.
    from polymarket_arb.strategies.t2_exit_manager import _sell_fill_size

    trades = [_sell_trade("tok", status=TradeStatus.PARTIAL, size=100.0, fill_size=None)]
    assert _sell_fill_size(trades, "tok") == pytest.approx(0.0)


def _updown_market() -> MarketInfo:
    """A market carrying the real crypto UPDOWN fee schedule (rate 0.07)."""
    market = _market()
    market.raw = {"feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True}}
    return market


def _tp_config(**overrides):
    base = dict(
        t2_stop_loss_bps=99999.0,
        t2_take_profit_capture_pct=0.6,
        t2_max_hold_sec=99999.0,
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    base.update(overrides)
    return make_test_config(**base)


def test_take_profit_does_not_fire_below_round_trip_fee():
    """`deviation * capture_pct` alone was a guaranteed-loss exit.

    At rate 0.07 and p~0.5 the round trip costs ~0.035/share, but
    `T2_MIN_DEVIATION=0.05` gives a take-profit target of 0.05*0.6 = 0.03.
    Every fill that exited on that trigger booked a net loss by construction.
    """
    executor = _StubExecutor()
    # Bought at 0.50, bid now 0.53 -> captured 0.03, exactly the old target.
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.53)})
    mgr = T2ExitManager(config=_tp_config(), executor=executor, ob_analyzer=ob)
    market = _updown_market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )
    assert mgr._positions["t-yes"].taker_fee_rate == pytest.approx(0.07)  # noqa: SLF001

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 0


def test_take_profit_fires_once_capture_clears_the_fee_floor():
    executor = _StubExecutor()
    # Bid 0.55 -> captured 0.05 > fee floor (0.0175 + 0.07*0.55*0.45 ~= 0.0348).
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.55)})
    mgr = T2ExitManager(config=_tp_config(), executor=executor, ob_analyzer=ob)
    market = _updown_market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.05},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )

    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "take_profit"


def test_fee_floor_is_inert_once_the_deviation_bar_is_high_enough():
    """At T2_MIN_DEVIATION=0.08 the deviation term dominates the floor.

    0.08 * 0.6 = 0.048 > the ~0.035 round trip, so raising the entry bar
    restores the original take-profit behaviour rather than overriding it.
    """
    executor = _StubExecutor()
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.548)})
    mgr = T2ExitManager(config=_tp_config(), executor=executor, ob_analyzer=ob)
    market = _updown_market()
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.58, "deviation": 0.08},
        market=market,
        trades=[_fill("t-yes", price=0.50, size=2.0)],
    )

    # captured = 0.048, exactly the deviation target and above the fee floor.
    result = mgr.evaluate(active_markets=[market])

    assert result.triggered == 1
    assert result.decisions[0]["reason"] == "take_profit"
