"""Tests for `polymarket_arb.main_helpers.t0_execution.execute_t0_opportunity`.

Pins the per-opportunity gating chain:

- Always: notify chat, log opportunity text.
- Bail with stable reason when depth verification fails.
- Bail with stable reason when risk pre-trade check fails.
- Bail with stable reason when collateral check fails.
- On success: returns ExecutionDelta with the right counter set,
  reconciles risk only for live trades, mirrors trades to dashboard,
  records trade entries to event_recorder, and notifies success/failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from polymarket_arb.main_helpers.strategy_execution import ExecutionDelta
from polymarket_arb.main_helpers.t0_execution import execute_t0_opportunity
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    OrderSide,
    TradeRecord,
    TradeStatus,
)


# ---------- shared stubs -----------------------------------------------------


class _Recorder:
    is_enabled = True

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write_event(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


class _DashState:
    def __init__(self) -> None:
        self.opportunities: list[dict] = []
        self.trades: list[dict] = []

    def append_opportunity(self, payload: dict) -> None:
        self.opportunities.append(payload)

    def append_trade(self, payload: dict) -> None:
        self.trades.append(payload)


class _Notifier:
    def __init__(self) -> None:
        self.arb_text: str | None = None
        self.success_calls: list[dict] = []
        self.failure_calls: list[dict] = []

    def notify_arb_found(self, text: str) -> None:
        self.arb_text = text

    def notify_trade_success(self, **kwargs) -> None:
        self.success_calls.append(kwargs)

    def notify_trade_failure(self, **kwargs) -> None:
        self.failure_calls.append(kwargs)


class _RiskMgrAccept:
    def __init__(self) -> None:
        self.recorded: list[tuple] = []

    def pre_trade_check(self, _opp, size):
        return True, "", size

    def record_execution(self, opp, trades, **_kwargs):
        self.recorded.append((opp, list(trades)))


class _RiskMgrReject:
    def __init__(self, reason: str = "max_positions_reached") -> None:
        self.reason = reason

    def pre_trade_check(self, _opp, _size):
        return False, self.reason, 0.0

    def record_execution(self, *_a, **_kw):
        raise AssertionError("must not be called when pre-check rejects")


def _config(**overrides):
    base = dict(
        dry_run=True,
        default_order_size_usdc=1.0,
        polymarket_taker_fee_rate=0.02,
        live_max_orderbook_snapshot_age_sec=2.0,
        live_min_ws_hit_ratio=0.8,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _opp(
    *,
    arb_type=ArbType.BINARY,
    total_cost: float = 0.5,
    net_edge: float = 0.05,
    max_executable_size: float = 100.0,
    confidence: float = 0.95,
) -> ArbOpportunity:
    leg = ArbLeg(
        token_id="tok-yes",
        condition_id="cond-1",
        outcome="Yes",
        side=OrderSide.BUY,
        price=total_cost,
        size=1.0,
        available_size=10.0,
        execution_price=total_cost,
        economic_cost=total_cost,
    )
    return ArbOpportunity(
        arb_type=arb_type,
        event_id="evt-1",
        event_title="Will X happen?",
        markets=[],
        total_cost=total_cost,
        guaranteed_payout=1.0,
        gross_edge=net_edge + 0.01,
        net_edge=net_edge,
        edge_pct=(net_edge / total_cost) * 100.0 if total_cost > 0 else 0.0,
        legs=[leg, leg],  # two legs so notify "filled_legs" path triggers
        max_executable_size=max_executable_size,
        confidence=confidence,
    )


def _trade(
    *,
    status: TradeStatus = TradeStatus.FILLED,
    simulated: bool = True,
    fill_size: float = 1.0,
) -> TradeRecord:
    return TradeRecord(
        trade_id="t1",
        arb_id="a1",
        token_id="tok-yes",
        condition_id="cond-1",
        side=OrderSide.BUY,
        price=0.5,
        size=fill_size,
        status=status,
        fill_price=0.5,
        fill_size=fill_size,
        economic_cost=0.5,
        simulated=simulated,
    )


def _executor(
    *,
    collateral_ok: bool = True,
    collateral_reason: str = "",
    trades: list[TradeRecord] | None = None,
    success: bool = True,
):
    def ensure_collateral(_amount):
        return collateral_ok, collateral_reason, None

    def execute(_opp, _size):
        return list(trades or [_trade()])

    def is_success(_opp, executed_trades):
        # Mirrors `ExecutionEngine.is_successful_execution`: every leg
        # must be FILLED. Stub flag overrides for negative tests.
        if not success:
            return False
        return bool(executed_trades) and all(t.status == TradeStatus.FILLED for t in executed_trades)

    return SimpleNamespace(
        ensure_sufficient_collateral=ensure_collateral,
        execute_arbitrage=execute,
        is_successful_execution=is_success,
    )


def _detector(*, verified=None, verify_returns_none: bool = False):
    def verify(opp, _size):
        if verify_returns_none:
            return None
        return verified or opp

    return SimpleNamespace(verify_opportunity_with_depth=verify)


# ---------- common assertion helper -----------------------------------------


def _has_event(rec: _Recorder, kind: str, event_name: str) -> bool:
    return any(k == kind and p.get("event") == event_name for k, p in rec.events)


# ---------- test happy path -------------------------------------------------


def test_dry_run_simulated_success_returns_simulated_delta() -> None:
    rec = _Recorder()
    dash = _DashState()
    notifier = _Notifier()
    risk = _RiskMgrAccept()

    delta = execute_t0_opportunity(
        opp=_opp(),
        config=_config(dry_run=True),
        detector=_detector(),
        executor=_executor(trades=[_trade(simulated=True)]),
        risk_mgr=risk,
        notifier=notifier,
        ai_advisor=None,
        dash_state=dash,
        event_recorder=rec,
    )

    # Always-side-effects.
    assert notifier.arb_text is not None
    # Verified opportunity card written.
    assert any(k == "opportunities" for k, _ in rec.events)
    # `trades` event written.
    assert any(k == "trades" for k, _ in rec.events)
    # Simulated trades MUST NOT touch risk_mgr.record_execution.
    assert risk.recorded == []
    # One trade row mirrored to dashboard.
    assert len(dash.trades) >= 1
    # Delta has simulated_successes set, not live_successes.
    assert delta.simulated_successes == 1
    assert delta.live_successes == 0


def test_live_success_records_through_risk_mgr_and_notifies() -> None:
    rec = _Recorder()
    dash = _DashState()
    notifier = _Notifier()
    risk = _RiskMgrAccept()

    delta = execute_t0_opportunity(
        opp=_opp(),
        config=_config(dry_run=False),
        detector=_detector(),
        executor=_executor(trades=[_trade(simulated=False)]),
        risk_mgr=risk,
        notifier=notifier,
        ai_advisor=None,
        dash_state=dash,
        event_recorder=rec,
    )

    # Live trades reconcile risk.
    assert len(risk.recorded) == 1
    # Live success populates live_successes.
    assert delta.live_successes == 1
    assert delta.simulated_successes == 0
    # On success a notify_trade_success goes out.
    assert len(notifier.success_calls) == 1
    assert notifier.success_calls[0]["arb_type"] == ArbType.BINARY.value


# ---------- bail paths -------------------------------------------------------


def test_depth_verification_failure_bails_with_stable_reason() -> None:
    rec = _Recorder()
    delta = execute_t0_opportunity(
        opp=_opp(),
        config=_config(),
        detector=_detector(verify_returns_none=True),
        executor=_executor(),
        risk_mgr=_RiskMgrAccept(),
        notifier=_Notifier(),
        ai_advisor=None,
        dash_state=_DashState(),
        event_recorder=rec,
    )
    assert delta == ExecutionDelta()
    assert _has_event(rec, "risk_events", "depth_verification_failed")


def test_pre_trade_reject_bails_with_stable_reason() -> None:
    rec = _Recorder()
    delta = execute_t0_opportunity(
        opp=_opp(),
        config=_config(),
        detector=_detector(),
        executor=_executor(),
        risk_mgr=_RiskMgrReject("max_positions_reached"),
        notifier=_Notifier(),
        ai_advisor=None,
        dash_state=_DashState(),
        event_recorder=rec,
    )
    assert delta == ExecutionDelta()
    assert _has_event(rec, "risk_events", "pre_trade_reject")
    payload = next(p for k, p in rec.events if k == "risk_events" and p.get("event") == "pre_trade_reject")
    assert payload["reason"] == "max_positions_reached"


def test_collateral_reject_bails_with_stable_reason() -> None:
    rec = _Recorder()
    delta = execute_t0_opportunity(
        opp=_opp(),
        config=_config(),
        detector=_detector(),
        executor=_executor(collateral_ok=False, collateral_reason="insufficient_balance"),
        risk_mgr=_RiskMgrAccept(),
        notifier=_Notifier(),
        ai_advisor=None,
        dash_state=_DashState(),
        event_recorder=rec,
    )
    assert delta == ExecutionDelta()
    assert _has_event(rec, "risk_events", "balance_reject")


# ---------- AI advisor wiring -----------------------------------------------


def test_ai_advisor_record_outcome_called_only_in_live_mode() -> None:
    """Dry-run path skips AI outcome update entirely."""
    advisor_calls: list[Any] = []
    advisor = SimpleNamespace(record_trade_outcome=lambda payload: advisor_calls.append(payload))

    execute_t0_opportunity(
        opp=_opp(),
        config=_config(dry_run=True),
        detector=_detector(),
        executor=_executor(trades=[_trade(simulated=True)]),
        risk_mgr=_RiskMgrAccept(),
        notifier=_Notifier(),
        ai_advisor=advisor,
        dash_state=_DashState(),
        event_recorder=_Recorder(),
    )
    assert advisor_calls == []

    # Live mode: the call should fire.
    execute_t0_opportunity(
        opp=_opp(),
        config=_config(dry_run=False),
        detector=_detector(),
        executor=_executor(trades=[_trade(simulated=False)]),
        risk_mgr=_RiskMgrAccept(),
        notifier=_Notifier(),
        ai_advisor=advisor,
        dash_state=_DashState(),
        event_recorder=_Recorder(),
    )
    assert len(advisor_calls) == 1


def test_no_ai_advisor_does_not_crash() -> None:
    delta = execute_t0_opportunity(
        opp=_opp(),
        config=_config(dry_run=False),
        detector=_detector(),
        executor=_executor(trades=[_trade(simulated=False)]),
        risk_mgr=_RiskMgrAccept(),
        notifier=_Notifier(),
        ai_advisor=None,
        dash_state=_DashState(),
        event_recorder=_Recorder(),
    )
    assert delta.live_successes == 1


# ---------- target_size math ------------------------------------------------


def test_zero_total_cost_bypasses_division_and_floors_target_size() -> None:
    """Defensive: total_cost=0 must not raise ZeroDivisionError."""
    captured = {}

    def verify(opp, size):
        captured["size"] = size
        return None  # bail to keep the test focused on the size calc

    detector = SimpleNamespace(verify_opportunity_with_depth=verify)

    execute_t0_opportunity(
        opp=_opp(total_cost=0.0, max_executable_size=10.0),
        config=_config(default_order_size_usdc=5.0),
        detector=detector,
        executor=_executor(),
        risk_mgr=_RiskMgrAccept(),
        notifier=_Notifier(),
        ai_advisor=None,
        dash_state=_DashState(),
        event_recorder=_Recorder(),
    )
    # min(0, 10.0) = 0
    assert captured["size"] == 0


def test_target_size_clamped_by_max_executable_size() -> None:
    """When default order size > book depth, target_size is capped."""
    captured = {}

    def verify(opp, size):
        captured["size"] = size
        return None

    detector = SimpleNamespace(verify_opportunity_with_depth=verify)

    execute_t0_opportunity(
        opp=_opp(total_cost=0.5, max_executable_size=2.0),
        config=_config(default_order_size_usdc=10.0),  # would request 20
        detector=detector,
        executor=_executor(),
        risk_mgr=_RiskMgrAccept(),
        notifier=_Notifier(),
        ai_advisor=None,
        dash_state=_DashState(),
        event_recorder=_Recorder(),
    )
    # min(10/0.5=20, 2.0) = 2.0
    assert captured["size"] == 2.0
