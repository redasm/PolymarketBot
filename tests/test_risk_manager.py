"""RiskManager 单元测试：成功/失败执行的记账语义."""

from __future__ import annotations

import polymarket_arb.risk_manager as risk_manager_module
import pytest

from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    MarketInfo,
    OrderSide,
    PositionSnapshot,
    RiskState,
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


def test_record_execution_only_updates_failure_state_on_full_success():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FILLED, economic_cost=0.45),
        TradeRecord("t2", "a1", "no", "c1", OrderSide.BUY, 0.50, 5, status=TradeStatus.FILLED, economic_cost=0.50),
    ]

    mgr.record_execution(opp, trades)

    assert mgr.state.daily_pnl == 0
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


def test_sell_fill_releases_market_exposure() -> None:
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    mgr.record_execution(
        opp,
        [TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FILLED, economic_cost=0.45)],
    )

    mgr.record_execution(
        opp,
        [
            TradeRecord(
                "t2",
                "a2",
                "yes",
                "c1",
                OrderSide.SELL,
                0.44,
                5,
                status=TradeStatus.FILLED,
                economic_cost=0.45,
                fill_size=5,
            )
        ],
    )

    assert mgr.state.total_exposure == pytest.approx(0.0)
    assert mgr.state.open_positions == 0


def test_release_market_exposure_reduces_open_position_count() -> None:
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    mgr.record_execution(
        opp,
        [TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FILLED, economic_cost=0.45)],
    )

    mgr.release_market_exposure("c1", 0.45 * 5)

    assert mgr.state.total_exposure == pytest.approx(0.0)
    assert mgr.state.open_positions == 0


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
    assert mgr.state.total_exposure == pytest.approx(0.45 * 2)
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


def test_full_success_does_not_book_expected_profit_without_realized_pnl():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trades = [
        TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.FILLED, economic_cost=0.45, fill_size=2),
        TradeRecord("t2", "a1", "no", "c1", OrderSide.BUY, 0.50, 5, status=TradeStatus.FILLED, economic_cost=0.50, fill_size=3),
    ]

    mgr.record_execution(opp, trades)

    assert mgr.state.daily_pnl == 0


def test_record_settlement_books_realized_pnl():
    mgr = RiskManager(make_test_config())

    mgr.record_settlement("c1", 1.25)

    assert mgr.state.daily_pnl == 1.25


def test_sync_portfolio_snapshot_overwrites_real_positions_and_realized_daily_pnl():
    mgr = RiskManager(make_test_config())
    positions = [
        PositionSnapshot(
            token_id="yes-token",
            condition_id="c1",
            outcome="Yes",
            size=3,
            avg_price=0.4,
            current_value=1.5,
            unrealized_pnl=0.3,
        ),
        PositionSnapshot(
            token_id="no-token",
            condition_id="c2",
            outcome="No",
            size=2,
            avg_price=0.6,
            current_value=1.1,
            unrealized_pnl=-0.1,
        ),
    ]

    mgr.sync_portfolio_snapshot(positions, realized_daily_pnl=2.25, synced_at=1234.0)

    assert mgr.state.daily_pnl == 2.25
    assert mgr.state.unrealized_pnl == pytest.approx(0.2)
    assert mgr.state.total_pnl == pytest.approx(2.45)
    assert mgr.state.current_position_value == pytest.approx(2.6)
    assert mgr.state.open_positions == 2
    assert mgr.state.total_exposure == (3 * 0.4) + (2 * 0.6)
    assert mgr.state.last_portfolio_sync_ts == 1234.0
    assert mgr.state.portfolio_sync_ok is True
    assert len(mgr.state.positions) == 2


def test_partial_fill_with_pending_increments_failure_counter():
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


def test_pre_trade_check_exposes_structured_reject_context(monkeypatch):
    base_time = 1_000.0
    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time)
    mgr = RiskManager(make_test_config(risk_event_cooldown_sec=60.0))
    opp = _make_opp()
    mgr.record_execution(
        opp,
        [TradeRecord("t1", "a1", "yes", "c1", OrderSide.BUY, 0.45, 5, status=TradeStatus.PENDING, economic_cost=0.45)],
    )

    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time + 1.0)
    can_trade, reason, _ = mgr.pre_trade_check(opp, 1)

    assert can_trade is False
    assert "已执行过套利" in reason
    assert mgr.last_reject["reason_code"] == "event_cooldown"
    assert mgr.last_reject["reason_context"]["event_id"] == "e1"


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


