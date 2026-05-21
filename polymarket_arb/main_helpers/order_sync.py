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
from polymarket_arb.main_helpers.maker_fill_notifications import handle_observed_maker_fills
from polymarket_arb.main_helpers.signal_helpers import apply_maker_fill_to_inventory
from polymarket_arb.notifier import NotificationManager
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.maker_strategy import MakerStrategy
from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator, StrategyTier

LOG = logging.getLogger("main_loop")


def sync_live_order_statuses(
    *,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    maker_strategy: MakerStrategy,
    event_recorder: EventRecorder,
    notifier: NotificationManager | None = None,
    orchestrator: StrategyOrchestrator | None = None,
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
            _release_t3_exposure_for_synced_orders(
                orchestrator,
                order_sync.changed,
                inventory_deltas=inventory_deltas,
            )
            handle_observed_maker_fills(
                trades=order_sync.changed,
                maker_strategy=maker_strategy,
                notifier=notifier,
                event_recorder=event_recorder,
                simulated=False,
                event_name="live_maker_fill_observed",
                apply_inventory=False,
                inventory_deltas=inventory_deltas,
            )
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
    orchestrator: StrategyOrchestrator | None = None,
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
            _release_t3_exposure_for_synced_orders(orchestrator, cancelled_stale)
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


def _release_t3_exposure_for_synced_orders(
    orchestrator: StrategyOrchestrator | None,
    trades: list,
    *,
    inventory_deltas: dict[str, float] | None = None,
) -> None:
    if orchestrator is None:
        return
    for trade in trades:
        amount = _t3_exposure_release_amount(
            trade,
            inventory_delta=(inventory_deltas or {}).get(str(getattr(trade, "trade_id", "")), 0.0),
        )
        if amount <= 0:
            continue
        try:
            orchestrator.record_settlement(StrategyTier.MARKET_MAKING, amount, 0.0)
        except Exception as exc:  # pragma: no cover - telemetry path must not break sync
            LOG.warning("释放 T3 策略预算失败: trade=%s amount=%.4f err=%s", getattr(trade, "trade_id", ""), amount, exc)


def _t3_exposure_release_amount(trade, *, inventory_delta: float = 0.0) -> float:
    if not bool(getattr(trade, "post_only", False)):
        return 0.0
    side = _side_value(trade)
    price = _trade_price(trade)
    if price <= 0:
        return 0.0
    if side == "SELL":
        return price * max(0.0, float(inventory_delta or 0.0))
    if side != "BUY":
        return 0.0
    status = _status_value(trade)
    if status not in {"cancelled", "canceled", "failed"}:
        return 0.0
    requested = max(0.0, _float(getattr(trade, "size", 0.0)))
    filled = max(0.0, _float(getattr(trade, "fill_size", 0.0)))
    return price * max(0.0, requested - filled)


def _trade_price(trade) -> float:
    economic_cost = getattr(trade, "economic_cost", None)
    if economic_cost not in (None, ""):
        return max(0.0, _float(economic_cost))
    return max(0.0, _float(getattr(trade, "price", 0.0)))


def _status_value(trade) -> str:
    return str(getattr(getattr(trade, "status", ""), "value", getattr(trade, "status", ""))).lower()


def _side_value(trade) -> str:
    return str(getattr(getattr(trade, "side", ""), "value", getattr(trade, "side", ""))).upper()


def _float(value) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
