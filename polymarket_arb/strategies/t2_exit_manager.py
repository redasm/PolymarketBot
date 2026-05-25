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

Scale-out (Kobylanski 2009 d-stop): take_profit and optimal_stopping do
not exit the whole position in one shot. Instead each trigger sells
`size_remaining / remaining_tranches`, so over `T2_SCALE_OUT_TRANCHES`
successful triggers the position is fully closed. This converts a single
exit-price realisation into N independent realisations, which strictly
dominates 1-stop when the model is uncertain (the bot is essentially
averaging the optimal exit across the value-function uncertainty band).
stop_loss / time_stop / floor_dump remain full-position exits — when the
model says "abort", dragging that out across cycles is the wrong tradeoff.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

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
from polymarket_arb.strategies.strategy_orchestrator import StrategyTier

LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from polymarket_arb.notifier import NotificationManager
    from polymarket_arb.risk_manager import RiskManager
    from polymarket_arb.strategies.recent_exit_cooldown import RecentExitCooldownStore
    from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator


_MAX_EXIT_RETRIES = 3
# Escalation ladder: after _FLOOR_EXIT_AFTER consecutive failures we stop
# trying to limit-sell at the moving bid and fire a "give up the spread"
# FAK at FLOOR_PRICE. After _ABANDON_AFTER total failures we release the
# exposure so RISK_MAX_OPEN_POSITIONS / RISK_MAX_TOTAL_EXPOSURE stop
# starving the rest of the strategies — the position remains on-chain
# (the bot can't dispose of it), but the in-memory book is unblocked.
_FLOOR_EXIT_AFTER = 10
_ABANDON_AFTER = 20
_FLOOR_PRICE = 0.01


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
    # Number of failed `_issue_exit` calls. Without a cap a transport error
    # would silently abandon the position; without retries a transient blip
    # would mark the position closed even though no SELL ever landed.
    exit_failure_count: int = 0
    next_exit_retry_ts: float = 0.0
    # Set when the in-memory tracking gives up and releases exposure. The
    # underlying on-chain position may still exist; portfolio_sync owns
    # the reconciliation from that point on.
    abandoned: bool = False
    # Scale-out state. `tranches_total` is fixed at registration from
    # config; `tranches_executed` only advances on a SUCCESSFUL full-fill
    # of a tranche slice (partial fills are retried until the slice
    # completes or the position abandons). Full-exit triggers
    # (stop_loss / time_stop / floor_dump) bypass tranching entirely.
    tranches_total: int = 1
    tranches_executed: int = 0
    # Most-recent rolling estimate of P(this token resolves to 1), in
    # token-space. Updated each evaluate() cycle when a
    # `model_prob_provider` is wired. `None` means we never had a fresh
    # estimate and fell back to `model_prob_at_entry`.
    current_model_prob: float | None = None
    # Rolling window of recently-observed mid prices. Used to compute a
    # per-position log-return stdev which feeds the dynamic stop-loss.
    # Sized in the manager from `t2_stop_loss_dynamic_warmup * 3` so we
    # have a stable rolling window after warmup.
    price_history: deque[float] = field(default_factory=lambda: deque(maxlen=30))
    # Cached effective stop-loss bps for the most recent evaluate()
    # pass, surfaced in telemetry. None = static stop in use.
    last_effective_stop_bps: float | None = None

    def add_fill(
        self,
        price: float,
        size: float,
        *,
        now_ts: float | None = None,
        model_prob: float | None = None,
        deviation: float | None = None,
    ) -> None:
        if size <= 0:
            return
        old_size = self.size_remaining
        total = self.size_remaining + size
        if total <= 0:
            self.entry_price = price
            self.size_remaining = size
            return
        self.entry_price = (
            (self.entry_price * self.size_remaining) + (price * size)
        ) / total
        if now_ts is not None:
            self.entry_ts = ((self.entry_ts * old_size) + (float(now_ts) * size)) / total
        if model_prob is not None:
            self.model_prob_at_entry = (
                (self.model_prob_at_entry * old_size) + (float(model_prob) * size)
            ) / total
        if deviation is not None:
            self.deviation_at_entry = (
                (self.deviation_at_entry * old_size) + (float(deviation) * size)
            ) / total
        self.size_remaining = total


