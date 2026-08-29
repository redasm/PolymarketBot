"""T3 挂单计分校验 (/orders-scoring)."""

from __future__ import annotations

import pytest

from polymarket_arb.execution_engine import _parse_orders_scoring_payload
from polymarket_arb.main_helpers.maker_scoring_audit import (
    MakerScoringAuditState,
    audit_maker_order_scoring,
)
from polymarket_arb.models import OrderSide, TradeRecord, TradeStatus
from tests.conftest import make_test_config


class _StubRecorder:
    is_enabled = True

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def write_event(self, stream, payload):
        self.events.append((stream, dict(payload)))

    def events_named(self, name):
        return [payload for _, payload in self.events if payload.get("event") == name]


class _StubRisk:
    def __init__(self):
        self.reconciled: list = []

    def reconcile_pending_order_statuses(self, trades):
        self.reconciled.extend(trades)


class _StubExecutor:
    def __init__(self, trades, scoring_map, *, scoring_error=None):
        self._trades = trades
        self._scoring_map = scoring_map
        self._scoring_error = scoring_error
        self.cancelled_ids: list[str] = []

    def live_maker_trades(self):
        return list(self._trades)

    def check_orders_scoring(self, order_ids):
        if self._scoring_error is not None:
            raise self._scoring_error
        return {oid: self._scoring_map[oid] for oid in order_ids if oid in self._scoring_map}

    def cancel_maker_orders_by_id(self, order_ids, *, reason):
        self.cancelled_ids.extend(order_ids)
        out = []
        for trade in self._trades:
            if str(trade.order_id) in set(order_ids):
                trade.status = TradeStatus.CANCELLED
                trade.error = reason
                out.append(trade)
        return out


def _maker_trade(order_id: str, condition_id: str = "cond-1") -> TradeRecord:
    return TradeRecord(
        trade_id=f"t-{order_id}",
        arb_id="a1",
        token_id="tok",
        condition_id=condition_id,
        side=OrderSide.BUY,
        price=0.5,
        size=10.0,
        status=TradeStatus.PENDING,
        order_id=order_id,
        post_only=True,
        order_type_name="GTC",
    )


def _config(**overrides):
    base = dict(dry_run=False, live_trading_ack=True, maker_scoring_audit_interval_sec=0.0)
    base.update(overrides)
    return make_test_config(**base)


def _run(executor, *, config=None, state=None, risk=None, recorder=None, now=1000.0):
    return audit_maker_order_scoring(
        config=config or _config(),
        executor=executor,
        risk_mgr=risk or _StubRisk(),
        event_recorder=recorder or _StubRecorder(),
        state=state or MakerScoringAuditState(),
        now=now,
    )


# --------- 响应解析 ----------


def test_parse_scoring_map_payload():
    out = _parse_orders_scoring_payload({"data": {"a": True, "b": False}}, ["a", "b"])
    assert out == {"a": True, "b": False}


def test_parse_scoring_list_payload():
    raw = [{"order_id": "a", "scoring": "true"}, {"orderId": "b", "scoring": 0}]
    assert _parse_orders_scoring_payload(raw, ["a", "b"]) == {"a": True, "b": False}


def test_parse_missing_order_defaults_false():
    assert _parse_orders_scoring_payload({"data": {"a": True}}, ["a", "b"]) == {
        "a": True,
        "b": False,
    }


def test_parse_none_payload_is_all_false():
    assert _parse_orders_scoring_payload(None, ["a"]) == {"a": False}


# --------- 审计主流程 ----------


def test_audit_reports_scoring_ratio():
    recorder = _StubRecorder()
    executor = _StubExecutor(
        [_maker_trade("o1"), _maker_trade("o2"), _maker_trade("o3", "cond-2")],
        {"o1": True, "o2": False, "o3": False},
    )
    summary = _run(executor, recorder=recorder)
    assert summary["checked"] == 3
    assert summary["scoring"] == 1
    assert summary["not_scoring"] == 2
    assert summary["scoring_ratio"] == pytest.approx(1 / 3, abs=1e-4)
    assert summary["unscored_by_market"]["cond-1"] == 1
    assert recorder.events_named("maker_scoring_audit")


