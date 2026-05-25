"""风险管理器：追踪持仓、敞口、盈亏，执行风控策略.

风控规则:
1. 最大持仓数量限制
2. 单市场最大敞口限制
3. 全局最大敞口限制
4. 日亏损止损线
5. 连续失败次数熔断
6. 同一市场去重（防止重复套利）
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import (
    ArbOpportunity,
    PositionSnapshot,
    RiskState,
    TradeRecord,
    TradeStatus,
)

LOG = logging.getLogger(__name__)


@dataclass
class PendingReservation:
    """Capital reserved against a still-open order.

    Replaces the prior 4-tuple `(condition_id, exposure, ts, consumes_slot)`,
    which forced positional unpacking everywhere and made adding fields a
    silent breakage.
    """

    condition_id: str
    exposure: float
    created_ts: float
    consumes_slot: bool


class RiskManager:
    """全局风险管理.

    Thread-safety: dashboard_api reads state from the HTTP thread while the
    main loop mutates it from the scan/exec path. All public methods take
    `_lock` (an RLock so internal helpers can recurse), and `state` returns a
    cheap shallow copy so readers never observe a half-applied update.
    """

    def __init__(self, config: ArbConfig):
        self._config = config
        self._state = RiskState()
        self._market_exposure: dict[str, float] = {}  # condition_id -> 敞口
        self._recent_arb_markets: dict[str, float] = {}  # event_id -> 最后执行时间
        self._pending_reservations: dict[str, PendingReservation] = {}
        self._daily_reset_ts: float = _start_of_day()
        self._effective_max_total_exposure = config.max_total_exposure
        self._effective_max_daily_loss = config.max_daily_loss
        self._pending_reservation_ttl_sec = config.risk_pending_reservation_ttl_sec
        self._halt_time: float | None = None
        self._last_reject: dict[str, Any] = {}
        self._shadow_snapshot: dict[str, Any] | None = None
        # RLock so e.g. `pre_trade_check` can call `_reconcile_pending_reservations`
        # without deadlocking on itself.
        self._lock = threading.RLock()

    @property
    def state(self) -> RiskState:
        with self._lock:
            self._maybe_reset_daily()
            self._reconcile_pending_reservations()
            self._apply_shadow_snapshot_locked()
            return _snapshot_risk_state(self._state)

    @property
    def halt_time(self) -> float | None:
        """UTC epoch seconds when the circuit breaker most recently tripped.

        ``None`` when the bot is not halted. Read-only — exposed so callers
        (e.g. the notifier) can render an auto-recovery countdown without
        reaching into private state.
        """
        return self._halt_time

    @property
    def last_reject(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_reject)

    def update_shadow_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        """Use the shadow lifecycle ledger as dry-run risk state.

        In dry-run, portfolio sync correctly reports the real wallet as flat,
        but the strategy needs the simulated ledger to gate new entries. This
        method keeps live trading untouched while making shadow runs respect
        max open positions, exposure, and daily loss limits on the next check.
        """
        if not self._config.dry_run:
            return
        with self._lock:
            self._shadow_snapshot = dict(snapshot or {})
            self._apply_shadow_snapshot_locked()

    def pre_trade_check(self, opp: ArbOpportunity, proposed_size: float) -> tuple[bool, str, float]:
        """交易前风控检查.

        Returns:
            (允许交易, 原因, 调整后的数量)
        """
        with self._lock:
            allowed, reason, size = self._pre_trade_check_locked(opp, proposed_size)
            if allowed:
                self._last_reject = {}
            return allowed, reason, size

    def _pre_trade_check_locked(self, opp: ArbOpportunity, proposed_size: float) -> tuple[bool, str, float]:
        self._maybe_reset_daily()
        self._reconcile_pending_reservations()
        self._apply_shadow_snapshot_locked()

        if (
            self._state.is_halted
            and self._config.risk_halt_auto_recover_sec > 0
            and self._halt_time is not None
            and time.time() - self._halt_time >= self._config.risk_halt_auto_recover_sec
        ):
            self._state.is_halted = False
            self._state.halt_reason = ""
            self._state.consecutive_failures = 0
            self._halt_time = None
            LOG.info("风控熔断自动解除（无新失败超过 %.0f 秒）", self._config.risk_halt_auto_recover_sec)

        can, reason = self._state.check_can_trade(
            max_positions=self._config.max_open_positions,
            max_total_exposure=self._effective_max_total_exposure,
            max_daily_loss=self._effective_max_daily_loss,
            max_failures=self._config.max_consecutive_failures,
        )
        if not can:
            structured_code = _reason_code_from_risk_state_reason(reason)
            self._set_last_reject("risk_state_gate", reason, {})
            if structured_code != "risk_state_gate":
                self._last_reject["reason_code"] = structured_code
            return False, reason, 0.0

        # Portfolio-sync circuit breaker: when sync has failed N consecutive
        # times the in-memory exposure / position state is stale and risk caps
        # cannot be trusted. Only enforced when portfolio_sync is required for
        # live trading; dry-run still records the failure count for telemetry.
        if (
            self._config.portfolio_sync_enabled
            and self._config.live_require_portfolio_sync
            and self._config.portfolio_sync_max_consecutive_failures > 0
            and self._state.portfolio_sync_consecutive_failures
            >= self._config.portfolio_sync_max_consecutive_failures
        ):
            reason = (
                f"账户同步连续失败 {self._state.portfolio_sync_consecutive_failures} 次，"
                f"已暂停开仓直到下一次成功同步"
            )
            self._set_last_reject(
                "portfolio_sync_gate",
                reason,
                {
                    "consecutive_failures": self._state.portfolio_sync_consecutive_failures,
                    "max_consecutive_failures": self._config.portfolio_sync_max_consecutive_failures,
                },
            )
            return (
                False,
                reason,
                0.0,
            )

        event_id = opp.event_id
        if event_id in self._recent_arb_markets:
            last_ts = self._recent_arb_markets[event_id]
            age_sec = time.time() - last_ts
            if age_sec < self._config.risk_event_cooldown_sec:
                reason = f"事件 {event_id} {self._format_cooldown_label()}内已执行过套利"
                self._set_last_reject(
                    "event_cooldown",
                    reason,
                    {
                        "event_id": event_id,
                        "cooldown_sec": self._config.risk_event_cooldown_sec,
                        "age_sec": age_sec,
                    },
                )
                return False, reason, 0.0

        for market in opp.markets:
            cid = market.condition_id
            current_exposure = self._market_exposure.get(cid, 0.0)
            if current_exposure >= self._config.max_exposure_per_market:
                reason = f"市场 {cid[:12]} 敞口已达上限"
                self._set_last_reject(
                    "market_exposure_cap",
                    reason,
                    {
                        "market_id": cid,
                        "current_exposure": current_exposure,
                        "max_exposure_per_market": self._config.max_exposure_per_market,
                    },
                )
                return False, reason, 0.0

        remaining_total = self._effective_max_total_exposure - self._state.total_exposure
        if remaining_total <= 0:
            self._set_last_reject(
                "total_exposure_cap",
                "全局敞口已满",
                {
                    "total_exposure": self._state.total_exposure,
                    "max_total_exposure": self._effective_max_total_exposure,
                },
            )
            return False, "全局敞口已满", 0.0

        per_market_size_limit = float("inf")
        exposure_per_share_by_market: dict[str, float] = {}
        for leg in opp.legs:
            leg_exposure = leg.economic_cost if leg.economic_cost is not None else leg.price
            if leg_exposure <= 0:
                continue
            exposure_per_share_by_market[leg.condition_id] = (
                exposure_per_share_by_market.get(leg.condition_id, 0.0) + leg_exposure
            )

        for market in opp.markets:
            cid = market.condition_id
            used = self._market_exposure.get(cid, 0.0)
            remaining = self._config.max_exposure_per_market - used
            if remaining <= 0:
                reason = f"市场 {cid[:12]} 敞口已达上限"
                self._set_last_reject(
                    "market_exposure_cap",
                    reason,
                    {
                        "market_id": cid,
                        "current_exposure": used,
                        "max_exposure_per_market": self._config.max_exposure_per_market,
                    },
                )
                return False, reason, 0.0
            per_share_market_exposure = exposure_per_share_by_market.get(cid, 0.0)
            if per_share_market_exposure > 0:
                per_market_size_limit = min(per_market_size_limit, remaining / per_share_market_exposure)

        max_affordable_size = min(
            proposed_size,
            remaining_total / opp.total_cost if opp.total_cost > 0 else 0,
            per_market_size_limit,
            opp.max_executable_size,
        )

        if max_affordable_size <= 0:
            self._set_last_reject(
                "zero_size_after_risk",
                "风控后可执行数量为 0",
                {
                    "proposed_size": proposed_size,
                    "remaining_total": remaining_total,
                    "max_executable_size": opp.max_executable_size,
                    "per_market_size_limit": per_market_size_limit,
                },
            )
            return False, "风控后可执行数量为 0", 0.0

        return True, "", max_affordable_size

    def _set_last_reject(self, reason_code: str, reason: str, context: dict[str, Any]) -> None:
        self._last_reject = {
            "reason_code": reason_code,
            "reason": reason,
            "reason_context": dict(context),
        }

    def record_execution(
        self,
        opp: ArbOpportunity,
        trades: list[TradeRecord],
        *,
        realized_pnl: float | None = None,
        count_pending_as_failure: bool = True,
    ) -> None:
        """记录交易执行结果，更新风险状态."""
        with self._lock:
            self._record_execution_locked(
                opp,
                trades,
                realized_pnl=realized_pnl,
                count_pending_as_failure=count_pending_as_failure,
            )

    def _record_execution_locked(
        self,
        opp: ArbOpportunity,
        trades: list[TradeRecord],
        *,
        realized_pnl: float | None,
        count_pending_as_failure: bool,
    ) -> None:
        self._maybe_reset_daily()
        filled_trades = [t for t in trades if t.status == TradeStatus.FILLED]
        partially_filled_trades = [t for t in trades if t.status == TradeStatus.PARTIAL]
        pending_trades = [t for t in trades if t.status == TradeStatus.PENDING]
        actual_exposure_trades = filled_trades + partially_filled_trades
        failed_trades = [t for t in trades if t.status in (TradeStatus.FAILED, TradeStatus.CANCELLED)]
        execution_success = (
            len(trades) == len(opp.legs)
            and len(filled_trades) == len(opp.legs)
            and not failed_trades
        )
        event_should_cooldown = bool(actual_exposure_trades or pending_trades)

        only_pending_submission = (
            bool(pending_trades)
            and not actual_exposure_trades
            and not failed_trades
        )

        has_any_fills = bool(actual_exposure_trades)
        if execution_success:
            self._state.consecutive_failures = 0
            self._halt_time = None
        elif has_any_fills:
            self._state.consecutive_failures += 1
            if self._state.consecutive_failures >= self._config.max_consecutive_failures > 0:
                self._state.is_halted = True
                self._halt_time = time.time()
                self._state.halt_reason = f"连续失败 {self._state.consecutive_failures} 次"
                LOG.error("风控熔断: %s", self._state.halt_reason)
        elif failed_trades and not (only_pending_submission and not count_pending_as_failure):
            # 零成交且有失败腿：计入连续失败
            self._state.consecutive_failures += 1
            if self._state.consecutive_failures >= self._config.max_consecutive_failures > 0:
                self._state.is_halted = True
                self._halt_time = time.time()
                self._state.halt_reason = f"连续失败 {self._state.consecutive_failures} 次"
                LOG.error("风控熔断: %s", self._state.halt_reason)

        buy_exposure_trades = [t for t in actual_exposure_trades if _trade_side_value(t) == "BUY"]
        sell_exposure_trades = [t for t in actual_exposure_trades if _trade_side_value(t) == "SELL"]

        actual_cost = sum(
            (t.economic_cost if t.economic_cost is not None else t.price)
            * _resolved_exposure_size(t)
            for t in buy_exposure_trades
        )
        pending_cost = sum(
            (t.economic_cost if t.economic_cost is not None else t.price) * t.size
            for t in pending_trades
            if _trade_side_value(t) == "BUY"
        )
        self._state.total_exposure += actual_cost + pending_cost

        for t in buy_exposure_trades:
            cid = t.condition_id
            leg_cost = t.economic_cost if t.economic_cost is not None else t.price
            exposure_size = _resolved_exposure_size(t)
            exposure = leg_cost * exposure_size
            self._market_exposure[cid] = self._market_exposure.get(cid, 0.0) + exposure

        for t in sell_exposure_trades:
            leg_cost = t.economic_cost if t.economic_cost is not None else t.price
            exposure = leg_cost * _resolved_exposure_size(t)
            self.release_market_exposure(t.condition_id, exposure)

        for t in pending_trades:
            if _trade_side_value(t) != "BUY":
                continue
            cid = t.condition_id
            leg_cost = t.economic_cost if t.economic_cost is not None else t.price
            exposure = leg_cost * t.size
            self._market_exposure[cid] = self._market_exposure.get(cid, 0.0) + exposure
            reservation_key = t.order_id or t.trade_id
            self._pending_reservations[reservation_key] = PendingReservation(
                condition_id=cid,
                exposure=exposure,
                created_ts=time.time(),
                consumes_slot=not bool(getattr(t, "post_only", False)),
            )

        self._state.open_positions = self._compute_open_positions()
        self._apply_shadow_snapshot_locked()

        if event_should_cooldown:
            self._recent_arb_markets[opp.event_id] = time.time()

        if realized_pnl is not None:
            self._state.daily_pnl += float(realized_pnl)
            self._state.total_pnl = self._state.daily_pnl + self._state.unrealized_pnl

        LOG.info(
            "风控状态: 持仓=%d, 总敞口=$%.2f, 日盈亏=$%.2f, 连续失败=%d",
            self._state.open_positions,
            self._state.total_exposure,
            self._state.daily_pnl,
            self._state.consecutive_failures,
        )

    def record_settlement(self, condition_id: str, pnl: float) -> None:
        """记录市场结算后的盈亏."""
        with self._lock:
            self._maybe_reset_daily()
            self._state.daily_pnl += pnl
            self._state.total_pnl = self._state.daily_pnl + self._state.unrealized_pnl
            exposure = self._market_exposure.pop(condition_id, 0.0)
            self._state.total_exposure = max(0, self._state.total_exposure - exposure)
            self._pending_reservations = {
                key: reservation
                for key, reservation in self._pending_reservations.items()
                if reservation.condition_id != condition_id
            }
            self._state.open_positions = self._compute_open_positions()

    def release_market_exposure(self, condition_id: str, exposure: float) -> None:
        """Release booked exposure immediately after a confirmed SELL fill."""
        amount = max(0.0, float(exposure))
        if not condition_id or amount <= 0:
            return
        with self._lock:
            current = self._market_exposure.get(condition_id, 0.0)
            released = min(current, amount)
            remaining = max(0.0, current - amount)
            if remaining > 1e-9:
                self._market_exposure[condition_id] = remaining
            else:
                self._market_exposure.pop(condition_id, None)
            self._state.total_exposure = max(0.0, self._state.total_exposure - released)
            self._state.open_positions = self._compute_open_positions()

    def reconcile_pending_order_statuses(self, trades: list[TradeRecord]) -> None:
        """根据订单状态同步结果修正 pending 预留敞口.

        目前主要用于 live maker/GTC 订单:
        - `PENDING` / `PARTIAL`: 续租预留，避免 TTL 误释放仍在交易所挂着的订单
        - `FILLED`: 释放预留并按最终成交数量落地真实敞口
        - `FAILED` / `CANCELLED`: 释放预留
        """
        with self._lock:
            now = time.time()
            updated = False

            for trade in trades:
                reservation_key = trade.order_id or trade.trade_id
                if not reservation_key:
                    continue

                reservation = self._pending_reservations.get(reservation_key)
                if reservation is None:
                    continue

                if trade.status in (TradeStatus.PENDING, TradeStatus.PARTIAL):
                    consumes_slot = reservation.consumes_slot or trade.status == TradeStatus.PARTIAL
                    self._pending_reservations[reservation_key] = PendingReservation(
                        condition_id=reservation.condition_id,
                        exposure=reservation.exposure,
                        created_ts=now,
                        consumes_slot=consumes_slot,
                    )
                    if consumes_slot != reservation.consumes_slot:
                        updated = True
                    continue

                self._pending_reservations.pop(reservation_key, None)
                current = self._market_exposure.get(reservation.condition_id, 0.0)
                leg_cost = trade.economic_cost if trade.economic_cost is not None else trade.price

                terminal_fill_exposure = leg_cost * _resolved_exposure_size(trade)
                if trade.status == TradeStatus.FILLED:
                    final_exposure = terminal_fill_exposure
                    delta = final_exposure - reservation.exposure
                    action = "成交落地"
                elif terminal_fill_exposure > 0:
                    # A resting order can partially fill and then be cancelled.
                    # Release only the unfilled reservation; keep filled inventory
                    # booked as live exposure.
                    final_exposure = leg_cost * _resolved_exposure_size(trade)
                    delta = final_exposure - reservation.exposure
                    action = "部分成交后释放剩余挂单"
                else:
                    delta = -reservation.exposure
                    action = "释放挂单"

                remaining = max(0.0, current + delta)
                if remaining > 0:
                    self._market_exposure[reservation.condition_id] = remaining
                else:
                    self._market_exposure.pop(reservation.condition_id, None)
                self._state.total_exposure = max(0.0, self._state.total_exposure + delta)
                updated = True
                LOG.info(
                    "同步订单后修正敞口: action=%s key=%s market=%s delta=$%.4f status=%s",
                    action,
                    reservation_key[:16],
                    reservation.condition_id[:12],
                    delta,
                    trade.status.value,
                )

            if updated:
                self._state.open_positions = self._compute_open_positions()

    def sync_portfolio_snapshot(
        self,
        positions: list[PositionSnapshot],
        *,
        realized_daily_pnl: float,
        synced_at: float,
    ) -> None:
        """用账户真实状态刷新持仓和已实现日盈亏."""
        with self._lock:
            self._maybe_reset_daily()
            self._reconcile_pending_reservations()
            # P1-3: 同步会直接覆盖 daily_pnl。如果与内存累计差距过大，
            # 说明本地 record_execution / record_settlement 跟链上结算之间
            # 有遗漏（漏记或重复记），先告警再覆盖，便于事后追源。
            prior_daily_pnl = float(self._state.daily_pnl)
            divergence = float(realized_daily_pnl) - prior_daily_pnl
            if abs(divergence) > 1.0:
                LOG.warning(
                    "账户同步: daily_pnl 内存值 $%.4f 与账户值 $%.4f 分歧 $%+.4f，以账户为准",
                    prior_daily_pnl,
                    float(realized_daily_pnl),
                    divergence,
                )

            actual_market_exposure: dict[str, float] = {}
            normalized_positions: list[PositionSnapshot] = []
            current_position_value = 0.0
            unrealized_pnl = 0.0
            for position in positions:
                size = max(0.0, float(position.size))
                if size <= 0:
                    continue
                avg_price = max(0.0, float(position.avg_price))
                position_value = max(0.0, float(position.current_value))
                position_unrealized = float(position.unrealized_pnl)
                normalized = PositionSnapshot(
                    token_id=position.token_id,
                    condition_id=position.condition_id,
                    outcome=position.outcome,
                    size=size,
                    avg_price=avg_price,
                    current_value=position_value,
                    unrealized_pnl=position_unrealized,
                )
                normalized_positions.append(normalized)
                current_position_value += position_value
                unrealized_pnl += position_unrealized
                actual_market_exposure[normalized.condition_id] = (
                    actual_market_exposure.get(normalized.condition_id, 0.0) + (normalized.avg_price * normalized.size)
                )

            self._state.positions = normalized_positions
            self._state.daily_pnl = float(realized_daily_pnl)
            self._state.current_position_value = current_position_value
            self._state.unrealized_pnl = unrealized_pnl
            self._state.total_pnl = self._state.daily_pnl + self._state.unrealized_pnl
            self._state.last_portfolio_sync_ts = float(synced_at)
            self._state.portfolio_sync_ok = True
            self._state.portfolio_sync_error = ""
            self._state.portfolio_pnl_stale = False
            if self._state.portfolio_sync_consecutive_failures > 0:
                LOG.info(
                    "账户同步恢复，重置连续失败计数 %d -> 0",
                    self._state.portfolio_sync_consecutive_failures,
                )
            self._state.portfolio_sync_consecutive_failures = 0

            merged_exposure = dict(actual_market_exposure)
            for reservation in self._pending_reservations.values():
                merged_exposure[reservation.condition_id] = (
                    merged_exposure.get(reservation.condition_id, 0.0) + reservation.exposure
                )
            self._market_exposure = merged_exposure
            self._state.total_exposure = sum(merged_exposure.values())
            self._state.open_positions = self._compute_open_positions()

    def mark_portfolio_sync_error(self, message: str, *, synced_at: float | None = None) -> None:
        with self._lock:
            self._state.portfolio_sync_ok = False
            self._state.portfolio_sync_error = message
            self._state.portfolio_sync_consecutive_failures += 1
            self._state.portfolio_pnl_stale = True
            if synced_at is not None:
                self._state.last_portfolio_sync_ts = float(synced_at)
            cap = self._config.portfolio_sync_max_consecutive_failures
            if cap > 0 and self._state.portfolio_sync_consecutive_failures >= cap:
                LOG.warning(
                    "账户同步连续失败 %d 次（阈值 %d）：实盘已暂停开仓直到下次同步成功",
                    self._state.portfolio_sync_consecutive_failures,
                    cap,
                )

    def reset_halt(self) -> None:
        """手动解除熔断."""
        with self._lock:
            self._state.is_halted = False
            self._state.halt_reason = ""
            self._state.consecutive_failures = 0
            self._halt_time = None
            LOG.info("风控熔断已手动解除")

    def _format_cooldown_label(self) -> str:
        seconds = self._config.risk_event_cooldown_sec
        if abs(seconds - round(seconds)) < 1e-9:
            return f"{int(round(seconds))}秒"
        return f"{seconds:.1f}秒"

    def _maybe_reset_daily(self) -> None:
        """检查是否需要重置日盈亏."""
        today = _start_of_day()
        if today > self._daily_reset_ts:
            LOG.info("日切: 重置日盈亏 $%.2f -> $0.00", self._state.daily_pnl)
            self._state.daily_pnl = 0.0
            self._state.total_pnl = self._state.unrealized_pnl
            self._daily_reset_ts = today

    def _reconcile_pending_reservations(self) -> None:
        now = time.time()
        expired_keys = [
            key for key, reservation in self._pending_reservations.items()
            if now - reservation.created_ts >= self._pending_reservation_ttl_sec
        ]
        for key in expired_keys:
            reservation = self._pending_reservations.pop(key)
            current = self._market_exposure.get(reservation.condition_id, 0.0)
            remaining = max(0.0, current - reservation.exposure)
            if remaining > 0:
                self._market_exposure[reservation.condition_id] = remaining
            else:
                self._market_exposure.pop(reservation.condition_id, None)
            self._state.total_exposure = max(0.0, self._state.total_exposure - reservation.exposure)
            LOG.info(
                "释放过期预留敞口: key=%s, market=%s, exposure=$%.4f",
                key[:16],
                reservation.condition_id[:12],
                reservation.exposure,
            )

            self._state.open_positions = self._compute_open_positions()

    def _apply_shadow_snapshot_locked(self) -> None:
        if not self._config.dry_run or not self._shadow_snapshot:
            return
        snap = self._shadow_snapshot
        self._state.daily_pnl = _safe_float(snap.get("realized_pnl"), self._state.daily_pnl)
        self._state.unrealized_pnl = _safe_float(snap.get("unrealized_pnl"), self._state.unrealized_pnl)
        self._state.total_pnl = _safe_float(
            snap.get("total_pnl"),
            self._state.daily_pnl + self._state.unrealized_pnl,
        )
        self._state.current_position_value = _safe_float(
            snap.get("current_position_value"),
            self._state.current_position_value,
        )
        self._state.total_exposure = _safe_float(
            snap.get("open_cost"),
            self._state.total_exposure,
        )
        try:
            self._state.open_positions = max(0, int(snap.get("open_lots", self._state.open_positions)))
        except (TypeError, ValueError):
            pass

    def _compute_open_positions(self) -> int:
        slotless_pending_by_market: dict[str, float] = {}
        for reservation in self._pending_reservations.values():
            if reservation.consumes_slot:
                continue
            slotless_pending_by_market[reservation.condition_id] = (
                slotless_pending_by_market.get(reservation.condition_id, 0.0) + reservation.exposure
            )

        count = 0
        for condition_id, exposure in self._market_exposure.items():
            effective_exposure = float(exposure) - float(slotless_pending_by_market.get(condition_id, 0.0))
            if effective_exposure > 1e-9:
                count += 1
        return count

    def format_status_zh(self) -> str:
        """格式化风控状态为中文文本."""
        s = self._state
        lines = [
            "📊 风控状态",
            f"持仓数: {s.open_positions}/{self._config.max_open_positions}",
            f"总敞口: ${s.total_exposure:.2f}/${self._config.max_total_exposure:.2f}",
            f"已实现日盈亏: ${s.daily_pnl:+.2f} (止损线: -${self._config.max_daily_loss:.2f})",
            f"未实现盈亏: ${s.unrealized_pnl:+.2f} | 合计: ${s.total_pnl:+.2f}",
            f"连续失败: {s.consecutive_failures}/{self._config.max_consecutive_failures}",
            f"状态: {'🔴 已暂停 - ' + s.halt_reason if s.is_halted else '🟢 正常'}",
        ]
        return "\n".join(lines)


def _snapshot_risk_state(state: RiskState) -> RiskState:
    """Return a shallow copy so HTTP readers cannot observe a half-applied write.

    Lists/dicts inside RiskState are recreated; nested dataclasses
    (PositionSnapshot) are immutable enough at the field level to share by
    reference without confusing the dashboard.
    """
    return RiskState(
        total_exposure=state.total_exposure,
        open_positions=state.open_positions,
        daily_pnl=state.daily_pnl,
        unrealized_pnl=state.unrealized_pnl,
        total_pnl=state.total_pnl,
        current_position_value=state.current_position_value,
        consecutive_failures=state.consecutive_failures,
        is_halted=state.is_halted,
        halt_reason=state.halt_reason,
        positions=list(state.positions),
        last_portfolio_sync_ts=state.last_portfolio_sync_ts,
        portfolio_sync_ok=state.portfolio_sync_ok,
        portfolio_sync_error=state.portfolio_sync_error,
        portfolio_sync_consecutive_failures=state.portfolio_sync_consecutive_failures,
        portfolio_pnl_stale=state.portfolio_pnl_stale,
    )


def _start_of_day() -> float:
    """当天 00:00 UTC 的时间戳."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def _safe_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return parsed


def _resolved_execution_size(trade: TradeRecord) -> float:
    if trade.fill_size is not None:
        return float(trade.fill_size)
    if trade.status == TradeStatus.FILLED:
        return float(trade.size)
    return 0.0


def _resolved_exposure_size(trade: TradeRecord) -> float:
    return _resolved_execution_size(trade)


def _trade_side_value(trade: TradeRecord) -> str:
    return str(getattr(trade.side, "value", trade.side)).upper()


def _reason_code_from_risk_state_reason(reason: str) -> str:
    text = str(reason or "")
    if text.startswith("交易已暂停"):
        return "risk_halted"
    if text.startswith("持仓数 "):
        return "open_position_cap"
    if text.startswith("总敞口 "):
        return "total_exposure_cap"
    if text.startswith("日亏损 "):
        return "daily_loss_stop"
    if text.startswith("连续失败 "):
        return "consecutive_failure_halt"
    return "risk_state_gate"