@dataclass
class T2ExitResult:
    """Result of one evaluate() pass — used for telemetry / dashboard."""

    attempted: int = 0
    triggered: int = 0  # full-position closures (last tranche or full-exit trigger)
    tranche_exits: int = 0  # successful intermediate tranche slices
    partial: int = 0
    failed: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)


class T2ExitManager:
    """Tracks T2 fills and emits exit orders when policy triggers."""

    def __init__(
        self,
        *,
        config: ArbConfig,
        executor: ExecutionEngine,
        ob_analyzer: OrderBookAnalyzer,
        risk_manager: "RiskManager | None" = None,
        notifier: "NotificationManager | None" = None,
        cooldown_store: "RecentExitCooldownStore | None" = None,
        orchestrator: "StrategyOrchestrator | None" = None,
        model_prob_provider: Callable[[MarketInfo, str], float | None] | None = None,
    ):
        self._config = config
        self._executor = executor
        self._ob = ob_analyzer
        self._risk_manager = risk_manager
        self._notifier = notifier
        self._cooldown_store = cooldown_store
        self._orchestrator = orchestrator
        # Optional callable that recomputes the model's belief P(token=1)
        # for an open position each evaluate() pass. Without it the
        # Bellman policy freezes at `model_prob_at_entry`, which means
        # information arriving after entry can't update the exit
        # threshold. Provider returns None ⇒ fall back to entry prob.
        self._model_prob_provider = model_prob_provider
        self._positions: dict[str, T2OpenPosition] = {}
        self._policy_cache: dict[float, OptimalStoppingPolicy] = {}
        self._stop_loss_bps = float(config.t2_stop_loss_bps)
        self._take_profit_capture_pct = float(config.t2_take_profit_capture_pct)
        self._max_hold_sec = float(config.t2_max_hold_sec)
        self._eval_interval_sec = max(0.0, float(config.t2_exit_eval_interval_sec))
        self._optimal_stopping_enabled = bool(config.t2_optimal_stopping_enabled)
        self._scale_out_tranches = max(1, int(getattr(config, "t2_scale_out_tranches", 1)))
        self._dyn_stop_enabled = bool(getattr(config, "t2_stop_loss_dynamic_enabled", False))
        self._dyn_stop_k = max(0.0, float(getattr(config, "t2_stop_loss_dynamic_k", 2.0)))
        self._dyn_stop_min_bps = max(0.0, float(getattr(config, "t2_stop_loss_min_bps", 100.0)))
        self._dyn_stop_max_bps = max(
            self._dyn_stop_min_bps,
            float(getattr(config, "t2_stop_loss_max_bps", 1000.0)),
        )
        self._dyn_stop_warmup = max(2, int(getattr(config, "t2_stop_loss_dynamic_warmup", 5)))
        # Window large enough that the EWMA-equivalent has stable
        # estimates well past warmup, but bounded so old observations
        # eventually leave the window.
        self._price_history_size = max(self._dyn_stop_warmup * 3, 12)
        # Serialises register_fills + evaluate. Both are called from the
        # main loop today, but evaluate could be triggered from a separate
        # exit-only thread in the future and we don't want concurrent
        # _issue_exit calls firing two SELL orders for the same token.
        self._lock = threading.RLock()
        # Tokens currently mid-exit. Belt-and-braces against re-entry inside
        # the same evaluate() pass when `_decide_exit` matches multiple
        # rules at once.
        self._exiting_tokens: set[str] = set()

    @property
    def open_positions(self) -> dict[str, T2OpenPosition]:
        with self._lock:
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
        deadline_ts = _parse_iso_to_ts(market.end_date)
        model_prob_yes = float(signal_payload.get("model_prob") or 0.5)
        deviation = abs(float(signal_payload.get("deviation") or 0.0))

        with self._lock:
            self._register_fills_locked(
                signal_payload=signal_payload,
                trades=trades,
                market=market,
                deadline_ts=deadline_ts,
                model_prob_yes=model_prob_yes,
                deviation=deviation,
            )

    def _register_fills_locked(
        self,
        *,
        signal_payload: dict[str, Any],
        trades: list[TradeRecord],
        market: MarketInfo,
        deadline_ts: Optional[float],
        model_prob_yes: float,
        deviation: float,
    ) -> None:
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

            outcome_label = _resolve_trade_outcome_label(signal_payload, market, trade)
            model_prob = _resolve_position_model_prob(model_prob_yes, outcome_label)
            existing = self._positions.get(trade.token_id)
            if existing is not None:
                existing.add_fill(
                    float(fill_price),
                    float(fill_size),
                    now_ts=time.time(),
                    model_prob=model_prob,
                    deviation=deviation,
                )
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
                tranches_total=self._scale_out_tranches,
                price_history=deque(maxlen=self._price_history_size),
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

        with self._lock:
            for token_id in list(self._positions.keys()):
                pos = self._positions[token_id]
                if pos.exit_attempted or pos.abandoned:
                    self._positions.pop(token_id, None)
                    continue
                if token_id in self._exiting_tokens:
                    # Another evaluate() call is already mid-flight for this
                    # position. Skip to avoid double-SELL.
                    continue
                if pos.next_exit_retry_ts > now:
                    continue
                if now - pos.last_eval_ts < self._eval_interval_sec:
                    continue
                pos.last_eval_ts = now

                snap = self._ob.get_snapshot(token_id)
                if snap is None or snap.best_bid is None or snap.best_bid <= 0:
                    continue
                current_price = float(snap.best_bid)
                tick_size = float(getattr(snap, "tick_size", 0.01) or 0.01)

                market = markets_by_cid.get(pos.condition_id)
                # Refresh p_t before the exit decision so the Bellman
                # policy reflects the latest information. Stored on the
                # position for telemetry; `_decide_exit` reads it via
                # `_current_terminal_prob`.
                self._refresh_model_prob(pos, market)
                # Track price for dynamic-stop volatility estimation.
                # Prefer mid when available — bid alone aliases bid-ask
                # oscillation into "vol" which would over-widen the
                # stop. Fall back to current_price (bid) if mid is
                # missing.
                ref_px = float(snap.mid) if snap.mid is not None and snap.mid > 0 else current_price
                pos.price_history.append(ref_px)

                reason, fraction = self._decide_exit(pos, current_price, now)
                pos.last_decision_reason = reason
                if reason == "hold":
                    continue

                if market is None:
                    LOG.warning(
                        "T2 退出无法构造 opportunity（缺市场 %s），跳过",
                        pos.condition_id[:12],
                    )
                    continue

                # Escalation: after N failures we stop limit-selling at the
                # bid (which keeps missing on thin or vanished books) and
                # switch to a floor-price FAK to dump at any price. Past
                # the abandon threshold the position is released so it
                # stops starving the rest of the bot.
                use_floor = pos.exit_failure_count >= _FLOOR_EXIT_AFTER
                if use_floor:
                    # Floor-dump is an "abort" — exit the full remaining
                    # position regardless of scale-out plan.
                    fraction = 1.0
                exit_price = _FLOOR_PRICE if use_floor else current_price
                exit_size = max(0.0, pos.size_remaining * float(fraction))
                if exit_size <= 1e-6:
                    continue

                decision = {
                    "token_id": token_id,
                    "market_id": pos.market_id,
                    "outcome": pos.outcome_label,
                    "entry_price": pos.entry_price,
                    "exit_price": exit_price,
                    "size": exit_size,
                    "size_remaining_before": pos.size_remaining,
                    "fraction": round(float(fraction), 4),
                    "tranche_index": pos.tranches_executed + 1,
                    "tranches_total": pos.tranches_total,
                    "reason": "floor_dump" if use_floor else reason,
                    "model_prob_entry": round(float(pos.model_prob_at_entry), 4),
                    "model_prob_current": (
                        None
                        if pos.current_model_prob is None
                        else round(float(pos.current_model_prob), 4)
                    ),
                    "effective_stop_bps": (
                        None
                        if pos.last_effective_stop_bps is None
                        else round(float(pos.last_effective_stop_bps), 2)
                    ),
                    "vol_bps": (
                        None
                        if (vb := _realised_vol_bps(pos.price_history)) is None
                        else round(vb, 2)
                    ),
                    "ts": now,
                }
                self._exiting_tokens.add(token_id)
                try:
                    issued_status, fill_size = self._issue_exit(
                        pos, market, exit_price, exit_size=exit_size, tick_size=tick_size
                    )
                finally:
                    self._exiting_tokens.discard(token_id)

                decision["status"] = issued_status
                decision["fill_size"] = fill_size
                decision["remaining_size"] = pos.size_remaining
                result.attempted += 1
                result.decisions.append(decision)

                if issued_status == "exited":
                    pos.exit_attempted = True
                    result.triggered += 1
                    self._record_cooldown(pos, now)
                elif issued_status == "tranche_exited":
                    pos.tranches_executed += 1
                    pos.exit_failure_count = 0
                    pos.next_exit_retry_ts = 0.0
                    result.tranche_exits += 1
                    # If this was the last tranche but we still have a
                    # rounding dust, mark fully closed so the position
                    # doesn't linger on the next pass.
                    if (
                        pos.tranches_executed >= pos.tranches_total
                        or pos.size_remaining <= 1e-6
                    ):
                        pos.exit_attempted = True
                        result.triggered += 1
                        self._record_cooldown(pos, now)
                elif issued_status == "partial_exit":
                    pos.exit_failure_count = 0
                    pos.next_exit_retry_ts = 0.0
                    result.partial += 1
                else:
                    pos.exit_failure_count += 1
                    if pos.exit_failure_count >= _FLOOR_EXIT_AFTER:
                        # Once we're in floor-dump mode, retry quickly: at
                        # $0.01 the only reason FAK fails is no bid at all,
                        # which we should reconfirm fast, not back off for
                        # an hour. Caps the abandon path at ~10 minutes
                        # from the moment we enter floor mode.
                        pos.next_exit_retry_ts = now + max(60.0, self._eval_interval_sec)
                    else:
                        pos.next_exit_retry_ts = now + _exit_retry_backoff_sec(
                            self._eval_interval_sec,
                            pos.exit_failure_count,
                        )
                    result.failed += 1
                    # Escalation ladder. The three terminal-ish milestones
                    # (max retries / floor mode / abandon) keep their
                    # ERROR-level logs and notifications. The gap between
                    # them used to be silent — operators only saw events
                    # at counts 3, 10 and 20, so a position stuck at 5
                    # looked like a forgotten incident. We now emit a
                    # single WARNING per attempt once we're past
                    # `_MAX_EXIT_RETRIES`: the first two failures are
                    # left quiet because they're normal FAK retries.
                    if pos.exit_failure_count == _MAX_EXIT_RETRIES:
                        LOG.error(
                            "T2 退出失败 %d 次: token=%s — 仓位仍在追踪，可能需要人工干预",
                            pos.exit_failure_count,
                            token_id[:16],
                        )
                        self._notify_fatal_exit_failure(pos)
                    elif pos.exit_failure_count == _FLOOR_EXIT_AFTER:
                        LOG.error(
                            "T2 退出失败 %d 次: token=%s — 下次起切到地板价 %.2f 强平",
                            pos.exit_failure_count,
                            token_id[:16],
                            _FLOOR_PRICE,
                        )
                    elif pos.exit_failure_count >= _ABANDON_AFTER:
                        self._abandon_position(pos)
                    elif pos.exit_failure_count > _MAX_EXIT_RETRIES:
                        LOG.warning(
                            "T2 退出失败 %d 次: token=%s reason=%s 下一次重试 +%.0fs",
                            pos.exit_failure_count,
                            token_id[:16],
                            pos.last_decision_reason,
                            max(0.0, pos.next_exit_retry_ts - now),
                        )

            self._positions = {
                k: v for k, v in self._positions.items()
                if not v.exit_attempted and not v.abandoned and v.size_remaining > 1e-6
            }
        return result

    def _abandon_position(self, pos: T2OpenPosition) -> None:
        """Give up on a position that won't exit after _ABANDON_AFTER tries.

        Releases the booked risk exposure so RISK_MAX_OPEN_POSITIONS /
        RISK_MAX_TOTAL_EXPOSURE stop blocking new entries. The on-chain
        position itself is untouched — operator must reconcile manually
        (or wait for market settlement). portfolio_sync will re-detect
        the position on its next pass and re-book exposure if the chain
        still owns it, so this is a *temporary* unblock, not a delete.
        """
        if pos.abandoned:
            return
        pos.abandoned = True
        if self._risk_manager is not None:
            notional = max(0.0, float(pos.entry_price) * float(pos.size_remaining))
            try:
                self._risk_manager.release_market_exposure(pos.condition_id, notional)
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("放弃 T2 仓位时释放 exposure 失败: %s", exc)
            self._release_orchestrator_exposure(notional)
        LOG.error(
            "T2 仓位 ABANDON: token=%s 失败 %d 次后放弃在 in-memory 追踪并释放 exposure；"
            "链上仓位仍存在，等待 portfolio_sync 重新发现或人工处理。",
            pos.token_id[:16],
            pos.exit_failure_count,
        )
        # Lock the market out from immediate re-entry. The bot just gave up
        # on this position; the entry path should not pick it back up on
        # the next scan even if the statistical signal still looks good.
        self._record_cooldown(pos, time.time())
        if self._notifier is not None:
            try:
                self._notifier.notify_fatal_error(
                    (
                        "T2 仓位连续失败已放弃 in-memory 追踪，exposure 已释放。\n"
                        f"市场: {pos.market_question}\n"
                        f"token: {pos.token_id[:16]}\n"
                        f"失败次数: {pos.exit_failure_count}\n"
                        f"剩余 size: {pos.size_remaining:.4f}"
                    ),
                    error_key=f"t2_abandon:{pos.token_id}",
                )
            except Exception:  # pragma: no cover - notifier transport
                pass

    def remove_position(self, token_id: str) -> None:
        with self._lock:
            self._positions.pop(token_id, None)

    def _decide_exit(
        self, pos: T2OpenPosition, current_price: float, now: float
    ) -> tuple[str, float]:
        """Return (reason, fraction). fraction in (0, 1] of size_remaining.

        Abort triggers (time_stop / stop_loss) always exit the full
        remaining position — dragging out an "abort" decision across
        cycles is the wrong tradeoff. Happy-path triggers (take_profit /
        optimal_stopping) sell `1 / remaining_tranches` so the position
        scales out over `tranches_total` triggers.
        """
        # 1. Time stop.
        if self._max_hold_sec > 0 and now - pos.entry_ts >= self._max_hold_sec:
            return "time_stop", 1.0

        # 2. Stop-loss (adverse move from entry). Uses dynamic threshold
        # when enabled and warm, else falls back to the static
        # `t2_stop_loss_bps`. Threshold is cached on the position for
        # telemetry visibility.
        if pos.entry_price > 0:
            stop_bps = self._effective_stop_bps(pos)
            pos.last_effective_stop_bps = stop_bps
            if stop_bps > 0:
                adverse_bps = (pos.entry_price - current_price) / pos.entry_price * 10_000.0
                if adverse_bps >= stop_bps:
                    return "stop_loss", 1.0

        # 3. Take-profit when most of the entry deviation has been captured.
        if (
            pos.deviation_at_entry > 1e-6
            and self._take_profit_capture_pct > 0
        ):
            captured = current_price - pos.entry_price
            target = pos.deviation_at_entry * self._take_profit_capture_pct
            if captured >= target:
                return "take_profit", self._tranche_fraction(pos)

        # 4. Optimal stopping (Bellman policy).
        if self._optimal_stopping_enabled:
            terminal_prob = self._current_terminal_prob(pos)
            policy = self._get_policy(terminal_prob)
            remaining_steps = self._remaining_steps(pos, now)
            decision = policy.decide(remaining_steps, current_price)
            if decision.action == "STOP":
                return "optimal_stopping", self._tranche_fraction(pos)

        return "hold", 0.0

    def _effective_stop_bps(self, pos: T2OpenPosition) -> float:
        """Return the per-position stop-loss threshold in basis points.

        When dynamic stop is enabled AND warm (enough price history),
        threshold = clamp(k * realised_vol_bps, min_bps, max_bps).
        Otherwise falls back to the static `t2_stop_loss_bps`.
        """
        if not self._dyn_stop_enabled:
            return float(self._stop_loss_bps)
        vol_bps = _realised_vol_bps(pos.price_history)
        if vol_bps is None or len(pos.price_history) < self._dyn_stop_warmup:
            return float(self._stop_loss_bps)
        dynamic = self._dyn_stop_k * vol_bps
        return max(self._dyn_stop_min_bps, min(self._dyn_stop_max_bps, dynamic))

    def _current_terminal_prob(self, pos: T2OpenPosition) -> float:
        """Return the most-recent model belief P(token=1), or entry prob.

        `current_model_prob` is refreshed by `_refresh_model_prob` at the
        top of each evaluate() pass. If the provider couldn't produce a
        fresh estimate (no snapshot, no detector, exception), we fall
        back to the entry-time belief — which matches the legacy
        behaviour before rolling p_t was added.
        """
        if pos.current_model_prob is None:
            return float(pos.model_prob_at_entry)
        return float(pos.current_model_prob)

    def _refresh_model_prob(
        self, pos: T2OpenPosition, market: MarketInfo | None
    ) -> None:
        """Update `pos.current_model_prob` from the injected provider.

        Failures (no provider, no market, provider raised, provider
        returned None) leave `current_model_prob` unchanged — so a
        transient miss falls back to the previous estimate, not to entry.
        """
        if self._model_prob_provider is None or market is None:
            return
        try:
            estimate = self._model_prob_provider(market, pos.token_id)
        except Exception as exc:  # pragma: no cover - provider transport
            LOG.debug(
                "model_prob_provider raised for token=%s: %s",
                pos.token_id[:16],
                exc,
            )
            return
        if estimate is None:
            return
        prob = max(0.0, min(1.0, float(estimate)))
        pos.current_model_prob = prob

    def _tranche_fraction(self, pos: T2OpenPosition) -> float:
        """Fraction of `size_remaining` to sell for one happy-path trigger.

        Counter-based, not threshold-based: each successful trigger
        advances `tranches_executed` by 1, and we always slice the
        currently-remaining size by the number of tranches still to go.
        That way the last tranche cleans up whatever is left, and
        partial fills naturally re-tranche (a 1/3 ask that only fills
        50% leaves a smaller `size_remaining` which next cycle slices
        again into 1/3 of what remains).
        """
        total = max(1, int(pos.tranches_total))
        remaining = max(1, total - int(pos.tranches_executed))
        if remaining <= 1:
            return 1.0
        return 1.0 / remaining

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
        *,
        exit_size: float | None = None,
        tick_size: float = 0.01,
    ) -> tuple[str, float]:
        """Submit a single-leg SELL via ExecutionEngine.

        Returns (`status`, `filled_size`). ``exit_size`` is the size to
        sell on this call; defaults to the full remaining position for
        backward compatibility. The position is removed only when the
        full position has been closed (status="exited") — failed FOK
        attempts and intermediate tranche fills keep the position open.

        Statuses returned:
          - "exited": full position closed (size_remaining ≤ ε after fill)
          - "tranche_exited": requested slice fully filled, position remains
          - "partial_exit": some fill but less than requested
          - "exit_failed": no fill
        """
        size_to_sell = float(exit_size if exit_size is not None else pos.size_remaining)
        if size_to_sell <= 1e-6:
            return "exit_failed", 0.0

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
                virtual_fill_tier="T2_STATISTICAL",
                virtual_fill_context={
                    "tier": "STATISTICAL_ARB",
                    "phase": "exit",
                    "market_id": pos.condition_id,
                    "event_title": market.question,
                    "exit_reason": getattr(pos, "last_decision_reason", "") or "",
                },
            )
        except Exception as exc:  # pragma: no cover - depends on live client
            LOG.warning(
                "T2 退出失败: token=%s reason=%s err=%s",
                pos.token_id[:16],
                pos.last_decision_reason,
                exc,
            )
            self._notify_exit_failure(pos, f"exception={exc}")
            return "exit_failed", 0.0

        fill_size = _sell_fill_size(trades, pos.token_id)
        if fill_size > 0:
            pos.size_remaining = max(0.0, pos.size_remaining - fill_size)
            self._release_exit_exposure(pos, current_price, fill_size)

        successful = _is_successful_exit(self._executor, opp, trades)
        if successful and pos.size_remaining <= 1e-6:
            LOG.info(
                "T2 退出成交: token=%s reason=%s filled=%.2f price=%.4f trades=%d",
                pos.token_id[:16],
                pos.last_decision_reason,
                fill_size,
                current_price,
                len(trades),
            )
            return "exited", fill_size

        # Tranche slice fully filled but position still has remaining.
        if successful and pos.size_remaining > 1e-6:
            LOG.info(
                "T2 分批退出 (tranche %d/%d): token=%s reason=%s filled=%.2f remaining=%.2f price=%.4f",
                pos.tranches_executed + 1,
                pos.tranches_total,
                pos.token_id[:16],
                pos.last_decision_reason,
                fill_size,
                pos.size_remaining,
                current_price,
            )
            return "tranche_exited", fill_size

        if fill_size > 0:
            LOG.warning(
                "T2 退出部分成交: token=%s reason=%s filled=%.2f remaining=%.2f price=%.4f trades=%d",
                pos.token_id[:16],
                pos.last_decision_reason,
                fill_size,
                pos.size_remaining,
                current_price,
                len(trades),
            )
            return "partial_exit", fill_size

        statuses = ",".join(str(t.status.value) for t in trades) if trades else "none"
        errors = "; ".join(
            str(t.error).strip()
            for t in trades
            if str(t.error or "").strip()
        )
        LOG.warning(
            "T2 退出未成交: token=%s reason=%s price=%.4f trades=%d statuses=%s%s",
            pos.token_id[:16],
            pos.last_decision_reason,
            current_price,
            len(trades),
            statuses,
            f" errors={errors}" if errors else "",
        )
        self._notify_exit_failure(pos, f"statuses={statuses}" + (f" errors={errors}" if errors else ""))
        return "exit_failed", 0.0

    def _record_cooldown(self, pos: T2OpenPosition, now: float) -> None:
        if self._cooldown_store is None:
            return
        try:
            self._cooldown_store.record_exit(pos.condition_id, now_ts=now)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("post-exit cooldown 写入失败: %s", exc)

    def _release_exit_exposure(self, pos: T2OpenPosition, fill_price: float, fill_size: float) -> None:
        # 释放敞口必须按 entry_price 折算 —— RiskManager 入仓时
        # 记账的就是 entry_price * size。若用 fill_price，止损（fill < entry）
        # 会少释放、止盈（fill > entry）会多释放，导致 _market_exposure 漂移。
        booked_release = max(0.0, pos.entry_price * fill_size)
        if self._risk_manager is None:
            self._release_orchestrator_exposure(booked_release)
            return
        self._risk_manager.release_market_exposure(pos.condition_id, booked_release)
        self._release_orchestrator_exposure(booked_release)

    def _release_orchestrator_exposure(self, amount: float) -> None:
        if self._orchestrator is None or amount <= 0:
            return
        try:
            self._orchestrator.record_settlement(StrategyTier.STATISTICAL_ARB, amount, 0.0)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("T2 退出释放 orchestrator exposure 失败: %s", exc)

    def _notify_exit_failure(self, pos: T2OpenPosition, details: str) -> None:
        if self._notifier is None:
            return
        self._notifier.notify_trade_failure(
            event_title=pos.market_question,
            filled_legs=0,
            total_legs=1,
            simulated=self._config.dry_run,
            details=f"T2 exit failed token={pos.token_id[:16]} reason={pos.last_decision_reason} {details}".strip(),
        )

    def _notify_fatal_exit_failure(self, pos: T2OpenPosition) -> None:
        if self._notifier is None:
            return
        self._notifier.notify_fatal_error(
            (
                "T2 退出连续失败，仓位仍在追踪并进入退避重试。\n"
                f"市场: {pos.market_question}\n"
                f"token: {pos.token_id[:16]}\n"
                f"失败次数: {pos.exit_failure_count}"
            ),
            error_key=f"t2_exit:{pos.token_id}",
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


def _resolve_trade_outcome_label(
    signal_payload: dict[str, Any],
    market: MarketInfo,
    trade: TradeRecord,
) -> str:
    for token in market.tokens:
        if token.token_id == trade.token_id and token.outcome:
            return str(token.outcome)

    action = _resolve_signal_action(signal_payload)
    if action == "BUY_NO":
        return "No"
    return "Yes"


def _resolve_signal_action(signal_payload: dict[str, Any]) -> str:
    direct = str(signal_payload.get("action") or "").upper()
    if direct:
        return direct
    execution_check = signal_payload.get("execution_check")
    if isinstance(execution_check, dict):
        action = str(execution_check.get("action") or "").upper()
        if action:
            return action
    research_overlay = signal_payload.get("research_overlay")
    if isinstance(research_overlay, dict):
        action = str(research_overlay.get("action") or "").upper()
        if action:
            return action
    signal_type = str(signal_payload.get("signal_type") or "").upper()
    if signal_type.endswith("_NO") or "BUY_NO" in signal_type:
        return "BUY_NO"
    if signal_type.endswith("_YES") or "BUY_YES" in signal_type:
        return "BUY_YES"
    return "BUY_YES"


def _resolve_position_model_prob(model_prob_yes: float, outcome_label: str) -> float:
    model_prob_yes = max(0.0, min(1.0, float(model_prob_yes)))
    if str(outcome_label).strip().lower() == "no":
        return 1.0 - model_prob_yes
    return model_prob_yes


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


def _realised_vol_bps(prices: deque[float]) -> float | None:
    """Stdev of log returns of consecutive prices, expressed in bps.

    Returns None if there are fewer than 3 prices, or if the prices are
    degenerate (all equal, or any non-positive). The bps unit is "bps
    move per observation interval" — the caller multiplies by k to
    decide the stop threshold.
    """
    if len(prices) < 3:
        return None
    valid = [p for p in prices if p > 0]
    if len(valid) < 3:
        return None
    log_returns: list[float] = []
    for prev, curr in zip(valid[:-1], valid[1:]):
        try:
            log_returns.append(math.log(curr / prev))
        except (ValueError, ZeroDivisionError):
            continue
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    if var <= 0:
        return None
    return math.sqrt(var) * 10_000.0


def _exit_retry_backoff_sec(eval_interval_sec: float, failure_count: int) -> float:
    base = max(30.0, float(eval_interval_sec or 0.0))
    exponent = max(0, int(failure_count) - 1)
    return min(3600.0, base * (2 ** exponent))


def _is_successful_exit(
    executor: ExecutionEngine,
    opp: ArbOpportunity,
    trades: list[TradeRecord],
) -> bool:
    checker = getattr(executor, "is_successful_execution", None)
    if callable(checker):
        return bool(checker(opp, trades))
    return (
        len(trades) == len(opp.legs)
        and all(t.status == TradeStatus.FILLED for t in trades)
    )


def t2_exit_telemetry(result: T2ExitResult, open_count: int) -> dict[str, Any]:
    return {
        "attempted": result.attempted,
        "triggered": result.triggered,
        "tranche_exits": result.tranche_exits,
        "partial": result.partial,
        "failed": result.failed,
        "open_positions": open_count,
        "decisions": [
            {
                "token_id": d["token_id"][:16],
                "market_id": d["market_id"][:12],
                "outcome": d["outcome"],
                "reason": d["reason"],
                "status": d.get("status", ""),
                "entry_price": round(float(d["entry_price"]), 4),
                "exit_price": round(float(d["exit_price"]), 4),
                "size": round(float(d["size"]), 4),
                "fraction": round(float(d.get("fraction") or 1.0), 4),
                "tranche_index": int(d.get("tranche_index") or 1),
                "tranches_total": int(d.get("tranches_total") or 1),
                "fill_size": round(float(d.get("fill_size") or 0.0), 4),
                "remaining_size": round(float(d.get("remaining_size") or 0.0), 4),
                "model_prob_entry": d.get("model_prob_entry"),
                "model_prob_current": d.get("model_prob_current"),
                "effective_stop_bps": d.get("effective_stop_bps"),
                "vol_bps": d.get("vol_bps"),
            }
            for d in result.decisions
        ],
    }
