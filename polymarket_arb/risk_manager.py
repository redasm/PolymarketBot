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
from typing import Optional

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
        self._daily_reset_ts: float = _start_of_day()
        self._effective_max_total_exposure = config.max_total_exposure
        self._effective_max_daily_loss = config.max_daily_loss

    @property
    def state(self) -> RiskState:
        return self._state

    def pre_trade_check(self, opp: ArbOpportunity, proposed_size: float) -> tuple[bool, str, float]:
        """交易前风控检查.

        Returns:
            (允许交易, 原因, 调整后的数量)
        """
        self._maybe_reset_daily()

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
            if time.time() - last_ts < 60:
                return False, f"事件 {event_id} 60秒内已执行过套利", 0.0

        for market in opp.markets:
            cid = market.condition_id
            current_exposure = self._market_exposure.get(cid, 0.0)
            if current_exposure >= self._config.max_exposure_per_market:
                return False, f"市场 {cid[:12]} 敞口已达上限", 0.0

        remaining_total = self._effective_max_total_exposure - self._state.total_exposure
        if remaining_total <= 0:
            return False, "全局敞口已满", 0.0

        per_market_remaining = self._config.max_exposure_per_market
        for market in opp.markets:
            cid = market.condition_id
            used = self._market_exposure.get(cid, 0.0)
            per_market_remaining = min(per_market_remaining, self._config.max_exposure_per_market - used)

        max_affordable_size = min(
            proposed_size,
            remaining_total / opp.total_cost if opp.total_cost > 0 else 0,
            per_market_remaining / opp.total_cost if opp.total_cost > 0 else 0,
            opp.max_executable_size,
        )

        if max_affordable_size <= 0:
            return False, "风控后可执行数量为 0", 0.0

        return True, "", max_affordable_size

    def record_execution(self, opp: ArbOpportunity, trades: list[TradeRecord]) -> None:
        """记录交易执行结果，更新风险状态."""
        filled_trades = [t for t in trades if t.status == TradeStatus.FILLED]
        failed_trades = [t for t in trades if t.status == TradeStatus.FAILED]

        if failed_trades:
            self._state.consecutive_failures += 1
            if self._state.consecutive_failures >= self._config.max_consecutive_failures > 0:
                self._state.is_halted = True
                self._state.halt_reason = f"连续失败 {self._state.consecutive_failures} 次"
                LOG.error("风控熔断: %s", self._state.halt_reason)
        elif filled_trades:
            self._state.consecutive_failures = 0

        total_cost = sum(t.price * t.size for t in filled_trades)
        self._state.total_exposure += total_cost

        for t in filled_trades:
            cid = t.condition_id
            self._market_exposure[cid] = self._market_exposure.get(cid, 0.0) + t.price * t.size

        if filled_trades:
            self._state.open_positions += 1
            self._recent_arb_markets[opp.event_id] = time.time()
            expected_profit = opp.net_edge * filled_trades[0].size if filled_trades else 0
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
        self._state.open_positions = max(0, self._state.open_positions - 1)

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

    def _maybe_reset_daily(self) -> None:
        """检查是否需要重置日盈亏."""
        today = _start_of_day()
        if today > self._daily_reset_ts:
            LOG.info("日切: 重置日盈亏 $%.2f -> $0.00", self._state.daily_pnl)
            self._state.daily_pnl = 0.0
            self._daily_reset_ts = today

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
