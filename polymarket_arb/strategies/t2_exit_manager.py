"""T2 directional position exit manager.

T2 (statistical arbitrage) used to enter directional positions and ride them
to settlement. Without an exit policy the bot was a charity: positive
expected-value entries plus zero-EV exits = settlement-time noise dominates.

This module tracks every T2 fill and evaluates four independent exit signals
each scan cycle:

  1. **Stop-loss**: token price moved adversely past `t2_stop_loss_bps`.
  2. **Take-profit**: token price advanced enough to capture
     `t2_take_profit_capture_pct` of the deviation that justified entry.
  3. **Time stop**: held longer than `t2_max_hold_sec` without trigger.
  4. **Optimal stopping**: Bellman-derived HOLD/STOP boundary based on the
     model's terminal-prob estimate and remaining horizon.

Triggering any of the four causes a SELL order via `ExecutionEngine`. In
dry-run mode the SELL is simulated through the same path so telemetry shape
matches live behaviour.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

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
from polymarket_arb.strategies.optimal_stopping import (
    OptimalStoppingPolicy,
    solve_markov_optimal_stopping,
)

LOG = logging.getLogger(__name__)


@dataclass
class T2OpenPosition:
    token_id: str
    condition_id: str
    market_id: str
    market_question: str
    outcome_label: str  # "Yes" / "No"
    entry_price: float
    size_remaining: float
    entry_ts: float
    deadline_ts: Optional[float]
    model_prob_at_entry: float
    deviation_at_entry: float
    last_eval_ts: float = 0.0
    exit_attempted: bool = False
    last_decision_reason: str = ""

    def add_fill(self, price: float, size: float) -> None:
        if size <= 0:
            return
        total = self.size_remaining + size
        if total <= 0:
            self.entry_price = price
            self.size_remaining = size
            return
        self.entry_price = (
            (self.entry_price * self.size_remaining) + (price * size)
        ) / total
        self.size_remaining = total


@dataclass
class T2ExitResult:
    """Result of one evaluate() pass — used for telemetry / dashboard."""

    triggered: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)


class T2ExitManager:
    """Tracks T2 fills and emits exit orders when policy triggers."""

    def __init__(
        self,
        *,
        config: ArbConfig,
        executor: ExecutionEngine,
        ob_analyzer: OrderBookAnalyzer,
    ):
        self._config = config
        self._executor = executor
        self._ob = ob_analyzer
        self._positions: dict[str, T2OpenPosition] = {}
        self._policy_cache: dict[float, OptimalStoppingPolicy] = {}
        self._stop_loss_bps = float(config.t2_stop_loss_bps)
        self._take_profit_capture_pct = float(config.t2_take_profit_capture_pct)
        self._max_hold_sec = float(config.t2_max_hold_sec)
        self._eval_interval_sec = max(0.0, float(config.t2_exit_eval_interval_sec))
        self._optimal_stopping_enabled = bool(config.t2_optimal_stopping_enabled)

    @property
    def open_positions(self) -> dict[str, T2OpenPosition]:
        return dict(self._positions)

    def register_fills(
        self,
        *,
        signal_payload: dict[str, Any],
        market: MarketInfo,
        trades: list[TradeRecord],
    ) -> None:
        """Register every filled (or partially filled) T2 leg."""
        if not trades:
            return
        action = str(signal_payload.get("action") or "BUY_YES").upper()
        outcome_label = "Yes" if action == "BUY_YES" else "No"
        deadline_ts = _parse_iso_to_ts(market.end_date)
        model_prob = float(signal_payload.get("model_prob") or 0.5)
        deviation = abs(float(signal_payload.get("deviation") or 0.0))

        for trade in trades:
            if trade.side != OrderSide.BUY:
                continue
            if trade.status not in (TradeStatus.FILLED, TradeStatus.PARTIAL):
                continue
            fill_size = trade.fill_size if trade.fill_size is not None else trade.size
            if fill_size is None or fill_size <= 0:
                continue
            fill_price = trade.fill_price if trade.fill_price is not None else trade.price
            if fill_price is None or fill_price <= 0:
                continue

            existing = self._positions.get(trade.token_id)
            if existing is not None:
                existing.add_fill(float(fill_price), float(fill_size))
                continue

            self._positions[trade.token_id] = T2OpenPosition(
                token_id=trade.token_id,
                condition_id=trade.condition_id,
                market_id=market.condition_id,
                market_question=market.question,
                outcome_label=outcome_label,
                entry_price=float(fill_price),
                size_remaining=float(fill_size),
                entry_ts=time.time(),
                deadline_ts=deadline_ts,
                model_prob_at_entry=model_prob,
                deviation_at_entry=deviation,
            )
            LOG.info(
                "T2 仓位已登记: token=%s outcome=%s entry=%.4f size=%.2f model_prob=%.3f dev=%.4f",
                trade.token_id[:16],
                outcome_label,
                float(fill_price),
                float(fill_size),
                model_prob,
                deviation,
            )

    def evaluate(
        self,
        *,
        active_markets: list[MarketInfo],
    ) -> T2ExitResult:
        """Run exit checks against the latest order book and emit SELL orders."""
        result = T2ExitResult()
        if not self._positions:
            return result

        markets_by_cid = {m.condition_id: m for m in active_markets}
        now = time.time()

        for token_id in list(self._positions.keys()):
            pos = self._positions[token_id]
            if pos.exit_attempted:
                self._positions.pop(token_id, None)
                continue
            if now - pos.last_eval_ts < self._eval_interval_sec:
                continue
            pos.last_eval_ts = now

            snap = self._ob.get_snapshot(token_id)
            if snap is None or snap.best_bid is None or snap.best_bid <= 0:
                continue
            current_price = float(snap.best_bid)

            reason = self._decide_exit(pos, current_price, now)
            pos.last_decision_reason = reason
            if reason == "hold":
                continue

            market = markets_by_cid.get(pos.condition_id)
            if market is None:
                LOG.warning(
                    "T2 退出无法构造 opportunity（缺市场 %s），跳过",
                    pos.condition_id[:12],
                )
                continue

            decision = {
                "token_id": token_id,
                "market_id": pos.market_id,
                "outcome": pos.outcome_label,
                "entry_price": pos.entry_price,
                "exit_price": current_price,
                "size": pos.size_remaining,
                "reason": reason,
                "ts": now,
            }
            result.decisions.append(decision)
            self._issue_exit(pos, market, current_price)
            pos.exit_attempted = True
            result.triggered += 1

        # Remove fully-exited positions.
        self._positions = {
            k: v for k, v in self._positions.items()
            if not v.exit_attempted and v.size_remaining > 1e-6
        }
        return result

    def remove_position(self, token_id: str) -> None:
        self._positions.pop(token_id, None)

    def _decide_exit(self, pos: T2OpenPosition, current_price: float, now: float) -> str:
        # 1. Time stop.
        if self._max_hold_sec > 0 and now - pos.entry_ts >= self._max_hold_sec:
            return "time_stop"

        # 2. Stop-loss (adverse move from entry).
        if pos.entry_price > 0 and self._stop_loss_bps > 0:
            adverse_bps = (pos.entry_price - current_price) / pos.entry_price * 10_000.0
            if adverse_bps >= self._stop_loss_bps:
                return "stop_loss"

        # 3. Take-profit when most of the entry deviation has been captured.
        if (
            pos.deviation_at_entry > 1e-6
            and self._take_profit_capture_pct > 0
        ):
            captured = current_price - pos.entry_price
            target = pos.deviation_at_entry * self._take_profit_capture_pct
            if captured >= target:
                return "take_profit"

        # 4. Optimal stopping (Bellman policy).
        if self._optimal_stopping_enabled:
            policy = self._get_policy(pos.model_prob_at_entry)
            remaining_steps = self._remaining_steps(pos, now)
            decision = policy.decide(remaining_steps, current_price)
            if decision.action == "STOP":
                return "optimal_stopping"

        return "hold"

    def _get_policy(self, terminal_prob: float) -> OptimalStoppingPolicy:
        # Bucket terminal_prob to 1% granularity to keep cache size bounded.
        bucket = round(max(0.0, min(1.0, float(terminal_prob))), 2)
        cached = self._policy_cache.get(bucket)
        if cached is not None:
            return cached
        # Horizon: 24 hourly steps. Coarse but adequate for short-hold T2.
        policy = solve_markov_optimal_stopping(
            horizon_steps=24,
            terminal_prob=bucket,
            price_step=0.05,
        )
        self._policy_cache[bucket] = policy
        return policy

    def _remaining_steps(self, pos: T2OpenPosition, now: float) -> int:
        if pos.deadline_ts is None or pos.deadline_ts <= now:
            return 0
        # One step ≈ one hour for the default policy horizon.
        return max(0, int((pos.deadline_ts - now) / 3600.0))

    def _issue_exit(
        self,
        pos: T2OpenPosition,
        market: MarketInfo,
        current_price: float,
    ) -> None:
        """Submit a single-leg SELL via ExecutionEngine."""
        opp = ArbOpportunity(
            arb_type=ArbType.DIRECTIONAL,
            event_id=market.event_id or market.condition_id,
            event_title=market.question,
            markets=[market],
            total_cost=current_price,
            guaranteed_payout=0.0,  # exit doesn't guarantee a payout
            gross_edge=0.0,
            net_edge=0.0,
            edge_pct=0.0,
            legs=[
                ArbLeg(
                    token_id=pos.token_id,
                    condition_id=pos.condition_id,
                    outcome=pos.outcome_label,
                    side=OrderSide.SELL,
                    price=current_price,
                    size=pos.size_remaining,
                    available_size=pos.size_remaining,
                )
            ],
            max_executable_size=pos.size_remaining,
        )
        try:
            trades = self._executor.execute_arbitrage(opp, pos.size_remaining)
            LOG.info(
                "T2 退出已提交: token=%s reason=%s size=%.2f price=%.4f trades=%d",
                pos.token_id[:16],
                pos.last_decision_reason,
                pos.size_remaining,
                current_price,
                len(trades),
            )
        except Exception as exc:  # pragma: no cover - depends on live client
            LOG.warning(
                "T2 退出失败: token=%s reason=%s err=%s",
                pos.token_id[:16],
                pos.last_decision_reason,
                exc,
            )


def _parse_iso_to_ts(value: str | None) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime
        # Polymarket end_date is ISO-8601 with Z.
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def t2_exit_telemetry(result: T2ExitResult, open_count: int) -> dict[str, Any]:
    return {
        "triggered": result.triggered,
        "open_positions": open_count,
        "decisions": [
            {
                "token_id": d["token_id"][:16],
                "market_id": d["market_id"][:12],
                "outcome": d["outcome"],
                "reason": d["reason"],
                "entry_price": round(float(d["entry_price"]), 4),
                "exit_price": round(float(d["exit_price"]), 4),
                "size": round(float(d["size"]), 4),
            }
            for d in result.decisions
        ],
    }
