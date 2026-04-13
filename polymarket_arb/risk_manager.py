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
        self._pending_reservations: dict[str, tuple[str, float, float]] = {}
        self._daily_reset_ts: float = _start_of_day()
        self._effective_max_total_exposure = config.max_total_exposure
        self._effective_max_daily_loss = config.max_daily_loss
        self._pending_reservation_ttl_sec = config.risk_pending_reservation_ttl_sec

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

        can, reason = self._state.check_can_trade(
            max_positions=self._config.max_open_positions,
            max_total_exposure=self._effective_max_total_exposure,
            max_daily_loss=self._effective_max_daily_loss,
            max_failures=self._config.max_consecutive_failures,
        )
        if not can:
            return False, reason, 0.0

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

    def record_execution(self, opp: ArbOpportunity, trades: list[TradeRecord]) -> None:
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

        if execution_success:
            self._state.consecutive_failures = 0
        elif trades:
            self._state.consecutive_failures += 1
            if self._state.consecutive_failures >= self._config.max_consecutive_failures > 0:
                self._state.is_halted = True
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
            )

        self._state.open_positions = sum(1 for exposure in self._market_exposure.values() if exposure > 0)

        if event_should_cooldown:
            self._recent_arb_markets[opp.event_id] = time.time()

        if execution_success and filled_trades:
            expected_profit = opp.net_edge * min(_resolved_execution_size(t) for t in filled_trades)
            self._state.daily_pnl += expected_profit

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
        self._state.open_positions = sum(1 for exp in self._market_exposure.values() if exp > 0)

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
            key for key, (_, _, created_ts) in self._pending_reservations.items()
            if now - created_ts >= self._pending_reservation_ttl_sec
        ]
        for key in expired_keys:
            condition_id, exposure, _ = self._pending_reservations.pop(key)
            current = self._market_exposure.get(condition_id, 0.0)
            remaining = max(0.0, current - exposure)
            if remaining > 0:
                self._market_exposure[condition_id] = remaining
            else:
                self._market_exposure.pop(condition_id, None)
            self._state.total_exposure = max(0.0, self._state.total_exposure - exposure)
            LOG.info("释放过期预留敞口: key=%s, market=%s, exposure=$%.4f", key[:16], condition_id[:12], exposure)

        self._state.open_positions = sum(1 for exp in self._market_exposure.values() if exp > 0)

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
