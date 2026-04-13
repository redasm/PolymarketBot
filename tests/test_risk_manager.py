"""RiskManager 单元测试：成功/失败执行的记账语义."""

from __future__ import annotations

import polymarket_arb.risk_manager as risk_manager_module

from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    MarketInfo,
    OrderSide,
    TokenInfo,
    TradeRecord,
    TradeStatus,
)
from polymarket_arb.risk_manager import RiskManager

from tests.conftest import make_test_config


def _make_opp() -> ArbOpportunity:
    market = MarketInfo(
        condition_id="c1",
        question="Test?",
        slug="test",
        tokens=[TokenInfo(token_id="yes", outcome="Yes"), TokenInfo(token_id="no", outcome="No")],
        active=True,
        closed=False,
        event_id="e1",
    )
    return ArbOpportunity(
        arb_type=ArbType.BINARY,
        event_id="e1",
        event_title="Test event",
        markets=[market],
        total_cost=0.95,
        guaranteed_payout=1.0,
        gross_edge=0.05,
        net_edge=0.03,
        edge_pct=3.15,
        legs=[
            ArbLeg("yes", "c1", "Yes", OrderSide.BUY, 0.45, 5, 100, execution_price=0.45, economic_cost=0.45),
            ArbLeg("no", "c1", "No", OrderSide.BUY, 0.50, 5, 100, execution_price=0.50, economic_cost=0.50),
        ],
        max_executable_size=5,
    )


def test_record_execution_only_books_pnl_on_full_success():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FILLED, economic_cost=0.45),
        TradeRecord("t2", "a1", "no", "c1", OrderSide.BUY, 0.50, 5, status=TradeStatus.FILLED, economic_cost=0.50),
    ]

    mgr.record_execution(opp, trades)

    assert mgr.state.daily_pnl > 0
    assert mgr.state.consecutive_failures == 0


def test_partial_failure_does_not_book_expected_profit():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FILLED, economic_cost=0.45),
        TradeRecord("t2", "a1", "no", "c1", OrderSide.BUY, 0.50, 5, status=TradeStatus.FAILED, economic_cost=0.50),
    ]

    mgr.record_execution(opp, trades)

    assert mgr.state.daily_pnl == 0
    assert mgr.state.consecutive_failures == 1
    assert mgr.state.total_exposure == 0.45 * 5


def test_consecutive_failures_trigger_halt():
    mgr = RiskManager(make_test_config(max_consecutive_failures=2))
    opp = _make_opp()
    failed_trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FAILED, economic_cost=0.45),
    ]

    mgr.record_execution(opp, failed_trades)
    mgr.record_execution(opp, failed_trades)

    assert mgr.state.is_halted is True
    assert mgr.state.consecutive_failures == 2


def test_event_cooldown_is_set_after_partial_or_pending_execution():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.PENDING, economic_cost=0.45),
    ]

    mgr.record_execution(opp, trades)
    can_trade, reason, _ = mgr.pre_trade_check(opp, 1)

    assert can_trade is False
    assert "60秒内已执行过套利" in reason
    assert mgr.state.total_exposure == 0.45 * 5
    assert mgr.state.open_positions == 1


def test_pending_reservation_expires_and_releases_exposure(monkeypatch):
    base_time = 1_000.0
    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time)
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord(
            "t1",
            "a1",
            "yes",
            "c1",
            OrderSide.BUY,
            0.45,
            5,
            status=TradeStatus.PENDING,
            economic_cost=0.45,
        ),
    ]

    mgr.record_execution(opp, trades)
    assert mgr.state.total_exposure == 0.45 * 5

    monkeypatch.setattr(
        risk_manager_module.time,
        "time",
        lambda: base_time + mgr._pending_reservation_ttl_sec + 1,
    )
    state = mgr.state

    assert state.total_exposure == 0
    assert state.open_positions == 0


