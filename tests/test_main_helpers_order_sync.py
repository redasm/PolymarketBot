"""Tests for `polymarket_arb.main_helpers.order_sync`.

Locks in the per-cycle live-mode housekeeping contract:

- `sync_live_order_statuses` reconciles polled + changed orders, applies
  fill deltas to the maker inventory, writes one `order_status_sync`
  per change, and never escapes an exception.
- `cancel_stale_maker_orders` is a no-op when TTL <= 0, otherwise
  cancels stale orders, reconciles them, and writes one `maker_order_cancelled`
  per cancellation; failures become `maker_order_cancel_error`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from polymarket_arb.main_helpers.order_sync import (
    cancel_stale_maker_orders,
    sync_live_order_statuses,
)


# ---------- shared stubs -----------------------------------------------------


class _Recorder:
    is_enabled = True

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write_event(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


class _DisabledRecorder:
    is_enabled = False

    def write_event(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("must not be called when disabled")


class _RiskMgr:
    def __init__(self) -> None:
        self.reconciled: list[list] = []

    def reconcile_pending_order_statuses(self, trades: list) -> None:
        self.reconciled.append(list(trades))


class _MakerStrategy:
    """Tiny stub matching the surface `apply_maker_fill_to_inventory` calls."""

    def __init__(self) -> None:
        self.updates: list = []

    def update_inventory(self, token_id: str, side: str, size: float) -> None:
        self.updates.append((token_id, side, size))

    def get_inventory(self, _token_id: str) -> float:
        return 0.0


def _trade(
    *,
    trade_id: str = "t1",
    order_id: str = "o1",
    condition_id: str = "c1",
    token_id: str = "tok1",
    status_value: str = "filled",
    fill_size: float = 1.0,
    fill_price: float = 0.5,
    error: str = "",
    side_value: str = "BUY",
    timestamp: float = 0.0,
    post_only: bool = True,  # T3 maker fills are post-only
    inventory_accounted_size: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        trade_id=trade_id,
        order_id=order_id,
        condition_id=condition_id,
        token_id=token_id,
        status=SimpleNamespace(value=status_value),
        fill_size=fill_size,
        fill_price=fill_price,
        error=error,
        side=SimpleNamespace(value=side_value),
        timestamp=timestamp,
        post_only=post_only,
        inventory_accounted_size=inventory_accounted_size,
    )


def _executor(
    *,
    sync_result=None,
    sync_exc: Exception | None = None,
    cancel_result=None,
    cancel_exc: Exception | None = None,
) -> SimpleNamespace:
    def sync():
        if sync_exc is not None:
            raise sync_exc
        return sync_result or SimpleNamespace(polled=[], changed=[])

    def cancel(_ttl):
        if cancel_exc is not None:
            raise cancel_exc
        return cancel_result or []

    return SimpleNamespace(
        sync_pending_trade_statuses=sync,
        cancel_stale_maker_orders=cancel,
    )


# ---------- sync_live_order_statuses -----------------------------------------


def test_sync_no_changes_emits_no_events() -> None:
    rec = _Recorder()
    risk = _RiskMgr()
    sync_live_order_statuses(
        executor=_executor(sync_result=SimpleNamespace(polled=[], changed=[])),
        risk_mgr=risk,
        maker_strategy=_MakerStrategy(),
        event_recorder=rec,
    )
    assert rec.events == []
    assert risk.reconciled == []


def test_sync_polled_only_reconciles_but_emits_nothing() -> None:
    rec = _Recorder()
    risk = _RiskMgr()
    polled = [_trade(trade_id="t1", status_value="pending")]
    sync_live_order_statuses(
        executor=_executor(sync_result=SimpleNamespace(polled=polled, changed=[])),
        risk_mgr=risk,
        maker_strategy=_MakerStrategy(),
        event_recorder=rec,
    )
    assert risk.reconciled == [polled]
    assert rec.events == []


def test_sync_changes_reconcile_apply_inventory_and_emit_event_per_trade() -> None:
    rec = _Recorder()
    risk = _RiskMgr()
    maker = _MakerStrategy()
    changed = [
        _trade(trade_id="t1", token_id="tok1", side_value="BUY", fill_size=2.0),
        _trade(trade_id="t2", token_id="tok2", side_value="SELL", fill_size=1.5),
    ]
    sync_live_order_statuses(
        executor=_executor(sync_result=SimpleNamespace(polled=changed, changed=changed)),
        risk_mgr=risk,
        maker_strategy=maker,
        event_recorder=rec,
    )
    # Reconciler runs once for `polled` (which includes the changes).
    assert risk.reconciled == [changed]
    # One event per changed trade.
    kinds = [k for k, _ in rec.events]
    assert kinds == ["risk_events", "risk_events"]
    payloads = [p for _, p in rec.events]
    assert payloads[0]["event"] == "order_status_sync"
    assert payloads[0]["trade_id"] == "t1"
    assert payloads[1]["trade_id"] == "t2"
    # Inventory deltas applied: BUY t1 +2.0, SELL t2 -1.5.
    assert ("tok1", "BUY", 2.0) in maker.updates
    assert ("tok2", "SELL", 1.5) in maker.updates


def test_sync_disabled_recorder_skips_events_but_still_reconciles() -> None:
    risk = _RiskMgr()
    changed = [_trade(trade_id="t1")]
    sync_live_order_statuses(
        executor=_executor(sync_result=SimpleNamespace(polled=changed, changed=changed)),
        risk_mgr=risk,
        maker_strategy=_MakerStrategy(),
        event_recorder=_DisabledRecorder(),
    )
    assert risk.reconciled == [changed]


def test_sync_swallow_exception_records_error_event() -> None:
    rec = _Recorder()
    sync_live_order_statuses(
        executor=_executor(sync_exc=RuntimeError("venue 503")),
        risk_mgr=_RiskMgr(),
        maker_strategy=_MakerStrategy(),
        event_recorder=rec,
    )
    assert len(rec.events) == 1
    kind, payload = rec.events[0]
    assert kind == "risk_events"
    assert payload["event"] == "order_status_sync_error"
    assert "venue 503" in payload["error"]


def test_sync_swallow_exception_with_disabled_recorder_does_not_crash() -> None:
    # `_DisabledRecorder.write_event` would raise — ensure the early
    # return on `is_enabled` fires.
    sync_live_order_statuses(
        executor=_executor(sync_exc=RuntimeError("boom")),
        risk_mgr=_RiskMgr(),
        maker_strategy=_MakerStrategy(),
        event_recorder=_DisabledRecorder(),
    )


# ---------- cancel_stale_maker_orders ----------------------------------------


def _config(ttl: float) -> SimpleNamespace:
    return SimpleNamespace(maker_stale_order_ttl_sec=ttl)


def test_cancel_disabled_when_ttl_zero_or_negative() -> None:
    rec = _Recorder()
    risk = _RiskMgr()
    cancel_stale_maker_orders(
        config=_config(0.0),
        executor=_executor(cancel_exc=RuntimeError("must not be called")),
        risk_mgr=risk,
        event_recorder=rec,
    )
    assert rec.events == []
    assert risk.reconciled == []

    cancel_stale_maker_orders(
        config=_config(-1.0),
        executor=_executor(cancel_exc=RuntimeError("must not be called")),
        risk_mgr=risk,
        event_recorder=rec,
    )
    assert rec.events == []


def test_cancel_no_stale_orders_emits_nothing() -> None:
    rec = _Recorder()
    risk = _RiskMgr()
    cancel_stale_maker_orders(
        config=_config(60.0),
        executor=_executor(cancel_result=[]),
        risk_mgr=risk,
        event_recorder=rec,
    )
    assert risk.reconciled == []
    assert rec.events == []


def test_cancel_stale_reconciles_and_emits_one_event_per_trade() -> None:
    rec = _Recorder()
    risk = _RiskMgr()
    cancelled = [
        _trade(trade_id="t1", order_id="o1", condition_id="c1"),
        _trade(trade_id="t2", order_id="o2", condition_id="c2"),
    ]
    cancel_stale_maker_orders(
        config=_config(60.0),
        executor=_executor(cancel_result=cancelled),
        risk_mgr=risk,
        event_recorder=rec,
    )
    assert risk.reconciled == [cancelled]
    kinds_payloads = [(k, p["event"], p["order_id"]) for k, p in rec.events]
    assert kinds_payloads == [
        ("risk_events", "maker_order_cancelled", "o1"),
        ("risk_events", "maker_order_cancelled", "o2"),
    ]


def test_cancel_swallow_exception_records_error_event() -> None:
    rec = _Recorder()
    cancel_stale_maker_orders(
        config=_config(60.0),
        executor=_executor(cancel_exc=RuntimeError("ws gone")),
        risk_mgr=_RiskMgr(),
        event_recorder=rec,
    )
    assert len(rec.events) == 1
    kind, payload = rec.events[0]
    assert kind == "risk_events"
    assert payload["event"] == "maker_order_cancel_error"
    assert "ws gone" in payload["error"]


def test_cancel_swallow_exception_with_disabled_recorder_does_not_crash() -> None:
    cancel_stale_maker_orders(
        config=_config(60.0),
        executor=_executor(cancel_exc=RuntimeError("boom")),
        risk_mgr=_RiskMgr(),
        event_recorder=_DisabledRecorder(),
    )
