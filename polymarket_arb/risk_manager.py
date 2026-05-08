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
import time

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import (
    ArbOpportunity,
    PositionSnapshot,
    RiskState,
    TradeRecord,
    TradeStatus,
)

LOG = logging.getLogger(__name__)


class RiskManager:
    """全局风险管理."""

    def __init__(self, config: ArbConfig):
        self._config = config
        self._state = RiskState()
        self._market_exposure: dict[str, float] = {}  # condition_id -> 敞口
        self._recent_arb_markets: dict[str, float] = {}  # event_id -> 最后执行时间
        self._pending_reservations: dict[str, tuple[str, float, float, bool]] = {}
        self._daily_reset_ts: float = _start_of_day()
        self._effective_max_total_exposure = config.max_total_exposure
        self._effective_max_daily_loss = config.max_daily_loss
        self._pending_reservation_ttl_sec = config.risk_pending_reservation_ttl_sec
        self._halt_time: float | None = None

    @property
    def state(self) -> RiskState:
        self._reconcile_pending_reservations()
        return self._state

    def pre_trade_check(self, opp: ArbOpportunity, proposed_size: float) -> tuple[bool, str, float]:
        """交易前风控检查.

        Returns:
            (允许交易, 原因, 调整后的数量)
        """
        self._maybe_reset_daily()
        self._reconcile_pending_reservations()

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
            return (
                False,
                f"账户同步连续失败 {self._state.portfolio_sync_consecutive_failures} 次，"
                f"已暂停开仓直到下一次成功同步",
                0.0,
            )

        event_id = opp.event_id
        if event_id in self._recent_arb_markets:
            last_ts = self._recent_arb_markets[event_id]
            if time.time() - last_ts < self._config.risk_event_cooldown_sec:
                return False, f"事件 {event_id} {self._format_cooldown_label()}内已执行过套利", 0.0

        for market in opp.markets:
            cid = market.condition_id
            current_exposure = self._market_exposure.get(cid, 0.0)
            if current_exposure >= self._config.max_exposure_per_market:
                return False, f"市场 {cid[:12]} 敞口已达上限", 0.0

        remaining_total = self._effective_max_total_exposure - self._state.total_exposure
        if remaining_total <= 0:
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
                return False, f"市场 {cid[:12]} 敞口已达上限", 0.0
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
            return False, "风控后可执行数量为 0", 0.0

        return True, "", max_affordable_size

    def record_execution(
        self,
        opp: ArbOpportunity,
        trades: list[TradeRecord],
        *,
        realized_pnl: float | None = None,
        count_pending_as_failure: bool = True,
    ) -> None:
        """记录交易执行结果，更新风险状态."""
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

        actual_cost = sum(
            (t.economic_cost if t.economic_cost is not None else t.price)
            * _resolved_exposure_size(t)
            for t in actual_exposure_trades
        )
        pending_cost = sum(
            (t.economic_cost if t.economic_cost is not None else t.price) * t.size
            for t in pending_trades
        )
        self._state.total_exposure += actual_cost + pending_cost

        for t in actual_exposure_trades:
            cid = t.condition_id
            leg_cost = t.economic_cost if t.economic_cost is not None else t.price
            exposure_size = _resolved_exposure_size(t)
            exposure = leg_cost * exposure_size
            self._market_exposure[cid] = self._market_exposure.get(cid, 0.0) + exposure

        for t in pending_trades:
            cid = t.condition_id
            leg_cost = t.economic_cost if t.economic_cost is not None else t.price
            exposure = leg_cost * t.size
            self._market_exposure[cid] = self._market_exposure.get(cid, 0.0) + exposure
            reservation_key = t.order_id or t.trade_id
            self._pending_reservations[reservation_key] = (
                cid,
                exposure,
                time.time(),
                not bool(getattr(t, "post_only", False)),
            )

        self._state.open_positions = self._compute_open_positions()

        if event_should_cooldown:
            self._recent_arb_markets[opp.event_id] = time.time()

        if realized_pnl is not None:
            self._state.daily_pnl += float(realized_pnl)

        LOG.info(
            "风控状态: 持仓=%d, 总敞口=$%.2f, 日盈亏=$%.2f, 连续失败=%d",
            self._state.open_positions,
            self._state.total_exposure,
            self._state.daily_pnl,
            self._state.consecutive_failures,
        )

    def record_settlement(self, condition_id: str, pnl: float) -> None:
        """记录市场结算后的盈亏."""
        self._state.daily_pnl += pnl
        exposure = self._market_exposure.pop(condition_id, 0.0)
        self._state.total_exposure = max(0, self._state.total_exposure - exposure)
        self._pending_reservations = {
            key: value
            for key, value in self._pending_reservations.items()
            if value[0] != condition_id
        }
        self._state.open_positions = self._compute_open_positions()

    def reconcile_pending_order_statuses(self, trades: list[TradeRecord]) -> None:
        """根据订单状态同步结果修正 pending 预留敞口.

        目前主要用于 live maker/GTC 订单:
        - `PENDING` / `PARTIAL`: 续租预留，避免 TTL 误释放仍在交易所挂着的订单
        - `FILLED`: 释放预留并按最终成交数量落地真实敞口
        - `FAILED` / `CANCELLED`: 释放预留
        """
        now = time.time()
        updated = False

        for trade in trades:
            reservation_key = trade.order_id or trade.trade_id
            if not reservation_key:
                continue

            reservation = self._pending_reservations.get(reservation_key)
            if reservation is None:
                continue

            condition_id, reserved_exposure, _, consumes_slot = reservation
            if trade.status in (TradeStatus.PENDING, TradeStatus.PARTIAL):
                self._pending_reservations[reservation_key] = (
                    condition_id,
                    reserved_exposure,
                    now,
                    consumes_slot or trade.status == TradeStatus.PARTIAL,
                )
                continue

            self._pending_reservations.pop(reservation_key, None)
            current = self._market_exposure.get(condition_id, 0.0)
            leg_cost = trade.economic_cost if trade.economic_cost is not None else trade.price

            terminal_fill_exposure = leg_cost * _resolved_exposure_size(trade)
            if trade.status == TradeStatus.FILLED:
                final_exposure = terminal_fill_exposure
                delta = final_exposure - reserved_exposure
                action = "成交落地"
            elif terminal_fill_exposure > 0:
                # A resting order can partially fill and then be cancelled.
                # Release only the unfilled reservation; keep filled inventory
                # booked as live exposure.
                final_exposure = leg_cost * _resolved_exposure_size(trade)
                delta = final_exposure - reserved_exposure
                action = "部分成交后释放剩余挂单"
            else:
                delta = -reserved_exposure
                action = "释放挂单"

            remaining = max(0.0, current + delta)
            if remaining > 0:
                self._market_exposure[condition_id] = remaining
            else:
                self._market_exposure.pop(condition_id, None)
            self._state.total_exposure = max(0.0, self._state.total_exposure + delta)
            updated = True
            LOG.info(
                "同步订单后修正敞口: action=%s key=%s market=%s delta=$%.4f status=%s",
                action,
                reservation_key[:16],
                condition_id[:12],
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
        self._reconcile_pending_reservations()

        actual_market_exposure: dict[str, float] = {}
        normalized_positions: list[PositionSnapshot] = []
        for position in positions:
            size = max(0.0, float(position.size))
            if size <= 0:
                continue
            avg_price = max(0.0, float(position.avg_price))
            normalized = PositionSnapshot(
                token_id=position.token_id,
                condition_id=position.condition_id,
                outcome=position.outcome,
                size=size,
                avg_price=avg_price,
                current_value=max(0.0, float(position.current_value)),
                unrealized_pnl=float(position.unrealized_pnl),
            )
            normalized_positions.append(normalized)
            actual_market_exposure[normalized.condition_id] = (
                actual_market_exposure.get(normalized.condition_id, 0.0) + (normalized.avg_price * normalized.size)
            )

        self._state.positions = normalized_positions
        self._state.daily_pnl = float(realized_daily_pnl)
        self._state.last_portfolio_sync_ts = float(synced_at)
        self._state.portfolio_sync_ok = True
        self._state.portfolio_sync_error = ""
        if self._state.portfolio_sync_consecutive_failures > 0:
            LOG.info(
                "账户同步恢复，重置连续失败计数 %d -> 0",
                self._state.portfolio_sync_consecutive_failures,
            )
        self._state.portfolio_sync_consecutive_failures = 0

        merged_exposure = dict(actual_market_exposure)
        for condition_id, exposure, _, _ in self._pending_reservations.values():
            merged_exposure[condition_id] = merged_exposure.get(condition_id, 0.0) + exposure
        self._market_exposure = merged_exposure
        self._state.total_exposure = sum(merged_exposure.values())
        self._state.open_positions = self._compute_open_positions()

    def mark_portfolio_sync_error(self, message: str, *, synced_at: float | None = None) -> None:
        self._state.portfolio_sync_ok = False
        self._state.portfolio_sync_error = message
        self._state.portfolio_sync_consecutive_failures += 1
        if synced_at is not None:
            self._state.last_portfolio_sync_ts = float(synced_at)
        cap = self._config.portfolio_sync_max_consecutive_failures
        if cap > 0 and self._state.portfolio_sync_consecutive_failures >= cap:
            LOG.warning(
                "账户同步连续失败 %d 次（阈值 %d）：实盘已暂停开仓直到下次同步成功",
                self._state.portfolio_sync_consecutive_failures,
                cap,
            )

    def apply_ai_adjustment(self, adjustments: dict) -> None:
        """应用 AI 建议的风控参数调整（受硬上限约束）.

        调整因子范围 [0.5, 1.5]，乘以 .env 中的原始值。
        AI 永远无法将参数提高到原始配置值的 150% 以上。

        Args:
            adjustments: {"max_exposure_factor": float, "daily_loss_factor": float}
        """
        if not adjustments:
            return

        base_exposure = self._config.max_total_exposure
        base_daily_loss = self._config.max_daily_loss

        if "max_exposure_factor" in adjustments:
            factor = max(0.5, min(1.5, float(adjustments["max_exposure_factor"])))
            new_val = base_exposure * factor
            LOG.info("AI 风控调整: max_total_exposure %.2f -> %.2f (factor=%.2f)", base_exposure, new_val, factor)
            self._effective_max_total_exposure = new_val

        if "daily_loss_factor" in adjustments:
            factor = max(0.5, min(1.5, float(adjustments["daily_loss_factor"])))
            new_val = base_daily_loss * factor
            LOG.info("AI 风控调整: max_daily_loss %.2f -> %.2f (factor=%.2f)", base_daily_loss, new_val, factor)
            self._effective_max_daily_loss = new_val

    def reset_halt(self) -> None:
        """手动解除熔断."""
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
            self._daily_reset_ts = today

    def _reconcile_pending_reservations(self) -> None:
        now = time.time()
        expired_keys = [
            key for key, (_, _, created_ts, _) in self._pending_reservations.items()
            if now - created_ts >= self._pending_reservation_ttl_sec
        ]
        for key in expired_keys:
            condition_id, exposure, _, _ = self._pending_reservations.pop(key)
            current = self._market_exposure.get(condition_id, 0.0)
            remaining = max(0.0, current - exposure)
            if remaining > 0:
                self._market_exposure[condition_id] = remaining
            else:
                self._market_exposure.pop(condition_id, None)
            self._state.total_exposure = max(0.0, self._state.total_exposure - exposure)
            LOG.info("释放过期预留敞口: key=%s, market=%s, exposure=$%.4f", key[:16], condition_id[:12], exposure)

        self._state.open_positions = self._compute_open_positions()

    def _compute_open_positions(self) -> int:
        slotless_pending_by_market: dict[str, float] = {}
        for condition_id, exposure, _, consumes_slot in self._pending_reservations.values():
            if consumes_slot:
                continue
            slotless_pending_by_market[condition_id] = slotless_pending_by_market.get(condition_id, 0.0) + exposure

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
            f"日盈亏: ${s.daily_pnl:+.2f} (止损线: -${self._config.max_daily_loss:.2f})",
            f"连续失败: {s.consecutive_failures}/{self._config.max_consecutive_failures}",
            f"状态: {'🔴 已暂停 - ' + s.halt_reason if s.is_halted else '🟢 正常'}",
        ]
        return "\n".join(lines)


def _start_of_day() -> float:
    """当天 00:00 UTC 的时间戳."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def _resolved_execution_size(trade: TradeRecord) -> float:
    if trade.fill_size is not None:
        return float(trade.fill_size)
    if trade.status == TradeStatus.FILLED:
        return float(trade.size)
    return 0.0


def _resolved_exposure_size(trade: TradeRecord) -> float:
    return _resolved_execution_size(trade)
