"""ExecutionEngine 单元测试：订单类型、失败记录与成功判定."""

from __future__ import annotations

import sys
import time
import types

import pytest

from polymarket_arb.execution_engine import ExecutionEngine, OrderSubmissionResult
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    TradeRecord,
    TradeStatus,
)

from tests.conftest import make_test_config


def _make_opp() -> ArbOpportunity:
    return ArbOpportunity(
        arb_type=ArbType.BINARY,
        event_id="e1",
        event_title="Test event",
        markets=[],
        total_cost=0.95,
        guaranteed_payout=1.0,
        gross_edge=0.05,
        net_edge=0.03,
        edge_pct=3.15,
        legs=[
            ArbLeg("yes", "c1", "Yes", OrderSide.BUY, 0.45, 10, 100, execution_price=0.45, economic_cost=0.45),
            ArbLeg("no", "c1", "No", OrderSide.BUY, 0.50, 10, 100, execution_price=0.50, economic_cost=0.50),
        ],
        max_executable_size=10,
    )


class _FakeClient:
    def __init__(self):
        self.last_order_type = None
        self.cancelled: list[str] = []
        self.last_post_only = None
        self.last_balance_params = None
        self.balance_response = {"balance": "100.0", "allowance": "100.0"}
        self.order_responses: dict[str, dict] = {}

    def create_order(self, order_args, options):
        return {"order": order_args, "options": options}

    def post_order(self, signed_order, orderType, post_only=False):
        self.last_order_type = orderType
        self.last_post_only = post_only
        return {"orderID": "oid-123"}

    def cancel(self, order_id):
        self.cancelled.append(order_id)

    def get_balance_allowance(self, params=None):
        self.last_balance_params = params
        return self.balance_response

    def get_order(self, order_id):
        return self.order_responses.get(order_id, {"orderID": order_id, "status": "open"})


def _install_fake_clob_modules(monkeypatch, order_type=None):
    fake_clob_types = types.ModuleType("py_clob_client.clob_types")
    fake_clob_types.OrderType = order_type or types.SimpleNamespace(GTC="GTC", FOK="FOK", FAK="FAK")

    class OrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class PartialCreateOrderOptions:
        def __init__(self):
            pass

    fake_clob_types.OrderArgs = OrderArgs
    fake_clob_types.PartialCreateOrderOptions = PartialCreateOrderOptions

    fake_constants = types.ModuleType("py_clob_client.order_builder.constants")
    fake_constants.BUY = "BUY"
    fake_constants.SELL = "SELL"

    monkeypatch.setitem(sys.modules, "py_clob_client.clob_types", fake_clob_types)
    monkeypatch.setitem(sys.modules, "py_clob_client.order_builder.constants", fake_constants)


def test_submit_order_uses_fok(monkeypatch):
    _install_fake_clob_modules(monkeypatch)

    client = _FakeClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)
    result = engine._submit_order("token-1", OrderSide.BUY, 0.42, 5)

    assert result.order_id == "oid-123"
    assert result.trade_status == TradeStatus.FAILED
    assert result.error == "non_gtc_not_filled"
    assert client.last_order_type == "FOK"
    assert client.last_post_only is False


def test_submit_order_non_gtc_without_fill_status_fails_for_taker_arb(monkeypatch):
    _install_fake_clob_modules(monkeypatch)

    class _PendingClient(_FakeClient):
        def post_order(self, signed_order, orderType, post_only=False):
            self.last_order_type = orderType
            self.last_post_only = post_only
            return {"orderID": "oid-123", "success": True, "status": "accepted"}

    client = _PendingClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    result = engine._submit_order("token-1", OrderSide.BUY, 0.42, 5)

    assert result.trade_status == TradeStatus.FAILED
    assert "non_gtc_not_filled" in result.error


