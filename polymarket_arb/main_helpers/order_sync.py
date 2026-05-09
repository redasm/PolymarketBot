"""Per-cycle live-mode housekeeping: order status sync + stale order cancel.

Two side-effect-heavy chunks lifted out of `main_loop.main()`:

- `sync_live_order_statuses` polls the venue for pending orders, hands
  the result to `RiskManager.reconcile_pending_order_statuses`, applies
  any fill deltas to the maker inventory, and writes one event-recorder
  entry per status change.
- `cancel_stale_maker_orders` cancels any maker orders older than the
  configured TTL and reconciles them through the same risk-manager
  pathway.

Both are no-ops in dry-run (they're guarded at the call site by
`config.dry_run`). They never raise — exceptions are caught and
recorded as `risk_events` so a transient venue failure can't kill the
main loop.
"""

from __future__ import annotations

import logging
import time

from polymarket_arb.config import ArbConfig
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.main_helpers.signal_helpers import apply_maker_fill_to_inventory
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.maker_strategy import MakerStrategy

LOG = logging.getLogger("main_loop")


def sync_live_order_statuses(
    *,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    maker_strategy: MakerStrategy,
    event_recorder: EventRecorder,
) -> None:
    """Poll the venue for pending order statuses and apply fills.

    Three-step flow on success:

    1. Reconcile risk-manager state (closes pending reservations,
       updates positions for filled trades).
    2. Apply any fill deltas to the maker inventory so subsequent T3
       quote selection sees the up-to-date inventory.
    3. Emit one `order_status_sync` `risk_events` entry per changed
       order with the delta + fill details.

    Failures are caught and recorded as `order_status_sync_error` —
    we never want a transient venue 5xx to crash the main loop.
    """
    try:
        order_sync = executor.sync_pending_trade_statuses()
        if order_sync.polled:
            risk_mgr.reconcile_pending_order_statuses(order_sync.polled)
        if order_sync.changed:
            inventory_deltas = {
                trade.trade_id: apply_maker_fill_to_inventory(maker_strategy, trade)
                for trade in order_sync.changed
            }
        if event_recorder.is_enabled and order_sync.changed:
            for trade in order_sync.changed:
                event_recorder.write_event("risk_events", {
                    "event": "order_status_sync",
                    "order_id": trade.order_id,
                    "trade_id": trade.trade_id,
                    "condition_id": trade.condition_id,
                    "status": trade.status.value,
                    "fill_size": trade.fill_size,
                    "fill_price": trade.fill_price,
                    "inventory_delta": inventory_deltas.get(trade.trade_id, 0.0),
                    "error": trade.error,
                    "ts": time.time(),
                })
    except Exception as exc:
        LOG.warning("订单状态同步失败: %s", exc)
        if event_recorder.is_enabled:
            event_recorder.write_event("risk_events", {
                "event": "order_status_sync_error",
                "error": str(exc),
                "ts": time.time(),
            })


def cancel_stale_maker_orders(
    *,
    config: ArbConfig,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    event_recorder: EventRecorder,
) -> None:
    """Cancel any maker (post-only GTC) orders older than the TTL.

    Disabled when `config.maker_stale_order_ttl_sec <= 0`. For each
    cancelled order, runs the same `reconcile_pending_order_statuses`
    pathway used by `sync_live_order_statuses` so risk state stays
    consistent. Failures are recorded as `maker_order_cancel_error`.
    """
    if config.maker_stale_order_ttl_sec <= 0:
        return
    try:
        cancelled_stale = executor.cancel_stale_maker_orders(
            config.maker_stale_order_ttl_sec
        )
        if cancelled_stale:
            risk_mgr.reconcile_pending_order_statuses(cancelled_stale)
            if event_recorder.is_enabled:
                for trade in cancelled_stale:
                    event_recorder.write_event("risk_events", {
                        "event": "maker_order_cancelled",
                        "reason": "stale_ttl",
                        "order_id": trade.order_id,
                        "trade_id": trade.trade_id,
                        "condition_id": trade.condition_id,
                        "age_sec": time.time() - trade.timestamp,
                        "ts": time.time(),
                    })
    except Exception as exc:
        LOG.warning("撤销过期挂单失败: %s", exc)
        if event_recorder.is_enabled:
            event_recorder.write_event("risk_events", {
                "event": "maker_order_cancel_error",
                "error": str(exc),
                "ts": time.time(),
            })
