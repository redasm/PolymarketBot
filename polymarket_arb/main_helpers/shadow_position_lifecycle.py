"""Shadow-mode position lifecycle ledger for strategy attribution.

The regular risk state answers whether the bot can keep trading. This ledger
answers a different question: did shadow fills form profitable positions after
fees, exits, and mark-to-market?
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.models import OrderBookSnapshot, OrderSide, TradeRecord


BookSnapshotProvider = Callable[[str], OrderBookSnapshot | None]


@dataclass
class ShadowLot:
    position_id: str
    token_id: str
    condition_id: str
    open_ts: float
    open_price: float
    open_size: float
    remaining_size: float
    signal_id: str = ""
    execution_id: str = ""
    tier: str = ""
    fees: float = 0.0
    source_trade_ids: list[str] = field(default_factory=list)
    decision_context: dict[str, Any] = field(default_factory=dict)


class ShadowPositionLifecycle:
    """FIFO lifecycle tracker fed by `virtual_fills` rows."""

    def __init__(
        self,
        *,
        event_recorder: EventRecorder,
        book_snapshot_provider: BookSnapshotProvider | None = None,
    ) -> None:
        self._event_recorder = event_recorder
        self._book_snapshot_provider = book_snapshot_provider
        self._lots_by_token: dict[str, list[ShadowLot]] = {}
        self._seq = 0
        self._realized_pnl = 0.0
        self._fees = 0.0

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def fees(self) -> float:
        return self._fees

    def record_fill(
        self,
        trade: TradeRecord,
        *,
        fee: float = 0.0,
        tier: str = "",
        decision_context: dict[str, Any] | None = None,
    ) -> None:
        filled_size = float(trade.fill_size or 0.0)
        if filled_size <= 0:
            return
        fill_price = float(trade.fill_price if trade.fill_price is not None else trade.price)
        side = str(getattr(trade.side, "value", trade.side)).upper()
        self._fees += max(0.0, float(fee))
        if side == OrderSide.BUY.value:
            self._open_lot(
                trade,
                fill_price=fill_price,
                fill_size=filled_size,
                fee=fee,
                tier=tier,
                decision_context=decision_context or {},
            )
            return
        if side == OrderSide.SELL.value:
            self._close_lots(
                trade,
                fill_price=fill_price,
                fill_size=filled_size,
                fee=fee,
                tier=tier,
                decision_context=decision_context or {},
            )

    def snapshot(self) -> dict[str, Any]:
        unrealized = 0.0
        current_value = 0.0
        open_cost = 0.0
        open_lots = 0
        open_size = 0.0
        for token_id, lots in self._lots_by_token.items():
            mark = self._mark_price(token_id)
            for lot in lots:
                if lot.remaining_size <= 1e-9:
                    continue
                open_lots += 1
                open_size += lot.remaining_size
                fee_alloc = lot.fees * (lot.remaining_size / lot.open_size) if lot.open_size > 0 else 0.0
                cost = lot.open_price * lot.remaining_size
                open_cost += cost + fee_alloc
                if mark is None:
                    continue
                value = mark * lot.remaining_size
                current_value += value
                unrealized += value - cost - fee_alloc
        return {
            "realized_pnl": round(self._realized_pnl, 6),
            "unrealized_pnl": round(unrealized, 6),
            "total_pnl": round(self._realized_pnl + unrealized, 6),
            "current_position_value": round(current_value, 6),
            "open_cost": round(open_cost, 6),
            "open_lots": open_lots,
            "open_size": round(open_size, 6),
            "fees": round(self._fees, 6),
        }

    def _open_lot(
        self,
        trade: TradeRecord,
        *,
        fill_price: float,
        fill_size: float,
        fee: float,
        tier: str,
        decision_context: dict[str, Any],
    ) -> None:
        self._seq += 1
        position_id = f"shadow-pos-{int(time.time())}-{self._seq}"
        lot = ShadowLot(
            position_id=position_id,
            token_id=trade.token_id,
            condition_id=trade.condition_id,
            open_ts=float(trade.timestamp),
            open_price=fill_price,
            open_size=fill_size,
            remaining_size=fill_size,
            signal_id=str(getattr(trade, "signal_id", "") or ""),
            execution_id=str(getattr(trade, "execution_id", "") or ""),
            tier=tier,
            fees=max(0.0, float(fee)),
            source_trade_ids=[trade.trade_id],
            decision_context=dict(decision_context),
        )
        self._lots_by_token.setdefault(trade.token_id, []).append(lot)
        if self._event_recorder.is_enabled:
            self._event_recorder.write_event("positions_lifecycle", {
                "event": "position_opened",
                "position_id": position_id,
                "token_id": trade.token_id,
                "market_id": trade.condition_id,
                "signal_id": lot.signal_id,
                "execution_id": lot.execution_id,
                "open_trade_id": trade.trade_id,
                "open_ts": lot.open_ts,
                "open_price": fill_price,
                "open_size": fill_size,
                "remaining_size": fill_size,
                "fees": round(float(fee), 6),
                "tier": tier,
                "decision_context": dict(decision_context),
            })

    def _close_lots(
        self,
        trade: TradeRecord,
        *,
        fill_price: float,
        fill_size: float,
        fee: float,
        tier: str,
        decision_context: dict[str, Any],
    ) -> None:
        lots = self._lots_by_token.get(trade.token_id, [])
        remaining_sell = fill_size
        total_closed = 0.0
        total_realized = 0.0
        fee_remaining = max(0.0, float(fee))
        now_ts = float(trade.timestamp)
        for lot in list(lots):
            if remaining_sell <= 1e-9:
                break
            if lot.remaining_size <= 1e-9:
                continue
            close_size = min(lot.remaining_size, remaining_sell)
            close_ratio = close_size / fill_size if fill_size > 0 else 0.0
            sell_fee_alloc = fee_remaining * close_ratio
            buy_fee_alloc = lot.fees * (close_size / lot.open_size) if lot.open_size > 0 else 0.0
            realized = (fill_price - lot.open_price) * close_size - buy_fee_alloc - sell_fee_alloc
            lot.remaining_size = max(0.0, lot.remaining_size - close_size)
            remaining_sell -= close_size
            total_closed += close_size
            total_realized += realized
            self._realized_pnl += realized
            if self._event_recorder.is_enabled:
                self._event_recorder.write_event("positions_lifecycle", {
                    "event": "position_closed" if lot.remaining_size <= 1e-9 else "position_partially_closed",
                    "position_id": lot.position_id,
                    "token_id": lot.token_id,
                    "market_id": lot.condition_id,
                    "signal_id": lot.signal_id,
                    "execution_id": lot.execution_id,
                    "close_signal_id": str(getattr(trade, "signal_id", "") or ""),
                    "close_execution_id": str(getattr(trade, "execution_id", "") or ""),
                    "open_trade_id": lot.source_trade_ids[0] if lot.source_trade_ids else "",
                    "close_trade_id": trade.trade_id,
                    "open_ts": lot.open_ts,
                    "open_price": lot.open_price,
                    "open_size": lot.open_size,
                    "close_ts": now_ts,
                    "close_price": fill_price,
                    "close_size": close_size,
                    "remaining_size": lot.remaining_size,
                    "realized_pnl": round(realized, 6),
                    "lagged_follow_pnl_usdc": round(realized, 6),
                    "markout_pnl_usdc": round(realized, 6),
                    "fees": round(buy_fee_alloc + sell_fee_alloc, 6),
                    "hold_sec": round(max(0.0, now_ts - lot.open_ts), 4),
                    "tier": lot.tier or tier,
                    "exit_tier": tier,
                    "decision_context": dict(lot.decision_context or decision_context),
                })
        self._lots_by_token[trade.token_id] = [
            lot for lot in lots if lot.remaining_size > 1e-9
        ]
        if remaining_sell > 1e-9 and self._event_recorder.is_enabled:
            self._event_recorder.write_event("positions_lifecycle", {
                "event": "unmatched_sell",
                "token_id": trade.token_id,
                "market_id": trade.condition_id,
                "close_trade_id": trade.trade_id,
                "close_price": fill_price,
                "close_size": remaining_sell,
                "matched_size": total_closed,
                "realized_pnl": round(total_realized, 6),
                "tier": tier,
                "decision_context": decision_context,
            })

    def _mark_price(self, token_id: str) -> float | None:
        if self._book_snapshot_provider is None:
            return None
        try:
            snap = self._book_snapshot_provider(token_id)
        except Exception:
            return None
        if snap is None:
            return None
        best_bid = getattr(snap, "best_bid", None)
        if best_bid is not None and float(best_bid) > 0:
            return float(best_bid)
        mid = getattr(snap, "mid", None)
        if mid is not None and float(mid) > 0:
            return float(mid)
        return None