def test_unknown_orders_are_not_counted_as_unscored():
    """API 没返回的 order 是"未知"，不能进分母，更不能被撤."""
    executor = _StubExecutor([_maker_trade("o1"), _maker_trade("o2")], {"o1": True})
    summary = _run(executor)
    assert summary["checked"] == 1
    assert summary["scoring"] == 1
    assert summary["unknown"] == 1
    assert summary["scoring_ratio"] == pytest.approx(1.0)


def test_dry_run_is_a_noop():
    executor = _StubExecutor([_maker_trade("o1")], {"o1": False})
    assert _run(executor, config=_config(dry_run=True)) == {}


def test_disabled_audit_is_a_noop():
    executor = _StubExecutor([_maker_trade("o1")], {"o1": False})
    assert _run(executor, config=_config(maker_scoring_audit_enabled=False)) == {}


def test_interval_throttles_repeat_runs():
    executor = _StubExecutor([_maker_trade("o1")], {"o1": True})
    state = MakerScoringAuditState()
    config = _config(maker_scoring_audit_interval_sec=60.0)
    assert _run(executor, config=config, state=state, now=1000.0)
    assert _run(executor, config=config, state=state, now=1030.0) == {}
    assert _run(executor, config=config, state=state, now=1061.0)


def test_scoring_api_failure_is_recorded_not_raised():
    recorder = _StubRecorder()
    executor = _StubExecutor([_maker_trade("o1")], {}, scoring_error=RuntimeError("boom"))
    assert _run(executor, recorder=recorder) == {}
    assert recorder.events_named("maker_scoring_audit_error")


def test_no_live_orders_returns_empty():
    assert _run(_StubExecutor([], {})) == {}


# --------- grace 与撤单 ----------


def test_unscored_order_is_not_cancelled_before_grace():
    executor = _StubExecutor([_maker_trade("o1")], {"o1": False})
    state = MakerScoringAuditState()
    config = _config(
        maker_scoring_cancel_unscored=True, maker_scoring_unscored_grace_sec=90.0
    )
    summary = _run(executor, config=config, state=state, now=1000.0)
    assert summary["cancelled"] == 0
    assert executor.cancelled_ids == []


def test_unscored_order_is_cancelled_after_grace():
    trade = _maker_trade("o1")
    executor = _StubExecutor([trade], {"o1": False})
    state = MakerScoringAuditState()
    risk = _StubRisk()
    recorder = _StubRecorder()
    config = _config(
        maker_scoring_cancel_unscored=True, maker_scoring_unscored_grace_sec=90.0
    )
    _run(executor, config=config, state=state, risk=risk, recorder=recorder, now=1000.0)
    summary = _run(
        executor, config=config, state=state, risk=risk, recorder=recorder, now=1100.0
    )
    assert summary["cancelled"] == 1
    assert executor.cancelled_ids == ["o1"]
    assert risk.reconciled == [trade]
    assert recorder.events_named("maker_order_cancelled")[0]["reason"] == "not_scoring"


def test_scoring_again_resets_the_grace_clock():
    executor = _StubExecutor([_maker_trade("o1")], {"o1": False})
    state = MakerScoringAuditState()
    config = _config(
        maker_scoring_cancel_unscored=True, maker_scoring_unscored_grace_sec=90.0
    )
    _run(executor, config=config, state=state, now=1000.0)
    executor._scoring_map["o1"] = True
    _run(executor, config=config, state=state, now=1050.0)
    executor._scoring_map["o1"] = False
    summary = _run(executor, config=config, state=state, now=1100.0)
    assert summary["cancelled"] == 0


def test_cancel_disabled_by_default():
    executor = _StubExecutor([_maker_trade("o1")], {"o1": False})
    state = MakerScoringAuditState()
    _run(executor, state=state, now=1000.0)
    summary = _run(executor, state=state, now=2000.0)
    assert summary["not_scoring"] == 1
    assert summary["cancelled"] == 0
    assert executor.cancelled_ids == []


def test_state_forgets_orders_that_left_the_book():
    state = MakerScoringAuditState()
    executor = _StubExecutor([_maker_trade("o1")], {"o1": False})
    _run(executor, state=state, now=1000.0)
    assert "o1" in state.first_unscored_ts
    _run(_StubExecutor([_maker_trade("o2")], {"o2": True}), state=state, now=2000.0)
    assert "o1" not in state.first_unscored_ts