def test_post_only_pending_order_does_not_consume_open_position_slot():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trade = TradeRecord(
        "t1",
        "a1",
        "yes",
        "c1",
        OrderSide.BUY,
        0.45,
        5,
        status=TradeStatus.PENDING,
        economic_cost=0.45,
        post_only=True,
        order_type_name="GTC",
    )

    mgr.record_execution(opp, [trade], count_pending_as_failure=False)

    assert mgr.state.total_exposure == 0.45 * 5
    assert mgr.state.open_positions == 0


def test_post_only_partial_order_starts_consuming_open_position_slot():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trade = TradeRecord(
        "t1",
        "a1",
        "yes",
        "c1",
        OrderSide.BUY,
        0.45,
        5,
        status=TradeStatus.PENDING,
        order_id="oid-1",
        economic_cost=0.45,
        post_only=True,
        order_type_name="GTC",
    )

    mgr.record_execution(opp, [trade], count_pending_as_failure=False)
    assert mgr.state.open_positions == 0

    trade.status = TradeStatus.PARTIAL
    trade.fill_size = 2.0
    mgr.reconcile_pending_order_statuses([trade])

    assert mgr.state.open_positions == 1


def test_reconcile_pending_order_statuses_releases_cancelled_order():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trade = TradeRecord(
        "t1",
        "a1",
        "yes",
        "c1",
        OrderSide.BUY,
        0.45,
        5,
        status=TradeStatus.PENDING,
        order_id="oid-1",
        economic_cost=0.45,
    )

    mgr.record_execution(opp, [trade], count_pending_as_failure=False)
    assert mgr.state.total_exposure == 0.45 * 5

    trade.status = TradeStatus.CANCELLED
    mgr.reconcile_pending_order_statuses([trade])

    assert mgr.state.total_exposure == 0.0
    assert mgr.state.open_positions == 0


def test_reconcile_pending_order_statuses_keeps_partial_order_reserved(monkeypatch):
    base_time = 1_000.0
    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time)
    mgr = RiskManager(make_test_config(risk_pending_reservation_ttl_sec=30.0))
    opp = _make_opp()
    trade = TradeRecord(
        "t1",
        "a1",
        "yes",
        "c1",
        OrderSide.BUY,
        0.45,
        5,
        status=TradeStatus.PENDING,
        order_id="oid-1",
        economic_cost=0.45,
    )

    mgr.record_execution(opp, [trade], count_pending_as_failure=False)
    trade.status = TradeStatus.PARTIAL
    trade.fill_size = 2.0
    mgr.reconcile_pending_order_statuses([trade])

    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time + 20.0)
    mgr.reconcile_pending_order_statuses([trade])
    assert mgr.state.total_exposure == 0.45 * 5

    monkeypatch.setattr(risk_manager_module.time, "time", lambda: base_time + 35.0)
    assert mgr.state.total_exposure == 0.45 * 5


def test_reconcile_pending_order_statuses_converts_filled_order_to_actual_exposure():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trade = TradeRecord(
        "t1",
        "a1",
        "yes",
        "c1",
        OrderSide.BUY,
        0.45,
        5,
        status=TradeStatus.PENDING,
        order_id="oid-1",
        economic_cost=0.45,
    )

    mgr.record_execution(opp, [trade], count_pending_as_failure=False)
    trade.status = TradeStatus.FILLED
    trade.fill_size = 3.0
    mgr.reconcile_pending_order_statuses([trade])

    assert mgr.state.total_exposure == 0.45 * 3
    assert mgr.state.open_positions == 1


def test_reconcile_pending_order_statuses_keeps_partial_fill_after_cancel():
    mgr = RiskManager(make_test_config())
    opp = _make_opp()
    trade = TradeRecord(
        "t1",
        "a1",
        "yes",
        "c1",
        OrderSide.BUY,
        0.45,
        5,
        status=TradeStatus.PENDING,
        order_id="oid-1",
        economic_cost=0.45,
    )

    mgr.record_execution(opp, [trade], count_pending_as_failure=False)
    trade.status = TradeStatus.PARTIAL
    trade.fill_size = 2.0
    mgr.reconcile_pending_order_statuses([trade])
    trade.status = TradeStatus.CANCELLED
    mgr.reconcile_pending_order_statuses([trade])

    assert mgr.state.total_exposure == pytest.approx(0.45 * 2)
    assert mgr.state.open_positions == 1


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


# --- shadow-mode daily_pnl mapping (regression: UTC day-rollover freeze) ---
#
# In dry_run, `_apply_shadow_snapshot_locked` maps the shadow ledger snapshot
# onto risk state. The fix maps the day-resetting `daily_realized_pnl` onto
# `daily_pnl` (NOT the cumulative `realized_pnl`), so `_maybe_reset_daily`'s
# zeroing is not clobbered by a cumulative value and the daily-loss breaker
# reads today's loss, not the all-time loss.

