"""Shadow-mode virtual fill recorder (roadmap §三-阶段 1).

When the bot runs with `ARB_DRY_RUN=true`, every simulated trade — T0
multi-leg fills, T2 entries, T3 maker quotes, T2 exits — feeds through
this emitter and is persisted to ``data/telemetry/<date>.virtual_fills.ndjson``.

The schema follows the 13 fields the roadmap requires before unlocking
real capital:

- ``timestamp / market_id / side / price / size / order_type``
- ``is_maker / fee / slippage / intended_price``
- ``decision_context`` — best bid/ask at decision time + any signal
  metadata the caller hands in
- ``result`` — terminal status, filled size, avg fill price

The downstream daily-report script consumes this stream to compute
maker ratio, per-fill PnL, slippage distribution — the metrics that
gate the transition from shadow → $200-500 live verification.

Implementation notes:

- The emitter is side-effect only; it never mutates ``TradeRecord``.
- ``book_snapshot_provider`` is a callable so the emitter doesn't need
  to import the orderbook layer directly — main_loop wires it to
  ``EnhancedBookStore.get_snapshot`` (or any equivalent provider).
- Taker fee is resolved **per market** through ``fee_rate_provider``;
  ``taker_fee_rate`` is only the fallback for markets whose metadata we
  never saw. This matters more than it looks: crypto UPDOWN markets carry
  ``feeSchedule.rate = 0.07`` and weather markets ``0.05``, against a
  configured default of ``0.005``. Charging the default made the recorded
  6.20–6.26 shadow book read ``+$2705`` when the true-rate figure is
  ``-$888`` — i.e. the whole go/no-go signal was an artefact of the fee
  column. Maker fee is treated as 0 (these markets are ``takerOnly``).
  Liquidity rewards (roadmap §三-阶段 3) are out of scope for this
  telemetry layer — they get computed downstream from the maker fills.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.models import FeeStructure, OrderBookSnapshot, TradeRecord

LOG = logging.getLogger(__name__)


BookSnapshotProvider = Callable[[str], Optional[OrderBookSnapshot]]
# condition_id -> that market's decimal taker rate, or None when unknown.
FeeRateProvider = Callable[[str], Optional[float]]


@dataclass
class VirtualFillEmitter:
    """Persist simulated TradeRecords as ``virtual_fills`` NDJSON rows.

    Each invocation of :meth:`record_fill` produces exactly one event.
    """

    event_recorder: EventRecorder
    book_snapshot_provider: Optional[BookSnapshotProvider]
    taker_fee_rate: float
    enabled: bool = True
    lifecycle: Any | None = None
    fee_rate_provider: Optional[FeeRateProvider] = None

    def _resolve_taker_rate(self, condition_id: str) -> float:
        """Per-market taker rate, falling back to the configured default.

        The provider is a read-only convenience and must never be able to
        break a fill recording, so anything it raises is swallowed and the
        fallback stands.
        """
        if self.fee_rate_provider is None or not condition_id:
            return float(self.taker_fee_rate)
        try:
            resolved = self.fee_rate_provider(condition_id)
        except Exception:  # noqa: BLE001 - telemetry must not break on lookup
            LOG.debug("fee_rate_provider 失败, 回落默认费率 cid=%s", str(condition_id)[:20])
            return float(self.taker_fee_rate)
        if resolved is None:
            return float(self.taker_fee_rate)
        try:
            rate = float(resolved)
        except (TypeError, ValueError):
            return float(self.taker_fee_rate)
        return rate if rate >= 0.0 else float(self.taker_fee_rate)

    def record_fill(
        self,
        trade: TradeRecord,
        *,
        intended_price: Optional[float] = None,
        tier: Optional[str] = None,
        signal_context: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled or not self.event_recorder.is_enabled:
            return

        snap = (
            self.book_snapshot_provider(trade.token_id)
            if self.book_snapshot_provider is not None
            else None
        )
        best_bid = float(snap.best_bid) if snap and snap.best_bid is not None else None
        best_ask = float(snap.best_ask) if snap and snap.best_ask is not None else None

        intended = float(intended_price) if intended_price is not None else float(trade.price)
        fill_price = float(trade.fill_price) if trade.fill_price is not None else None
        slippage = (fill_price - intended) if fill_price is not None else 0.0
        is_maker = bool(trade.post_only)
        fee_rate = 0.0 if is_maker else self._resolve_taker_rate(trade.condition_id)
        # Fee is charged on the *actual* filled size only; an unfilled or
        # rejected order pays nothing. Using `trade.size` as a fallback
        # would inflate the shadow-mode fee budget for every quote that
        # never crossed.
        actual_filled = float(trade.fill_size) if trade.fill_size is not None else 0.0
        effective_price = fill_price if fill_price is not None else float(trade.price)
        fee = FeeStructure(taker_fee_rate=fee_rate).estimate_price_fee(
            effective_price,
            size=actual_filled,
        )

        decision_context: dict[str, Any] = {
            "best_bid_at_decision": best_bid,
            "best_ask_at_decision": best_ask,
        }
        if signal_context:
            decision_context.update(signal_context)

        payload: dict[str, Any] = {
            "event": "virtual_fill",
            "trade_id": trade.trade_id,
            "arb_id": trade.arb_id,
            "signal_id": getattr(trade, "signal_id", ""),
            "execution_id": getattr(trade, "execution_id", ""),
            "fill_timestamp": datetime.fromtimestamp(trade.timestamp, tz=timezone.utc).isoformat(),
            "market_id": trade.condition_id,
            "token_id": trade.token_id,
            "side": trade.side.value,
            "price": float(trade.price),
            "size": float(trade.size),
            "order_type": trade.order_type_name or "",
            "is_maker": is_maker,
            "fee": round(fee, 6),
            # Recorded alongside the fee so an audit can read the rate off the
            # row instead of dividing back out of `fee`.
            "fee_rate": round(fee_rate, 6),
            "slippage": round(slippage, 6),
            "intended_price": round(intended, 6),
            "decision_context": decision_context,
            "result": {
                "status": trade.status.value,
                "filled_size": actual_filled,
                "avg_fill_price": fill_price,
                "rolled_back": bool(trade.rolled_back),
                "error": trade.error or "",
            },
        }
        if tier:
            payload["tier"] = tier

        self.event_recorder.write_event("virtual_fills", payload)
        if self.lifecycle is not None:
            try:
                self.lifecycle.record_fill(
                    trade,
                    fee=fee,
                    tier=tier or "",
                    decision_context=decision_context,
                )
            except Exception as exc:  # pragma: no cover - telemetry must not break trading
                LOG.warning("shadow position lifecycle emit failed: %s", exc)
