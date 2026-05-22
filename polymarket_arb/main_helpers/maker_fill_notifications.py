"""Shared handling for maker fills observed after initial submission."""

from __future__ import annotations

from typing import Any, Callable

from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.main_helpers.signal_helpers import apply_maker_fill_to_inventory
from polymarket_arb.models import TradeRecord
from polymarket_arb.notifier import NotificationManager
from polymarket_arb.strategies.maker_strategy import MakerStrategy


def handle_observed_maker_fills(
    *,
    trades: list[TradeRecord],
    maker_strategy: MakerStrategy,
    notifier: NotificationManager | Any | None,
    event_recorder: EventRecorder,
    simulated: bool,
    event_name: str,
    apply_inventory: bool = True,
    inventory_deltas: dict[str, float] | None = None,
    on_observed: Callable[[TradeRecord, float], None] | None = None,
) -> int:
    """Apply maker fill deltas, emit telemetry, and send one notification per new fill.

    Maker GTC orders normally start as PENDING and are filled later by either
    live order-status sync or the shadow-mode sweep. This helper keeps that
    late-fill path consistent with the immediate-fill path used by taker orders.
    Returns the count of trades with a newly observed fill delta.
    """
    observed = 0
    for trade in trades:
        if not bool(getattr(trade, "post_only", False)):
            continue
        status_value = str(getattr(trade.status, "value", trade.status)).lower()
        if status_value not in {"filled", "partial"}:
            continue

        fill_size = float(trade.fill_size or 0.0)
        notified = float(getattr(trade, "notification_accounted_size", 0.0) or 0.0)
        fill_delta = max(0.0, fill_size - notified)
        if apply_inventory:
            inventory_delta = apply_maker_fill_to_inventory(maker_strategy, trade)
        else:
            inventory_delta = float((inventory_deltas or {}).get(trade.trade_id, 0.0))
        if fill_delta <= 0:
            continue

        observed += 1
        trade.notification_accounted_size = notified + fill_delta
        expected_edge = float(getattr(trade, "expected_edge_per_share", 0.0) or 0.0)
        expected_profit = expected_edge * fill_delta
        event_title = str(getattr(trade, "event_title", "") or trade.condition_id)

        if event_recorder.is_enabled:
            event_recorder.write_event("risk_events", {
                "event": event_name,
                "signal_id": getattr(trade, "signal_id", ""),
                "execution_id": getattr(trade, "execution_id", ""),
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "condition_id": trade.condition_id,
                "status": status_value,
                "fill_size": fill_size,
                "fill_delta": fill_delta,
                "fill_price": trade.fill_price,
                "inventory_delta": inventory_delta,
                "expected_edge_per_share": expected_edge,
                "expected_profit": expected_profit,
                "simulated": simulated,
            })

        if notifier is not None:
            notifier.notify_trade_success(
                event_title=event_title,
                arb_type="T3_market_making",
                filled_legs=1,
                total_legs=1,
                expected_profit=expected_profit,
                simulated=simulated,
                now_ts=trade.timestamp,
            )

        if on_observed is not None:
            on_observed(trade, fill_delta)

    return observed