def test_shadow_snapshot_maps_daily_realized_to_daily_pnl():
    mgr = RiskManager(make_test_config())  # dry_run=True
    # Cumulative realized is large; today's realized is flat.
    mgr.update_shadow_snapshot({
        "realized_pnl": 2.3297,
        "daily_realized_pnl": 0.0,
        "unrealized_pnl": 0.5,
        "total_pnl": 2.8297,
    })
    state = mgr.state
    assert state.daily_pnl == pytest.approx(0.0)          # day value, not cumulative
    assert state.total_pnl == pytest.approx(2.8297)       # cumulative preserved
    assert state.unrealized_pnl == pytest.approx(0.5)


def test_shadow_snapshot_daily_pnl_is_not_frozen_cumulative():
    """Directly pins the observed bug value: daily_pnl must be 0, not 2.3297."""
    mgr = RiskManager(make_test_config())
    mgr.update_shadow_snapshot({
        "realized_pnl": 2.3297,
        "daily_realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": 2.3297,
    })
    assert mgr.state.daily_pnl != pytest.approx(2.3297)
    assert mgr.state.daily_pnl == pytest.approx(0.0)


def test_shadow_snapshot_without_daily_key_falls_back_to_cumulative():
    """Defensive fallback: malformed snapshot lacking the day key reverts to
    the prior (cumulative) behaviour rather than crashing."""
    mgr = RiskManager(make_test_config())
    mgr.update_shadow_snapshot({
        "realized_pnl": 1.5,
        "unrealized_pnl": 0.0,
        "total_pnl": 1.5,
    })
    assert mgr.state.daily_pnl == pytest.approx(1.5)


def test_shadow_daily_loss_breaker_uses_daily_not_cumulative():
    """The risk-relevant payoff of the fix: a big intraday loss must trip the
    daily-loss breaker even when cumulative realized is comfortably positive.

    Pre-fix, daily_pnl was overwritten with the (positive) cumulative realized,
    so the breaker never saw today's loss. The breaker gates on
    daily_pnl + min(0, unrealized_pnl), so today's realized loss alone must
    engage it."""
    mgr = RiskManager(make_test_config(max_daily_loss=5.0))
    mgr.update_shadow_snapshot({
        "realized_pnl": -8.0,        # cumulative incl. today
        "daily_realized_pnl": -8.0,  # today's loss exceeds the $5 line
        "unrealized_pnl": 0.0,
        "total_pnl": -8.0,
    })
    state = mgr.state
    assert state.daily_pnl == pytest.approx(-8.0)
    can, reason = state.check_can_trade(
        max_positions=10,
        max_total_exposure=1000.0,
        max_daily_loss=5.0,
        max_failures=5,
    )
    assert can is False
    assert "止损线" in reason


def test_daily_loss_breaker_resets_after_utc_rollover():
    """Regression (5.30->5.31/6.01 freeze): once daily_pnl is zeroed at the UTC
    rollover, a prior day's cumulative loss carried in total_pnl must NOT keep
    the daily-loss breaker tripped. Pre-fix `min(daily_pnl, total_pnl)` read the
    never-resetting cumulative value and froze trading indefinitely."""
    state = RiskState(
        daily_pnl=0.0,        # already zeroed by _maybe_reset_daily at midnight
        unrealized_pnl=0.0,
        total_pnl=-33.18,     # yesterday's cumulative loss, never day-reset
    )
    can, reason = state.check_can_trade(
        max_positions=10,
        max_total_exposure=1000.0,
        max_daily_loss=30.0,
        max_failures=5,
    )
    assert can is True, f"breaker should clear after rollover, got: {reason}"


def test_daily_loss_breaker_counts_unrealized_drawdown():
    """A live-mode open position bleeding unrealized PnL must count toward the
    daily-loss line even before it is closed (daily_pnl + min(0, unrealized))."""
    state = RiskState(daily_pnl=-2.0, unrealized_pnl=-4.0, total_pnl=-6.0)
    can, _ = state.check_can_trade(
        max_positions=10,
        max_total_exposure=1000.0,
        max_daily_loss=5.0,
        max_failures=5,
    )
    assert can is False  # -2 + min(0,-4) = -6 <= -5


def test_daily_loss_breaker_ignores_unrealized_gains():
    """Unrealized GAINS must not offset a realized daily loss (min(0, unreal)),
    so a paper profit can't mask a blown daily budget."""
    state = RiskState(daily_pnl=-6.0, unrealized_pnl=+10.0, total_pnl=+4.0)
    can, _ = state.check_can_trade(
        max_positions=10,
        max_total_exposure=1000.0,
        max_daily_loss=5.0,
        max_failures=5,
    )
    assert can is False  # -6 + min(0,+10) = -6 <= -5

