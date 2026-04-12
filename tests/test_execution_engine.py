"""ExecutionEngine 单元测试：订单类型、失败记录与成功判定."""

from __future__ import annotations

import sys
import types

from polymarket_arb.execution_engine import ExecutionEngine, OrderSubmissionResult
from polymarket_arb.models import ArbLeg, ArbOpportunity, ArbType, OrderSide, TradeStatus

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

    def create_order(self, order_args, options):
        return {"order": order_args, "options": options}

    def post_order(self, signed_order, orderType):
        self.last_order_type = orderType
        return {"orderID": "oid-123"}

    def cancel(self, order_id):
        self.cancelled.append(order_id)


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
    assert result.trade_status == TradeStatus.PENDING
    assert client.last_order_type == "FOK"


def test_submit_order_non_gtc_without_fill_status_stays_pending(monkeypatch):
    _install_fake_clob_modules(monkeypatch)

    class _PendingClient(_FakeClient):
        def post_order(self, signed_order, orderType):
            self.last_order_type = orderType
            return {"orderID": "oid-123", "success": True, "status": "accepted"}

    client = _PendingClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    result = engine._submit_order("token-1", OrderSide.BUY, 0.42, 5)

    assert result.trade_status == TradeStatus.PENDING


def test_submit_order_non_gtc_partial_status_maps_to_partial(monkeypatch):
    _install_fake_clob_modules(monkeypatch)

    class _PartialClient(_FakeClient):
        def post_order(self, signed_order, orderType):
            self.last_order_type = orderType
            return {"orderID": "oid-123", "success": True, "status": "partial"}

    client = _PartialClient()
    engine = ExecutionEngine(make_test_config(dry_run=False), client)

    result = engine._submit_order("token-1", OrderSide.BUY, 0.42, 5)

    assert result.trade_status == TradeStatus.PARTIAL


def test_submit_order_extracts_partial_fill_details(monkeypatch):
    _install_fake_clob_modules(monkeypatch)

    class _PartialClient(_FakeClient):
        def post_order(self, signed_order, orderType):
            self.last_order_type = orderType
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

    assert len(trades) == 2
    assert all(trade.status == TradeStatus.PARTIAL for trade in trades)
    assert all(trade.fill_size == 2.0 for trade in trades)
    assert all(trade.fill_price == 0.421 for trade in trades)


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

    assert len(trades) == 2
    assert trades[0].status == TradeStatus.FILLED
    assert trades[0].error == "hedge_incomplete"
    assert trades[0].rolled_back is False
    assert trades[1].status == TradeStatus.FAILED
    assert len(engine.trade_history) == 2
    assert engine.is_successful_execution(opp, trades) is False


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


def test_get_pnl_summary_excludes_simulated_trades_by_default():
    engine = ExecutionEngine(make_test_config(dry_run=True), _FakeClient())
    opp = _make_opp()

    engine.execute_arbitrage(opp, 5)

    assert engine.get_pnl_summary()["total_trades"] == 0
    assert engine.get_pnl_summary(include_simulated=True)["total_trades"] == 2
