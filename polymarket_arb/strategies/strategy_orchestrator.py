"""策略编排器：按优先级协调多个策略的执行.

策略优先级（从高到低）:

Tier 0: 结构性套利 (确定性利润, 最高优先)
  - 二元套利: Yes + No < 1
  - 多结果套利: Σ ask_i < 1
  → 触发条件: WebSocket 推送 → 立即检测 → 立即执行
  → 仓位: Kelly (high win_prob ≈ 0.95)
  → 费率: Taker 2%（不可避免，需要速度）

Tier 1: 跨平台套利 (近确定性利润)
  - Polymarket vs Kalshi 价差
  → 触发条件: 定时扫描（两平台不共享 WS）
  → 仓位: Kelly (win_prob ≈ 0.85, 考虑结算差异风险)
  → 额外风险: 跨平台资金锁定、结算规则差异

Tier 2: 统计套利 (模型驱动)
  - 贝叶斯模型 vs 市场价格的偏差
  - 订单簿不平衡 + 动量 + 跨市场信号
  → 触发条件: 偏差超过阈值
  → 仓位: Kelly (win_prob = model_confidence, 通常 0.55-0.70)
  → 半 Kelly 或四分之一 Kelly（模型不确定性高）

Tier 3: 做市 (持续被动收入)
  - 在模型 fair value 两侧挂 maker 单
  - 赚 spread + 流动性激励
  → 触发条件: 持续运行
  → 仓位: 固定（不用 Kelly，因为不是离散赌注）
  → 费率: 0%（maker）

编排规则:
1. 高优先级策略先执行，用掉的资金从低优先级的可用资金中扣除
2. Tier 0/1 是事件驱动的（机会出现才执行）
3. Tier 2/3 是持续运行的
4. 风控贯穿所有层级

资金分配:
  可用资金的分配比例（可配置）:
  - 结构性套利: 30% (机会稀少但确定性高)
  - 跨平台: 20%
  - 统计套利: 30%
  - 做市: 20%
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

LOG = logging.getLogger(__name__)


class StrategyTier(IntEnum):
    STRUCTURAL_ARB = 0
    CROSS_PLATFORM = 1
    STATISTICAL_ARB = 2
    MARKET_MAKING = 3


@dataclass
class StrategyAllocation:
    """策略资金分配."""

    tier: StrategyTier
    allocation_pct: float  # 0-1
    current_exposure: float = 0.0
    realized_pnl: float = 0.0
    trade_count: int = 0
    last_trade_ts: float = 0.0


@dataclass
class StrategySignal:
    """策略信号：由各策略产生，交给编排器决定执行."""

    tier: StrategyTier
    signal_type: str
    market_id: str
    description: str
    expected_edge: float
    confidence: float
    recommended_size_usdc: float
    urgency: float = 1.0  # 0-1, 结构性套利=1.0（立即执行）, 做市=0.3
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class StrategyOrchestrator:
    """策略编排器."""

    DEFAULT_ALLOCATIONS = {
        StrategyTier.STRUCTURAL_ARB: 0.30,
        StrategyTier.CROSS_PLATFORM: 0.20,
        StrategyTier.STATISTICAL_ARB: 0.30,
        StrategyTier.MARKET_MAKING: 0.20,
    }

    def __init__(
        self,
        total_bankroll: float,
        allocations: Optional[dict[StrategyTier, float]] = None,
    ):
        self._bankroll = total_bankroll
        alloc_map = allocations or self.DEFAULT_ALLOCATIONS

        self._allocations: dict[StrategyTier, StrategyAllocation] = {}
        for tier, pct in alloc_map.items():
            self._allocations[tier] = StrategyAllocation(tier=tier, allocation_pct=pct)

        self._pending_signals: list[StrategySignal] = []
        self._executed_signals: list[StrategySignal] = []

    def submit_signal(self, signal: StrategySignal) -> None:
        self._pending_signals.append(signal)

    def process_signals(self) -> list[StrategySignal]:
        """处理所有待处理信号，返回应执行的信号（按优先级排序）.

        执行顺序:
        1. 按 tier 升序（0=最高优先）
        2. 同 tier 内按 urgency × expected_edge 降序
        3. 每个信号检查资金是否足够
        """
        if not self._pending_signals:
            return []

        self._pending_signals.sort(
            key=lambda s: (s.tier, -(s.urgency * s.expected_edge))
        )

        to_execute: list[StrategySignal] = []
        remaining_by_tier = self._available_by_tier()

        for signal in self._pending_signals:
            tier = signal.tier
            available = remaining_by_tier.get(tier, 0.0)

            if signal.recommended_size_usdc <= 0:
                continue
            if signal.recommended_size_usdc > available:
                adjusted = available
                if adjusted < 1.0:
                    continue
                signal.recommended_size_usdc = adjusted

            to_execute.append(signal)
            remaining_by_tier[tier] = available - signal.recommended_size_usdc

        self._pending_signals.clear()
        return to_execute

    def record_execution(self, signal: StrategySignal, success: bool, pnl: float = 0.0) -> None:
        """记录信号执行结果."""
        alloc = self._allocations.get(signal.tier)
        if alloc is None:
            return

        if success:
            alloc.current_exposure += signal.recommended_size_usdc
            alloc.trade_count += 1
            alloc.last_trade_ts = time.time()
        alloc.realized_pnl += pnl
        self._executed_signals.append(signal)

    def record_settlement(self, tier: StrategyTier, amount: float, pnl: float) -> None:
        """仓位结算后释放敞口."""
        alloc = self._allocations.get(tier)
        if alloc:
            alloc.current_exposure = max(0, alloc.current_exposure - amount)
            alloc.realized_pnl += pnl

    def update_bankroll(self, new_bankroll: float) -> None:
        self._bankroll = new_bankroll

    def _available_by_tier(self) -> dict[StrategyTier, float]:
        result = {}
        for tier, alloc in self._allocations.items():
            budget = self._bankroll * alloc.allocation_pct
            available = max(0, budget - alloc.current_exposure)
            result[tier] = available
        return result

    def format_status_zh(self) -> str:
        lines = ["📋 策略编排状态", f"总资金: ${self._bankroll:.2f}", ""]
        tier_names = {
            StrategyTier.STRUCTURAL_ARB: "结构性套利",
            StrategyTier.CROSS_PLATFORM: "跨平台套利",
            StrategyTier.STATISTICAL_ARB: "统计套利",
            StrategyTier.MARKET_MAKING: "做市策略",
        }
        for tier, alloc in sorted(self._allocations.items(), key=lambda x: x[0]):
            name = tier_names.get(tier, str(tier))
            budget = self._bankroll * alloc.allocation_pct
            available = max(0, budget - alloc.current_exposure)
            lines.append(
                f"T{int(tier)} {name}: "
                f"预算 ${budget:.0f} ({alloc.allocation_pct:.0%}) | "
                f"已用 ${alloc.current_exposure:.0f} | "
                f"可用 ${available:.0f} | "
                f"PnL ${alloc.realized_pnl:+.2f} | "
                f"交易 {alloc.trade_count}笔"
            )
        return "\n".join(lines)
