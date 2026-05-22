"""T3 maker fill exit manager.

Pre-fix, T3 market-making fills had no exit path: a maker_crossed
`buy_yes @ $0.23` on a geopolitical market just sat there bleeding
mark-to-market until on-chain resolution (2027). The 2026-05-22 shadow
run sat on 5 such positions for 3+ hours with the bot's position cap
saturated and zero realized PnL.

This module mirrors `T2ExitManager` but is deliberately simpler:

  * **TTL** — close anything held longer than `MAKER_MAX_HOLD_SEC`.
  * **Stop-loss** — close when current best-bid is `MAKER_STOP_LOSS_BPS`
    below entry VWAP (adverse move).
  * **Take-profit** — close when current best-bid is
    `MAKER_TAKE_PROFIT_BPS` above entry VWAP (favorable move / reversal
    captured — the "反向" trigger).

What this manager intentionally does NOT replicate from T2:

  * Scale-out tranches — T3 maker positions are smaller and a single FAK
    sweep is fine; tranching adds latency for no diversification benefit.
  * Optimal-stopping Bellman — T3 has no model belief on terminal
    probability; the exit decision is purely price-vs-entry.
  * Escalation ladder + abandon path — these can be added once T3 exits
    are validated in shadow.

Only net-long positions are managed. Net-short maker inventory would
require a BUY-back; we surface a warning and skip until that path is
needed in practice.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    MarketInfo,
    OrderSide,
    TradeRecord,
    TradeStatus,
)
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.strategies.strategy_orchestrator import StrategyTier

LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from polymarket_arb.notifier import NotificationManager
    from polymarket_arb.risk_manager import RiskManager
    from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator


@dataclass
class T3MakerOpenPosition:
    token_id: str
    condition_id: str
    market_question: str
    outcome_label: str
    entry_price: float
    size_remaining: float
    entry_ts: float
    last_eval_ts: float = 0.0
    exit_attempted: bool = False
    last_decision_reason: str = ""

    def add_fill(self, price: float, size: float, *, now_ts: float | None = None) -> None:
        if size <= 0:
            return
        old_size = self.size_remaining
        total = old_size + size
        if total <= 0:
            self.entry_price = price
            self.size_remaining = size
            return
        self.entry_price = ((self.entry_price * old_size) + (price * size)) / total
        if now_ts is not None:
            self.entry_ts = ((self.entry_ts * old_size) + (float(now_ts) * size)) / total
        self.size_remaining = total


@dataclass
class T3MakerExitResult:
    attempted: int = 0
    triggered: int = 0
    partial: int = 0
    failed: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)


class T3MakerExitManager:
    """Tracks T3 maker fills and emits SELL orders when policy triggers."""

    def __init__(
        self,
        *,
        config: ArbConfig,
        executor: ExecutionEngine,
        ob_analyzer: OrderBookAnalyzer,
        risk_manager: "RiskManager | None" = None,
        notifier: "NotificationManager | None" = None,
        orchestrator: "StrategyOrchestrator | None" = None,
    ) -> None:
        self._config = config
        self._executor = executor
        self._ob = ob_analyzer
        self._risk_manager = risk_manager
        self._notifier = notifier
        self._orchestrator = orchestrator
        self._max_hold_sec = float(config.maker_max_hold_sec)
        self._stop_loss_bps = float(config.maker_stop_loss_bps)
        self._take_profit_bps = float(config.maker_take_profit_bps)
        self._eval_interval_sec = max(0.0, float(config.maker_exit_eval_interval_sec))
        self._positions: dict[str, T3MakerOpenPosition] = {}
        self._lock = threading.RLock()
        self._exiting_tokens: set[str] = set()

    @property
    def open_positions(self) -> dict[str, T3MakerOpenPosition]:
        with self._lock:
            return dict(self._positions)

    def register_fill(
        self,
        *,
        trade: TradeRecord,
        market: MarketInfo | None = None,
    ) -> None:
        """Register a single observed maker fill.

        Only BUY fills create / extend a position. SELL fills are
        accounted for by the exit path itself (which calls
        `_apply_exit_fill`), so registering a SELL here would
        double-count.
        """
        if str(getattr(trade.side, "value", trade.side)).upper() != OrderSide.BUY.value:
            return
        if trade.status not in (TradeStatus.FILLED, TradeStatus.PARTIAL):
            return
        fill_size = float(trade.fill_size if trade.fill_size is not None else trade.size or 0.0)
        if fill_size <= 0:
            return
        fill_price = float(trade.fill_price if trade.fill_price is not None else trade.price or 0.0)
        if fill_price <= 0:
            return

        outcome_label = _resolve_outcome_label(market, trade)
        market_question = getattr(market, "question", "") if market is not None else ""
        now = time.time()
        with self._lock:
            existing = self._positions.get(trade.token_id)
            if existing is not None:
                existing.add_fill(fill_price, fill_size, now_ts=now)
                return
            self._positions[trade.token_id] = T3MakerOpenPosition(
                token_id=trade.token_id,
                condition_id=trade.condition_id,
                market_question=market_question,
                outcome_label=outcome_label,
                entry_price=fill_price,
                size_remaining=fill_size,
                entry_ts=now,
            )
            LOG.info(
                "T3 maker 仓位已登记: token=%s outcome=%s entry=%.4f size=%.2f",
                trade.token_id[:16],
                outcome_label,
                fill_price,
                fill_size,
            )

    def evaluate(self, *, active_markets: list[MarketInfo]) -> T3MakerExitResult:
        result = T3MakerExitResult()
        with self._lock:
            if not self._positions:
                return result
            markets_by_cid = {m.condition_id: m for m in active_markets}
            now = time.time()
            for token_id in list(self._positions.keys()):
                pos = self._positions[token_id]
                if pos.exit_attempted:
                    self._positions.pop(token_id, None)
                    continue
                if token_id in self._exiting_tokens:
                    continue
                if pos.size_remaining <= 1e-6:
                    self._positions.pop(token_id, None)
                    continue
                if now - pos.last_eval_ts < self._eval_interval_sec:
                    continue
                pos.last_eval_ts = now

                snap = self._ob.get_snapshot(token_id)
                if snap is None or snap.best_bid is None or snap.best_bid <= 0:
                    continue
                current_bid = float(snap.best_bid)
                tick_size = float(getattr(snap, "tick_size", 0.01) or 0.01)

                reason = self._decide_exit(pos, current_bid, now)
                pos.last_decision_reason = reason
                if reason == "hold":
                    continue

                market = markets_by_cid.get(pos.condition_id)
                if market is None:
                    LOG.warning(
                        "T3 maker 退出缺少 market (%s)，跳过",
                        pos.condition_id[:12],
                    )
                    continue

                decision = {
                    "token_id": token_id,
                    "condition_id": pos.condition_id,
                    "outcome": pos.outcome_label,
                    "entry_price": round(pos.entry_price, 6),
                    "exit_price": round(current_bid, 6),
                    "size": round(pos.size_remaining, 6),
                    "reason": reason,
                    "ts": now,
                }
                self._exiting_tokens.add(token_id)
                try:
                    status, fill_size = self._issue_exit(
                        pos, market, current_bid, tick_size=tick_size
                    )
                finally:
                    self._exiting_tokens.discard(token_id)
                decision["status"] = status
                decision["fill_size"] = round(fill_size, 6)
                decision["remaining_size"] = round(pos.size_remaining, 6)
                result.attempted += 1
                result.decisions.append(decision)
                if status == "exited":
                    pos.exit_attempted = True
                    result.triggered += 1
                elif status == "partial_exit":
                    result.partial += 1
                else:
                    result.failed += 1
            self._positions = {
                k: v for k, v in self._positions.items()
                if not v.exit_attempted and v.size_remaining > 1e-6
            }
        return result

    def _decide_exit(self, pos: T3MakerOpenPosition, current_bid: float, now: float) -> str:
        if self._max_hold_sec > 0 and now - pos.entry_ts >= self._max_hold_sec:
            return "time_stop"
        if pos.entry_price <= 0:
            return "hold"
        bps = (current_bid - pos.entry_price) / pos.entry_price * 10_000.0
        if self._stop_loss_bps > 0 and bps <= -self._stop_loss_bps:
            return "stop_loss"
        if self._take_profit_bps > 0 and bps >= self._take_profit_bps:
            return "take_profit"
        return "hold"

    def _issue_exit(
        self,
        pos: T3MakerOpenPosition,
        market: MarketInfo,
        current_bid: float,
        *,
        tick_size: float,
    ) -> tuple[str, float]:
        size_to_sell = float(pos.size_remaining)
        if size_to_sell <= 1e-6:
            return "exit_failed", 0.0
        opp = ArbOpportunity(
            arb_type=ArbType.DIRECTIONAL,
            event_id=market.event_id or market.condition_id,
            event_title=market.question,
            markets=[market],
            total_cost=current_bid,
            guaranteed_payout=0.0,
            gross_edge=0.0,
            net_edge=0.0,
            edge_pct=0.0,
            legs=[
                ArbLeg(
                    token_id=pos.token_id,
                    condition_id=pos.condition_id,
                    outcome=pos.outcome_label,
                    side=OrderSide.SELL,
                    price=current_bid,
                    size=size_to_sell,
                    available_size=size_to_sell,
                    tick_size=tick_size,
                )
            ],
            max_executable_size=size_to_sell,
        )
        try:
            trades = self._executor.execute_arbitrage(
                opp,
                size_to_sell,
                order_type_name="FAK",
            )
        except Exception as exc:  # pragma: no cover - depends on live client
            LOG.warning(
                "T3 maker 退出失败: token=%s reason=%s err=%s",
                pos.token_id[:16],
                pos.last_decision_reason,
                exc,
            )
            return "exit_failed", 0.0
        fill_size = _sell_fill_size(trades, pos.token_id)
        if fill_size > 0:
            pos.size_remaining = max(0.0, pos.size_remaining - fill_size)
            self._release_exit_exposure(pos, current_bid, fill_size)
        if fill_size > 0 and pos.size_remaining <= 1e-6:
            LOG.info(
                "T3 maker 退出成交: token=%s reason=%s filled=%.2f price=%.4f",
                pos.token_id[:16],
                pos.last_decision_reason,
                fill_size,
                current_bid,
            )
            return "exited", fill_size
        if fill_size > 0:
            LOG.info(
                "T3 maker 部分退出: token=%s reason=%s filled=%.2f remaining=%.2f price=%.4f",
                pos.token_id[:16],
                pos.last_decision_reason,
                fill_size,
                pos.size_remaining,
                current_bid,
            )
            return "partial_exit", fill_size
        LOG.warning(
            "T3 maker 退出未成交: token=%s reason=%s price=%.4f",
            pos.token_id[:16],
            pos.last_decision_reason,
            current_bid,
        )
        return "exit_failed", 0.0

    def _release_exit_exposure(
        self, pos: T3MakerOpenPosition, fill_price: float, fill_size: float
    ) -> None:
        notional_at_entry = max(0.0, pos.entry_price * fill_size)
        if self._risk_manager is not None:
            try:
                self._risk_manager.release_market_exposure(
                    pos.condition_id, max(0.0, fill_price * fill_size)
                )
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("T3 maker 退出释放 risk exposure 失败: %s", exc)
        if self._orchestrator is not None and notional_at_entry > 0:
            try:
                self._orchestrator.record_settlement(
                    StrategyTier.MARKET_MAKING, notional_at_entry, 0.0
                )
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("T3 maker 退出释放 orchestrator exposure 失败: %s", exc)


def _resolve_outcome_label(market: MarketInfo | None, trade: TradeRecord) -> str:
    if market is not None:
        for token in market.tokens:
            if token.token_id == trade.token_id and token.outcome:
                return str(token.outcome)
    # T2 helper convention: default to Yes when unknown.
    return "Yes"


def _sell_fill_size(trades: list[TradeRecord], token_id: str) -> float:
    total = 0.0
    for trade in trades:
        if trade.token_id != token_id or trade.side != OrderSide.SELL:
            continue
        fill_size = trade.fill_size
        if fill_size is None and trade.status in (TradeStatus.FILLED, TradeStatus.PARTIAL):
            fill_size = trade.size
        if fill_size and fill_size > 0:
            total += float(fill_size)
    return total


# Unused import suppression: `uuid` may be required by callers extending
# this module to generate exit-order IDs.
_ = uuid
