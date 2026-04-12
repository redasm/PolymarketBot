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
import math
import time
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any, Optional

from research_signal.normalizers.topic import topic_overlap_score

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
        self._research_overlay_stats = {
            "applied": 0,
            "boosted": 0,
            "penalized": 0,
            "vetoed": 0,
        }
        self._overlay_history: list[dict[str, Any]] = []

    def submit_signal(
        self,
        signal: StrategySignal,
        *,
        active_markets: list[Any] | None = None,
        research_report: dict | Any | None = None,
        research_signals: list[Any] | None = None,
    ) -> bool:
        signal_copy = replace(signal, payload=dict(signal.payload))
        overlay = self._apply_research_overlay(
            signal_copy,
            active_markets=active_markets or [],
            research_report=research_report,
            research_signals=research_signals or [],
        )
        signal_copy.payload["research_overlay"] = overlay
        self._record_overlay(overlay)
        if overlay.get("veto"):
            LOG.info(
                "策略信号被 research veto: tier=%s market=%s reasons=%s",
                signal_copy.tier,
                signal_copy.market_id,
                overlay.get("reasons", []),
            )
            return False
        self._pending_signals.append(signal_copy)
        return True

    def process_signals(self) -> list[StrategySignal]:
        """处理所有待处理信号，返回应执行的信号（按优先级排序）.

        执行顺序:
        1. 按 tier 升序（0=最高优先）
        2. 同 tier 内按 urgency × expected_edge 降序
        3. 每个信号检查资金是否足够
        """
        if not self._pending_signals:
            return []

        edge_scales = self._edge_scales_by_tier(self._pending_signals)
        self._pending_signals.sort(
            key=lambda s: (s.tier, -self._priority_score(s, edge_scales.get(s.tier, 1.0)))
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

    def record_execution(
        self,
        signal: StrategySignal,
        success: bool,
        pnl: float = 0.0,
        *,
        exposure_amount_usdc: float | None = None,
    ) -> None:
        """记录信号执行结果."""
        alloc = self._allocations.get(signal.tier)
        if alloc is None:
            return

        exposure_to_book = max(
            0.0,
            exposure_amount_usdc
            if exposure_amount_usdc is not None
            else (signal.recommended_size_usdc if success else 0.0),
        )
        if exposure_to_book > 0:
            alloc.current_exposure += exposure_to_book
        if success or exposure_to_book > 0:
            alloc.trade_count += 1
            alloc.last_trade_ts = time.time()
        alloc.realized_pnl += pnl
        self._executed_signals.append(signal)

    def record_processed(self, signal: StrategySignal) -> None:
        """记录信号已被编排器消费，但未进入真实执行."""
        self._executed_signals.append(signal)

    def record_settlement(self, tier: StrategyTier, amount: float, pnl: float) -> None:
        """仓位结算后释放敞口."""
        alloc = self._allocations.get(tier)
        if alloc:
            alloc.current_exposure = max(0, alloc.current_exposure - amount)
            alloc.realized_pnl += pnl

    def update_bankroll(self, new_bankroll: float) -> None:
        self._bankroll = new_bankroll

    def get_status(self) -> dict[str, Any]:
        tier_keys = {
            StrategyTier.STRUCTURAL_ARB: "T0",
            StrategyTier.CROSS_PLATFORM: "T1",
            StrategyTier.STATISTICAL_ARB: "T2",
            StrategyTier.MARKET_MAKING: "T3",
        }
        status: dict[str, Any] = {}
        for tier, alloc in sorted(self._allocations.items(), key=lambda item: item[0]):
            budget = self._bankroll * alloc.allocation_pct
            status[tier_keys[tier]] = {
                "allocation_pct": alloc.allocation_pct,
                "budget": budget,
                "current_exposure": alloc.current_exposure,
                "available": max(0.0, budget - alloc.current_exposure),
                "realized_pnl": alloc.realized_pnl,
                "trade_count": alloc.trade_count,
            }
        status["meta"] = {
            "pending_signals": len(self._pending_signals),
            "executed_signals": len(self._executed_signals),
            "research_overlay": dict(self._research_overlay_stats),
            "recent_overlays": list(self._overlay_history[-10:]),
        }
        return status

    def _available_by_tier(self) -> dict[StrategyTier, float]:
        result = {}
        for tier, alloc in self._allocations.items():
            budget = self._bankroll * alloc.allocation_pct
            available = max(0, budget - alloc.current_exposure)
            result[tier] = available
        return result

    def _edge_scales_by_tier(self, signals: list[StrategySignal]) -> dict[StrategyTier, float]:
        scales: dict[StrategyTier, float] = {}
        grouped: dict[StrategyTier, list[float]] = {}
        for signal in signals:
            grouped.setdefault(signal.tier, []).append(abs(float(signal.expected_edge)))

        for tier, values in grouped.items():
            nonzero = sorted(value for value in values if value > 0)
            if not nonzero:
                scales[tier] = 1.0
                continue
            idx = max(0, min(len(nonzero) - 1, math.ceil(len(nonzero) * 0.75) - 1))
            scales[tier] = max(nonzero[idx], nonzero[-1] * 0.5, 1.0)
        return scales

    def _priority_score(self, signal: StrategySignal, edge_scale: float) -> float:
        normalized_edge = min(1.5, abs(float(signal.expected_edge)) / max(edge_scale, 1e-9))
        confidence = max(0.0, min(1.0, float(signal.confidence)))
        urgency = max(0.0, min(1.0, float(signal.urgency)))
        return (urgency * 0.5) + (confidence * 0.35) + (normalized_edge * 0.15)

    def _apply_research_overlay(
        self,
        signal: StrategySignal,
        *,
        active_markets: list[Any],
        research_report: dict | Any | None,
        research_signals: list[Any],
    ) -> dict[str, Any]:
        action = self._resolve_signal_action(signal)
        matched_rows = self._match_research_rows(signal, active_markets, research_signals)
        if action not in {"BUY_YES", "BUY_NO"} or not matched_rows:
            return {
                "applied": False,
                "veto": False,
                "action": action,
                "matched_count": len(matched_rows),
                "size_multiplier": 1.0,
                "confidence_delta": 0.0,
                "reasons": [],
            }

        confidence_values = [float(row.get("confidence", 0.0)) for row in matched_rows]
        freshness_values = [float(row.get("freshness_sec", 0.0)) for row in matched_rows]
        avg_conf = sum(confidence_values) / max(1, len(confidence_values))
        avg_freshness = sum(freshness_values) / max(1, len(freshness_values))
        stance_counts: dict[str, int] = {}
        for row in matched_rows:
            stance = row.get("stance", "uncertain")
            stance_counts[stance] = stance_counts.get(stance, 0) + 1
        dominant_stance = self._dominant_stance(stance_counts)
        mixed = stance_counts.get("bullish", 0) > 0 and stance_counts.get("bearish", 0) > 0
        aligned = (
            (action == "BUY_YES" and dominant_stance == "bullish")
            or (action == "BUY_NO" and dominant_stance == "bearish")
        )
        conflicting = (
            (action == "BUY_YES" and dominant_stance == "bearish")
            or (action == "BUY_NO" and dominant_stance == "bullish")
        )

        reasons: list[str] = []
        size_multiplier = 1.0
        confidence_delta = 0.0
        veto = False

        if aligned:
            size_multiplier *= 1.10 if avg_conf >= 0.60 else 1.05
            confidence_delta += 0.08 if avg_conf >= 0.60 else 0.04
            reasons.append("research_aligned")
        elif conflicting:
            size_multiplier *= 0.50 if avg_conf >= 0.75 else 0.75
            confidence_delta -= 0.18 if avg_conf >= 0.75 else 0.10
            reasons.append("research_conflict")
            if avg_conf >= 0.85 and len(matched_rows) >= 3 and not mixed:
                veto = True
                reasons.append("strong_conflicting_research")
            elif avg_conf >= 0.80 and len(matched_rows) >= 2:
                reasons.append("high_conviction_conflict")

        if mixed:
            size_multiplier *= 0.90
            confidence_delta -= 0.05
            reasons.append("mixed_research_stance")

        if avg_freshness > 6 * 3600:
            size_multiplier *= 0.85
            confidence_delta -= 0.03
            reasons.append("stale_research")

        if len(matched_rows) == 1 and avg_conf < 0.55:
            size_multiplier *= 0.90
            reasons.append("weak_research_coverage")

        size_multiplier = max(0.25, min(1.25, size_multiplier))
        signal.confidence = max(0.0, min(1.0, signal.confidence + confidence_delta))
        signal.recommended_size_usdc = max(0.0, signal.recommended_size_usdc * size_multiplier)

        report_row = research_report.to_dict() if hasattr(research_report, "to_dict") else dict(research_report or {})
        source_counts = dict(report_row.get("source_counts", {}))
        return {
            "applied": True,
            "veto": veto,
            "action": action,
            "matched_count": len(matched_rows),
            "dominant_stance": dominant_stance,
            "avg_confidence": round(avg_conf, 3),
            "avg_freshness_sec": round(avg_freshness, 1),
            "size_multiplier": round(size_multiplier, 3),
            "confidence_delta": round(confidence_delta, 3),
            "reasons": reasons,
            "source_counts": source_counts,
        }

    def _match_research_rows(
        self,
        signal: StrategySignal,
        active_markets: list[Any],
        research_signals: list[Any],
    ) -> list[dict[str, Any]]:
        market = self._find_market(signal.market_id, active_markets)
        if market is not None:
            market_rows = [
                self._to_signal_row(row)
                for row in getattr(market, "raw", {}).get("research_signals", [])
            ]
            market_rows = [row for row in market_rows if row]
            if market_rows:
                return market_rows

        normalized_rows = [self._to_signal_row(row) for row in research_signals]
        normalized_rows = [row for row in normalized_rows if row]
        if market is None:
            return []

        matched: list[dict[str, Any]] = []
        for row in normalized_rows:
            if self._row_matches_market(row, market):
                matched.append(row)
        return matched[:3]

    def _find_market(self, signal_market_id: str, active_markets: list[Any]) -> Any | None:
        for market in active_markets:
            if self._market_matches_signal_id(signal_market_id, getattr(market, "condition_id", "")):
                return market
            if signal_market_id and signal_market_id == getattr(market, "event_id", ""):
                return market
        return None

    def _market_matches_signal_id(self, signal_market_id: str, condition_id: str) -> bool:
        if not signal_market_id or not condition_id:
            return False
        if condition_id == signal_market_id:
            return True
        return (
            len(signal_market_id) >= 12
            and len(condition_id) >= 12
            and (condition_id.startswith(signal_market_id) or signal_market_id.startswith(condition_id))
        )

    def _resolve_signal_action(self, signal: StrategySignal) -> str:
        if "action" in signal.payload:
            return str(signal.payload.get("action"))
        signal_type = signal.signal_type.upper()
        if "BUY_YES" in signal_type:
            return "BUY_YES"
        if "BUY_NO" in signal_type:
            return "BUY_NO"
        if "SELL_YES" in signal_type:
            return "SELL_YES"
        if "SELL_NO" in signal_type:
            return "SELL_NO"
        return ""

    def _to_signal_row(self, row: Any) -> dict[str, Any]:
        if hasattr(row, "to_dict"):
            return row.to_dict()
        if isinstance(row, dict):
            return dict(row)
        return {}

    def _row_matches_market(self, row: dict[str, Any], market: Any) -> bool:
        event_candidates = row.get("event_candidates", [])
        if getattr(market, "event_id", "") and getattr(market, "event_id", "") in event_candidates:
            return True

        metadata = row.get("metadata", {}) if isinstance(row.get("metadata", {}), dict) else {}
        row_condition_ids = metadata.get("condition_ids", [])
        if isinstance(row_condition_ids, str):
            row_condition_ids = [row_condition_ids]
        market_condition_id = getattr(market, "condition_id", "")
        if any(self._market_matches_signal_id(str(condition_id), market_condition_id) for condition_id in row_condition_ids):
            return True

        canonical_topic = str(metadata.get("canonical_topic", ""))
        summary = str(row.get("summary", ""))
        return max(
            topic_overlap_score(getattr(market, "question", ""), canonical_topic),
            topic_overlap_score(getattr(market, "question", ""), summary),
        ) >= 0.5

    def _record_overlay(self, overlay: dict[str, Any]) -> None:
        if overlay.get("applied"):
            self._research_overlay_stats["applied"] += 1
        delta = float(overlay.get("confidence_delta", 0.0))
        if delta > 0:
            self._research_overlay_stats["boosted"] += 1
        elif delta < 0:
            self._research_overlay_stats["penalized"] += 1
        if overlay.get("veto"):
            self._research_overlay_stats["vetoed"] += 1
        self._overlay_history.append({
            "timestamp": time.time(),
            **overlay,
        })
        if len(self._overlay_history) > 50:
            self._overlay_history = self._overlay_history[-50:]

    def _dominant_stance(self, stance_counts: dict[str, int]) -> str:
        if not stance_counts:
            return "uncertain"
        ranked = sorted(stance_counts.items(), key=lambda item: (-item[1], item[0]))
        if len(ranked) >= 2 and ranked[0][1] == ranked[1][1]:
            return "uncertain"
        return ranked[0][0]

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
        overlay = self._research_overlay_stats
        lines.extend([
            "",
            "Research Overlay:",
            f"已应用 {overlay['applied']} | 加分 {overlay['boosted']} | "
            f"减分 {overlay['penalized']} | veto {overlay['vetoed']}",
        ])
        return "\n".join(lines)