def test_submit_order_non_gtc_partial_status_maps_to_partial(monkeypatch):
    _install_fake_clob_modules(monkeypatch)

    class _PartialClient(_FakeClient):
        def post_order(self, signed_order, orderType, post_only=False):
            self.last_order_type = orderType
            self.last_post_only = post_only
            return {"orderID": "oid-123", "success": True, "status": "partial"}

    client = _PartialClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    result = engine._submit_order("token-1", OrderSide.BUY, 0.42, 5)

    assert result.trade_status == TradeStatus.PARTIAL


def test_submit_order_extracts_partial_fill_details(monkeypatch):
    _install_fake_clob_modules(monkeypatch)

    class _PartialClient(_FakeClient):
        def post_order(self, signed_order, orderType, post_only=False):
            self.last_order_type = orderType
            self.last_post_only = post_only
            return {
                "orderID": "oid-123",
                "success": True,
                "status": "partial",
                "filledSize": 2.5,
                "avgPrice": 0.419,
            }

    client = _PartialClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    result = engine._submit_order("token-1", OrderSide.BUY, 0.42, 5)

    assert result.trade_status == TradeStatus.PARTIAL
    assert result.fill_size == 2.5
    assert result.fill_price == 0.419


def test_execute_arbitrage_records_partial_fill_size(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    engine = ExecutionEngine(make_test_config(dry_run=False), _FakeClient())
    opp = _make_opp()

    monkeypatch.setattr(
        engine,
        "_submit_order",
        lambda *args, **kwargs: OrderSubmissionResult(
            order_id="oid-partial",
            trade_status=TradeStatus.PARTIAL,
            fill_size=2.0,
            fill_price=0.421,
        ),
    )
    monkeypatch.setattr(engine, "_rollback_orders", lambda order_ids: set())

    trades = engine.execute_arbitrage(opp, 5)

    assert len(trades) == 4
    assert all(trade.status == TradeStatus.PARTIAL for trade in trades[:2])
    assert all(trade.fill_size == 2.0 for trade in trades[:2])
    assert all(trade.fill_price == 0.421 for trade in trades[:2])
    assert all(trade.side == OrderSide.SELL for trade in trades[2:])
    assert all("auto_flatten" in str(trade.error) for trade in trades[2:])


def test_execution_engine_validates_order_type_enum_at_init(monkeypatch):
    _install_fake_clob_modules(monkeypatch, order_type=types.SimpleNamespace())

    try:
        ExecutionEngine(make_test_config(dry_run=False), _FakeClient())
        raised = False
    except ValueError as exc:
        raised = True
        assert "OrderType" in str(exc)

    assert raised is True


def test_execute_arbitrage_records_failed_leg(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    engine = ExecutionEngine(make_test_config(dry_run=False), _FakeClient())
    opp = _make_opp()
    outcomes = iter([
        OrderSubmissionResult(order_id="oid-1", trade_status=TradeStatus.FILLED),
        RuntimeError("boom"),
    ])

    def fake_submit_order(*args, **kwargs):
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(engine, "_submit_order", fake_submit_order)
    monkeypatch.setattr(engine, "_rollback_orders", lambda order_ids: set(order_ids))

    trades = engine.execute_arbitrage(opp, 5)

    assert len(trades) == 3
    assert trades[0].status == TradeStatus.FILLED
    assert trades[0].error == "hedge_incomplete"
    assert trades[0].rolled_back is False
    assert trades[1].status == TradeStatus.FAILED
    assert trades[2].side == OrderSide.SELL
    assert "auto_flatten" in str(trades[2].error)
    assert len(engine.trade_history) == 3
    assert engine.is_successful_execution(opp, trades) is False


def test_execute_arbitrage_accepts_order_type_override(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    seen_order_types: list[str | None] = []
    engine = ExecutionEngine(make_test_config(dry_run=False), _FakeClient())
    opp = _make_opp()

    def fake_submit_order(*args, **kwargs):
        seen_order_types.append(kwargs.get("order_type"))
        return OrderSubmissionResult(order_id="oid-ok", trade_status=TradeStatus.FILLED)

    monkeypatch.setattr(engine, "_submit_order", fake_submit_order)

    trades = engine.execute_arbitrage(opp, 5, order_type_name="FAK")

    assert len(trades) == 2
    assert seen_order_types == ["FAK", "FAK"]


def test_execute_arbitrage_successful_when_all_legs_fill(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    engine = ExecutionEngine(make_test_config(dry_run=False), _FakeClient())
    opp = _make_opp()

    monkeypatch.setattr(
        engine,
        "_submit_order",
        lambda *args, **kwargs: OrderSubmissionResult(order_id="oid-ok", trade_status=TradeStatus.FILLED),
    )

    trades = engine.execute_arbitrage(opp, 5)

    assert len(trades) == 2
    assert all(trade.status == TradeStatus.FILLED for trade in trades)
    assert engine.is_successful_execution(opp, trades) is True


def test_execute_arbitrage_treats_unmatched_order_as_not_success(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    engine = ExecutionEngine(make_test_config(dry_run=False), _FakeClient())
    opp = _make_opp()

    monkeypatch.setattr(
        engine,
        "_submit_order",
        lambda *args, **kwargs: OrderSubmissionResult(order_id="oid-pending", trade_status=TradeStatus.PENDING),
    )
    monkeypatch.setattr(engine, "_rollback_orders", lambda order_ids: set(order_ids))

    trades = engine.execute_arbitrage(opp, 5)

    assert len(trades) == 2
    assert all(trade.status == TradeStatus.CANCELLED for trade in trades)
    assert engine.is_successful_execution(opp, trades) is False


def test_dry_run_marks_records_as_simulated():
    engine = ExecutionEngine(make_test_config(dry_run=True), _FakeClient())
    opp = _make_opp()

    trades = engine.execute_arbitrage(opp, 5)

    assert len(trades) == 2
    assert all(trade.simulated is True for trade in trades)
    assert all(trade.status == TradeStatus.FILLED for trade in trades)
    assert engine.trade_history == []


def test_submit_limit_order_uses_post_only_gtc(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    client = _FakeClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    trade = engine.submit_limit_order(
        token_id="token-1",
        condition_id="cond-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
        order_type_name="GTC",
    )

    assert trade.status == TradeStatus.PENDING
    assert client.last_order_type == "GTC"
    assert client.last_post_only is True


def test_submit_limit_order_v2_passes_post_only(monkeypatch):
    fake_v2 = types.ModuleType("py_clob_client_v2")
    fake_v2.OrderType = types.SimpleNamespace(GTC="GTC", FOK="FOK", FAK="FAK")
    fake_v2.Side = types.SimpleNamespace(BUY="BUY", SELL="SELL")

    class OrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class PartialCreateOrderOptions:
        def __init__(self, tick_size=None, neg_risk=None):
            self.tick_size = tick_size
            self.neg_risk = neg_risk

    fake_v2.OrderArgs = OrderArgs
    fake_v2.PartialCreateOrderOptions = PartialCreateOrderOptions
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_v2)

    class _FakeV2Client:
        def __init__(self):
            self.last_post_only = None
            self.last_order_type = None

        def create_and_post_order(self, *, order_args, options, order_type, post_only=False):
            self.last_post_only = post_only
            self.last_order_type = order_type
            return {"orderID": "oid-v2", "success": True, "status": "open"}

    client = _FakeV2Client()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    trade = engine.submit_limit_order(
        token_id="token-1",
        condition_id="cond-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
        order_type_name="GTC",
    )

    assert trade.status == TradeStatus.PENDING
    assert client.last_order_type == "GTC"
    assert client.last_post_only is True


def test_submit_limit_order_dry_run_post_only_stays_pending():
    engine = ExecutionEngine(make_test_config(dry_run=True), _FakeClient())

    trade = engine.submit_limit_order(
        token_id="token-1",
        condition_id="cond-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
    )

    assert trade.simulated is True
    assert trade.status == TradeStatus.PENDING


def test_dry_run_post_only_fill_keeps_virtual_context():
    class _Emitter:
        def __init__(self):
            self.rows = []

        def record_fill(self, trade, **kwargs):
            self.rows.append((trade, kwargs))

    engine = ExecutionEngine(make_test_config(dry_run=True), _FakeClient())
    emitter = _Emitter()
    engine.set_virtual_fill_emitter(emitter)

    trade = engine.submit_limit_order(
        token_id="token-1",
        condition_id="cond-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
        virtual_fill_context={
            "signal_id": "sig-1",
            "execution_id": "exe-1",
            "maker_side": "buy_yes",
            "signal_type": "maker_quote",
            "event_title": "Test market",
        },
    )

    assert trade.status == TradeStatus.PENDING
    assert emitter.rows == []

    snap = OrderBookSnapshot(
        token_id="token-1",
        best_ask=0.40,
        asks=[OrderBookLevel(price=0.40, size=10.0)],
    )
    filled = engine.sweep_simulated_maker_fills(lambda _token_id: snap, now_ts=trade.timestamp + 10.0)

    assert filled == [trade]
    assert len(emitter.rows) == 1
    _, kwargs = emitter.rows[0]
    assert kwargs["tier"] == "T3_MAKER"
    assert kwargs["signal_context"]["maker_side"] == "buy_yes"
    assert kwargs["signal_context"]["signal_type"] == "maker_quote"
    assert kwargs["signal_context"]["event_title"] == "Test market"


def test_ensure_sufficient_collateral_uses_balance_allowance(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    client = _FakeClient()
    client.balance_response = {"balance": "25.0", "allowance": "20.0"}
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    ok, reason, available = engine.ensure_sufficient_collateral(15.0)

    assert ok is True
    assert reason == ""
    assert available == 20.0


def test_dry_run_balance_observation_does_not_call_private_endpoint():
    class _Client:
        called = False

        def get_balance_allowance(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("dry-run must not call private balance endpoint")

    client = _Client()
    engine = ExecutionEngine(make_test_config(dry_run=True), client)

    assert engine.get_available_collateral_balance() is None
    assert client.called is False


def test_ensure_sufficient_collateral_rejects_when_balance_too_low(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    client = _FakeClient()
    client.balance_response = {"balance": "5.0", "allowance": "4.0"}
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    ok, reason, available = engine.ensure_sufficient_collateral(10.0)

    assert ok is False
    assert "insufficient_balance" in reason
    assert available == 4.0


def test_ensure_sufficient_collateral_converts_raw_usdc_units(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    client = _FakeClient()
    client.balance_response = {
        "balance": "9952392",
        "allowance": "115792089237316195423570985008687907853269984665640564039457584007913129639935",
    }
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    ok, reason, available = engine.ensure_sufficient_collateral(9.0)

    assert ok is True
    assert reason == ""
    assert available == pytest.approx(9.952392)


def test_deposit_wallet_balance_check_uses_signature_type_3(monkeypatch):
    poly_1271 = object()
    collateral = object()

    class BalanceAllowanceParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_v2 = types.ModuleType("py_clob_client_v2")
    fake_v2.AssetType = types.SimpleNamespace(COLLATERAL=collateral)
    fake_v2.BalanceAllowanceParams = BalanceAllowanceParams
    fake_v2.OrderType = types.SimpleNamespace(GTC="GTC", FOK="FOK", FAK="FAK")
    fake_v2.SignatureTypeV2 = types.SimpleNamespace(POLY_1271=poly_1271)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_v2)

    client = _FakeClient()
    engine = ExecutionEngine(
        make_test_config(dry_run=False, clob_client_version="v2", signature_type=3),
        client,
    )

    ok, reason, available = engine.ensure_sufficient_collateral(10.0)

    assert ok is True
    assert reason == ""
    assert available == 100.0
    assert client.last_balance_params.kwargs == {
        "asset_type": collateral,
        "signature_type": poly_1271,
    }


def test_sync_pending_trade_statuses_updates_filled_trade(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    client = _FakeClient()
    client.order_responses["oid-123"] = {
        "orderID": "oid-123",
        "status": "filled",
        "filledSize": 3.0,
        "avgPrice": 0.411,
    }
    engine = ExecutionEngine(make_test_config(dry_run=False), client)
    trade = engine.submit_limit_order(
        token_id="token-1",
        condition_id="cond-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
        order_type_name="GTC",
    )

    result = engine.sync_pending_trade_statuses()

    assert result.polled == [trade]
    assert result.changed == [trade]
    assert trade.status == TradeStatus.FILLED
    assert trade.fill_size == 3.0
    assert trade.fill_price == 0.411


def test_sync_pending_trade_statuses_keeps_open_order_without_change(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    client = _FakeClient()
    client.order_responses["oid-123"] = {"orderID": "oid-123", "status": "open"}
    engine = ExecutionEngine(make_test_config(dry_run=False), client)
    trade = engine.submit_limit_order(
        token_id="token-1",
        condition_id="cond-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
        order_type_name="GTC",
    )

    result = engine.sync_pending_trade_statuses()

    assert result.polled == [trade]
    assert result.changed == []
    assert trade.status == TradeStatus.PENDING


def test_cancel_stale_maker_orders_cancels_only_old_post_only_gtc(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    client = _FakeClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    stale_maker = engine.submit_limit_order(
        token_id="token-maker",
        condition_id="cond-maker",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
        order_type_name="GTC",
    )
    stale_maker.order_id = "oid-maker"
    stale_maker.timestamp = time.time() - 120.0

    stale_non_maker = engine.submit_limit_order(
        token_id="token-taker",
        condition_id="cond-taker",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.42,
        size=2,
        post_only=False,
        order_type_name="GTC",
    )
    stale_non_maker.order_id = "oid-taker"
    stale_non_maker.status = TradeStatus.PENDING
    stale_non_maker.timestamp = time.time() - 120.0

    recent_maker = engine.submit_limit_order(
        token_id="token-recent",
        condition_id="cond-recent",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.43,
        size=1,
        post_only=True,
        order_type_name="GTC",
    )
    recent_maker.order_id = "oid-recent"
    recent_maker.timestamp = time.time() - 10.0

    cancelled = engine.cancel_stale_maker_orders(60.0)

    assert cancelled == [stale_maker]
    assert client.cancelled == ["oid-maker"]
    assert stale_maker.status == TradeStatus.CANCELLED
    assert stale_non_maker.status == TradeStatus.PENDING
    assert recent_maker.status == TradeStatus.PENDING


def test_cancel_stale_maker_orders_skips_dry_run():
    client = _FakeClient()
    engine = ExecutionEngine(make_test_config(dry_run=True), client)

    trade = engine.submit_limit_order(
        token_id="token-maker",
        condition_id="cond-maker",
        outcome="Yes",
        side=OrderSide.BUY,
        price=0.41,
        size=3,
        post_only=True,
        order_type_name="GTC",
    )
    trade.order_id = "oid-maker"
    trade.timestamp = time.time() - 120.0

    cancelled = engine.cancel_stale_maker_orders(60.0)

    assert cancelled == []
    assert client.cancelled == []


def test_get_pnl_summary_excludes_simulated_trades_by_default():
    engine = ExecutionEngine(make_test_config(dry_run=True), _FakeClient())
    opp = _make_opp()

    engine.execute_arbitrage(opp, 5)

    assert engine.get_pnl_summary()["total_trades"] == 0
    assert engine.get_pnl_summary(include_simulated=True)["total_trades"] == 2


def test_get_pnl_summary_uses_fill_size_for_filled_cost(monkeypatch):
    _install_fake_clob_modules(monkeypatch)
    engine = ExecutionEngine(make_test_config(dry_run=False), _FakeClient())
    trade = TradeRecord(
        trade_id="t1",
        arb_id="a1",
        token_id="token",
        condition_id="cond",
        side=OrderSide.BUY,
        price=0.50,
        size=10.0,
        status=TradeStatus.FILLED,
        fill_size=4.0,
        economic_cost=0.50,
    )
    engine._trade_history.append(trade)

    assert engine.get_pnl_summary()["total_cost"] == 2.0