def test_partial_fill_exposure_is_not_released_by_pending_ttl(monkeypatch):
    base_time = 1_000.0
    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time)
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord(
            "t1",
            "a1",
            "yes",
            "c1",
            OrderSide.BUY,
            0.45,
            5,
            status=TradeStatus.PARTIAL,
            economic_cost=0.45,
            fill_size=2,
        ),
    ]

    mgr.record_execution(opp, trades)
    assert mgr.state.total_exposure == 0.45 * 2
    assert mgr.state.consecutive_failures == 1

    monkeypatch.setattr(
        risk_manager_module.time,
        "time",
        lambda: base_time + mgr._pending_reservation_ttl_sec + 1,
    )
    state = mgr.state

    assert state.total_exposure == 0.45 * 2
    assert state.open_positions == 1


def test_partial_fill_without_fill_size_does_not_assume_requested_size():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord(
            "t1",
            "a1",
            "yes",
            "c1",
            OrderSide.BUY,
            0.45,
            5,
            status=TradeStatus.PARTIAL,
            economic_cost=0.45,
            fill_size=None,
        ),
    ]

    mgr.record_execution(opp, trades)

    assert mgr.state.total_exposure == 0.0
    assert mgr.state.open_positions == 0


def test_full_success_daily_pnl_uses_fill_size_when_present():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FILLED, economic_cost=0.45, fill_size=2),
        TradeRecord("t2", "a1", "no", "c1", OrderSide.BUY, 0.50, 5, status=TradeStatus.FILLED, economic_cost=0.50, fill_size=3),
    ]

    mgr.record_execution(opp, trades)

    assert mgr.state.daily_pnl == opp.net_edge * 2


def test_partial_or_pending_execution_counts_as_failure():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.PARTIAL, economic_cost=0.45, fill_size=2),
        TradeRecord("t2", "a1", "no", "c1", OrderSide.BUY, 0.50, 5, status=TradeStatus.PENDING, economic_cost=0.50),
    ]

    mgr.record_execution(opp, trades)

    assert mgr.state.consecutive_failures == 1
    assert mgr.state.is_halted is False


def test_event_cooldown_uses_configured_duration(monkeypatch):
    base_time = 1_000.0
    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time)
    mgr = RiskManager(make_test_config(risk_event_cooldown_sec=5.0))
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.PENDING, economic_cost=0.45),
    ]

    mgr.record_execution(opp, trades)

    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time + 6.0)
    can_trade, reason, _ = mgr.pre_trade_check(opp, 1)

    assert can_trade is True
    assert reason == ""


def test_pending_reservation_ttl_uses_configured_duration(monkeypatch):
    base_time = 1_000.0
    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time)
    mgr = RiskManager(make_test_config(risk_pending_reservation_ttl_sec=300.0))
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.PENDING, economic_cost=0.45),
    ]

    mgr.record_execution(opp, trades)

    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time + 31.0)
    state = mgr.state

    assert state.total_exposure == 0.45 * 5


def test_pre_trade_check_uses_leg_exposure_per_market_in_multi_outcome():
    mgr = RiskManager(make_test_config(max_exposure_per_market=100.0))
    market_a = MarketInfo(
        condition_id="c1",
        question="Outcome A?",
        slug="a",
        tokens=[TokenInfo(token_id="a", outcome="A")],
        active=True,
        closed=False,
        event_id="e-multi",
    )
    market_b = MarketInfo(
        condition_id="c2",
        question="Outcome B?",
        slug="b",
        tokens=[TokenInfo(token_id="b", outcome="B")],
        active=True,
        closed=False,
        event_id="e-multi",
    )
    opp = ArbOpportunity(
        arb_type=ArbType.MULTI_OUTCOME,
        event_id="e-multi",
        event_title="Multi",
        markets=[market_a, market_b],
        total_cost=0.7,
        guaranteed_payout=1.0,
        gross_edge=0.3,
        net_edge=0.28,
        edge_pct=40.0,
        legs=[
            ArbLeg("a", "c1", "A", OrderSide.BUY, 0.4, 0, 100, execution_price=0.4, economic_cost=0.4),
            ArbLeg("b", "c2", "B", OrderSide.BUY, 0.3, 0, 100, execution_price=0.3, economic_cost=0.3),
        ],
        max_executable_size=100,
    )
    mgr._market_exposure["c1"] = 90.0
    mgr._market_exposure["c2"] = 50.0

    can_trade, reason, adj_size = mgr.pre_trade_check(opp, 50.0)

    assert can_trade is True, reason
    assert adj_size == 25.0
