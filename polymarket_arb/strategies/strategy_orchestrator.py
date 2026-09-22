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
import uuid
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any, Optional

from polymarket_arb.strategies.signal_policies import (
    BarbellPolicy,
    NearCertaintyClassifier,
    NearCertaintyResult,
    TailRiskClassifier,
)
from research_signal.normalizers.topic import topic_overlap_score

LOG = logging.getLogger(__name__)


def _new_signal_id() -> str:
    return f"sig-{uuid.uuid4().hex[:12]}"


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
    signal_id: str = ""


class StrategyOrchestrator:
    """策略编排器."""

    # Weights shifted toward T3 (maker) after Becker 2025 demonstrated that
    # Polymarket takers average -1.12% EV vs makers' +1.12% across 72M
    # trades. T0 still gets the biggest slice because structural arb is
    # priced-locked profit, not a directional bet; T2 (statistical taker)
    # is halved because it's the role the paper most clearly identifies
    # as a negative-EV game in efficient categories.
    DEFAULT_ALLOCATIONS = {
        StrategyTier.STRUCTURAL_ARB: 0.35,
        StrategyTier.CROSS_PLATFORM: 0.10,
        StrategyTier.STATISTICAL_ARB: 0.35,
        StrategyTier.MARKET_MAKING: 0.20,
    }
    _MAX_SIGNAL_HISTORY = 2000
    # Per-market-per-hour cap applies only to directional tiers; T0 / T3 are
    # exempt because their cadence is bounded by other mechanisms (event-driven
    # arbs, maker rest-order TTL).
    _RATE_CAPPED_TIERS: frozenset[StrategyTier] = frozenset(
        {StrategyTier.STATISTICAL_ARB, StrategyTier.CROSS_PLATFORM}
    )

    def __init__(
        self,
        total_bankroll: float,
        allocations: Optional[dict[StrategyTier, float]] = None,
        *,
        max_signals_per_market_per_hour: int = 2,
        tail_risk_classifier: Optional[TailRiskClassifier] = None,
        near_certainty_classifier: Optional[NearCertaintyClassifier] = None,
        barbell_policy: Optional[BarbellPolicy] = None,
        sniper_gate: Any | None = None,
        max_signal_size_multiplier: float = 1.35,
    ):
        self._bankroll = total_bankroll
        self._peak_equity = max(0.0, float(total_bankroll))
        alloc_map = allocations or self.DEFAULT_ALLOCATIONS

        self._allocations: dict[StrategyTier, StrategyAllocation] = {}
        for tier, pct in alloc_map.items():
            self._allocations[tier] = StrategyAllocation(tier=tier, allocation_pct=pct)
        self._tail_risk_classifier = tail_risk_classifier or TailRiskClassifier()
        # Near-certainty rule defaults to shadow mode. Operators wire a
        # non-shadow instance from config once empirical validation is
        # complete (see scripts/verify_near_certainty_trap.py).
        self._near_certainty_classifier = (
            near_certainty_classifier or NearCertaintyClassifier()
        )
        # Barbell pool: see signal_policies.BarbellPolicy. Disabled by
        # default — operator wires a real instance from config once
        # the data_driven_exposure / tail_exposure split surfaced under
        # `meta.barbell` looks healthy.
        self._barbell_policy = barbell_policy or BarbellPolicy(
            enabled=False, tail_budget_usdc=0.0
        )
        # Per-class T2 exposure tracker for the barbell. The keys are
        # the bucket names returned by BarbellPolicy.classify_bucket
        # ("data_driven" / "tail"). Updated on submit (positive) and on
        # `record_settlement` for STATISTICAL_ARB tier (negative).
        self._t2_class_exposure_usdc: dict[str, float] = {
            "data_driven": 0.0,
            "tail": 0.0,
        }
        # Tracks (signal_id → (bucket, amount)) so settlement can debit
        # the right bucket. Bounded along with executed_signals via the
        # MAX_SIGNAL_HISTORY cap.
        self._t2_class_signal_ledger: dict[str, tuple[str, float]] = {}
        self._sniper_gate = sniper_gate

        self._pending_signals: list[StrategySignal] = []
        self._executed_signals: list[StrategySignal] = []
        self._sniper_gate_stats = {
            "applied": 0,
            "accepted": 0,
            "rejected": 0,
        }
        self._research_overlay_stats = {
            "applied": 0,
            "boosted": 0,
            "penalized": 0,
            "vetoed": 0,
        }
        self._tail_risk_stats = {
            "applied": 0,
            "penalized": 0,
            "high_risk": 0,
            "vetoed": 0,
        }
        # Near-certainty telemetry. `would_apply_*` counts shadow-mode
        # hits where the rule would have fired but did not modify the
        # signal. `applied_*` counts only fires under live (non-shadow)
        # mode. Operators use the would_apply samples to validate the
        # rule offline before flipping shadow_mode=false.
        self._near_certainty_stats = {
            "evaluated": 0,
            "would_apply_high": 0,
            "would_apply_longshot": 0,
            "applied_high": 0,
            "applied_longshot": 0,
        }
        self._overlay_history: list[dict[str, Any]] = []
        self._last_skip_reasons: dict[str, int] = {}
        self._last_skipped_by_tier: dict[str, int] = {}
        # Per-market signal-rate cap: (tier, market_id) -> [submission_ts, ...]
        # Without this the bot will repeatedly fire on the same model-vs-market
        # mispricing every scan cycle (telemetry showed 2957 signals on one
        # market over 4 days). Submissions blocked by the cap are surfaced
        # through `_last_skip_reasons["per_market_rate_cap"]`.
        self._signal_history_by_market: dict[tuple[StrategyTier, str], list[float]] = {}
        self._max_signals_per_market_per_hour = max(0, int(max_signals_per_market_per_hour))
        self._rate_cap_log_state: dict[tuple[StrategyTier, str], tuple[float, int]] = {}
        self._max_signal_size_multiplier = max(1.0, float(max_signal_size_multiplier))

    def submit_signal(
        self,
        signal: StrategySignal,
        *,
        active_markets: list[Any] | None = None,
        research_report: dict | Any | None = None,
        research_signals: list[Any] | None = None,
    ) -> bool:
        active_markets = active_markets or []
        original_size = max(0.0, float(signal.recommended_size_usdc))
        if self._sniper_gate is not None:
            market = self._find_market(signal.market_id, active_markets)
            gate_decision = self._sniper_gate.evaluate(signal, market=market)
            self._record_sniper_gate(gate_decision)
            if not gate_decision.accepted:
                self._record_process_skip(signal, "sniper_gate_reject")
                LOG.info(
                    "策略信号被 sniper gate 拦截: tier=%s market=%s reasons=%s",
                    signal.tier,
                    signal.market_id[:12] if signal.market_id else "?",
                    gate_decision.reasons,
                )
                return False
            if getattr(gate_decision, "size_multiplier", 1.0) != 1.0:
                signal = replace(
                    signal,
                    recommended_size_usdc=max(
                        0.0,
                        signal.recommended_size_usdc * float(gate_decision.size_multiplier),
                    ),
                    payload={
                        **dict(signal.payload),
                        "sniper_gate": {
                            "size_multiplier": round(float(gate_decision.size_multiplier), 3),
                            "reasons": list(gate_decision.reasons),
                        },
                    },
                )
        if not self._check_per_market_rate_cap(signal):
            self._record_process_skip(signal, "per_market_rate_cap")
            self._log_rate_cap_skip(signal)
            return False
        if not signal.signal_id:
            signal.signal_id = _new_signal_id()
        signal_copy = replace(signal, payload=dict(signal.payload))
        overlay = self._apply_research_overlay(
            signal_copy,
            active_markets=active_markets,
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
        tail_risk = self._apply_tail_risk_adjustment(
            signal_copy,
            active_markets=active_markets,
        )
        signal_copy.payload["tail_risk"] = tail_risk
        self._record_tail_risk(tail_risk)
        if tail_risk.get("veto"):
            LOG.info(
                "策略信号被 tail_risk veto: tier=%s market=%s risk_class=%s reasons=%s",
                signal_copy.tier,
                signal_copy.market_id[:12] if signal_copy.market_id else "?",
                tail_risk.get("risk_class"),
                tail_risk.get("reasons", []),
            )
            self._record_process_skip(signal_copy, "tail_risk_veto")
            return False
        barbell = self._apply_barbell_adjustment(signal_copy, tail_risk, original_size)
        signal_copy.payload["barbell"] = barbell
        near_certainty = self._apply_near_certainty_adjustment(
            signal_copy,
            active_markets=active_markets,
        )
        signal_copy.payload["near_certainty"] = near_certainty
        self._record_near_certainty(near_certainty)
        self._cap_signal_size(signal_copy, original_size)
        # Book exposure to the per-class ledger AFTER all multipliers
        # have been applied. Tracking here (not at execution time)
        # over-counts skipped signals, but for the shadow-default mode
        # that's fine — operators look at the trend, not the
        # cents-accurate level. settlement debits via signal_id.
        self._book_barbell_exposure(signal_copy, barbell)
        drawdown_scaling = self._apply_drawdown_size_scaling(signal_copy)
        signal_copy.payload["drawdown_size_scaling"] = drawdown_scaling
        if drawdown_scaling.get("blocked"):
            self._record_process_skip(signal_copy, "drawdown_size_scaling_block")
            return False
        self._record_per_market_submission(signal_copy)
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
        requested_by_tier: dict[StrategyTier, float] = {}
        for signal in self._pending_signals:
            if signal.recommended_size_usdc > 0:
                requested_by_tier[signal.tier] = (
                    requested_by_tier.get(signal.tier, 0.0)
                    + float(signal.recommended_size_usdc)
                )
        remaining_by_tier = self._effective_available_by_tier(requested_by_tier)
        self._last_skip_reasons = {}
        self._last_skipped_by_tier = {}

        for signal in self._pending_signals:
            tier = signal.tier
            available = remaining_by_tier.get(tier, 0.0)

            if signal.recommended_size_usdc <= 0:
                self._record_process_skip(signal, "non_positive_size")
                continue
            if signal.recommended_size_usdc > available:
                adjusted = available
                if adjusted < 1.0:
                    self._record_process_skip(signal, "tier_budget_below_min_order")
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
        self._refresh_peak_equity()
        self._append_executed_signal(signal)

    def record_processed(self, signal: StrategySignal) -> None:
        """记录信号已被编排器消费，但未进入真实执行."""
        self._append_executed_signal(signal)

    def record_settlement(self, tier: StrategyTier, amount: float, pnl: float) -> None:
        """仓位结算后释放敞口."""
        alloc = self._allocations.get(tier)
        if alloc:
            alloc.current_exposure = max(0, alloc.current_exposure - amount)
            alloc.realized_pnl += pnl
            self._refresh_peak_equity()

    def update_bankroll(self, new_bankroll: float) -> None:
        self._bankroll = new_bankroll
        self._refresh_peak_equity()

    def _append_executed_signal(self, signal: StrategySignal) -> None:
        self._executed_signals.append(signal)
        if len(self._executed_signals) > self._MAX_SIGNAL_HISTORY:
            del self._executed_signals[:-self._MAX_SIGNAL_HISTORY]

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
            "tail_risk": dict(self._tail_risk_stats),
            "near_certainty": {
                **dict(self._near_certainty_stats),
                "shadow_mode": self._near_certainty_classifier.shadow_mode,
            },
            "barbell": {
                "enabled": self._barbell_policy.enabled,
                "tail_budget_usdc": round(
                    float(self._barbell_policy.tail_budget_usdc), 4
                ),
                "exposure_usdc": {
                    k: round(float(v), 4)
                    for k, v in self._t2_class_exposure_usdc.items()
                },
                "ledger_size": len(self._t2_class_signal_ledger),
            },
            "sniper_gate": dict(self._sniper_gate_stats),
            "equity": {
                "current": round(self._current_equity(), 8),
                "peak": round(self._peak_equity, 8),
                "drawdown": round(self._current_drawdown(), 8),
            },
            "recent_overlays": list(self._overlay_history[-10:]),
            "last_skip_reasons": dict(self._last_skip_reasons),
            "last_skipped_by_tier": dict(self._last_skipped_by_tier),
        }
        return status

    def get_last_skip_reasons(self) -> dict[str, Any]:
        return {
            "reasons": dict(self._last_skip_reasons),
            "by_tier": dict(self._last_skipped_by_tier),
            "total": sum(self._last_skip_reasons.values()),
        }

    def _available_by_tier(self) -> dict[StrategyTier, float]:
        result = {}
        for tier, alloc in self._allocations.items():
            budget = self._bankroll * alloc.allocation_pct
            available = max(0, budget - alloc.current_exposure)
            result[tier] = available
        return result

    def _effective_available_by_tier(
        self,
        requested_by_tier: dict[StrategyTier, float],
    ) -> dict[StrategyTier, float]:
        static_available = self._available_by_tier()
        total_available = max(
            0.0,
            self._bankroll
            - sum(max(0.0, alloc.current_exposure) for alloc in self._allocations.values()),
        )
        effective: dict[StrategyTier, float] = {}
        for tier, available in static_available.items():
            requested = max(0.0, float(requested_by_tier.get(tier, 0.0)))
            effective[tier] = min(float(available), requested) if requested > 0 else 0.0

        spare = max(0.0, total_available - sum(effective.values()))
        if spare <= 1e-9:
            return effective

        unmet = {
            tier: requested - effective.get(tier, 0.0)
            for tier, requested in requested_by_tier.items()
            if requested > effective.get(tier, 0.0)
            and self._allocations.get(tier) is not None
            and self._allocations[tier].allocation_pct > 0
        }
        total_unmet = sum(unmet.values())
        if total_unmet <= 1e-9:
            return effective

        for tier, amount in unmet.items():
            effective[tier] = effective.get(tier, 0.0) + min(
                amount,
                spare * (amount / total_unmet),
            )
        return effective

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

    def _record_process_skip(self, signal: StrategySignal, reason: str) -> None:
        self._last_skip_reasons[reason] = self._last_skip_reasons.get(reason, 0) + 1
        tier_name = getattr(signal.tier, "name", str(signal.tier))
        self._last_skipped_by_tier[tier_name] = self._last_skipped_by_tier.get(tier_name, 0) + 1

    def _check_per_market_rate_cap(self, signal: StrategySignal) -> bool:
        if self._max_signals_per_market_per_hour <= 0:
            return True
        if signal.tier not in self._RATE_CAPPED_TIERS:
            return True
        if not signal.market_id:
            return True
        key = (signal.tier, signal.market_id)
        history = self._signal_history_by_market.get(key, [])
        cutoff = time.time() - 3600.0
        fresh = [ts for ts in history if ts >= cutoff]
        if fresh != history:
            self._signal_history_by_market[key] = fresh
        return len(fresh) < self._max_signals_per_market_per_hour

    def _log_rate_cap_skip(self, signal: StrategySignal) -> None:
        key = (signal.tier, signal.market_id)
        now = time.time()
        last_ts, suppressed = self._rate_cap_log_state.get(key, (0.0, 0))
        if now - last_ts < 60.0:
            self._rate_cap_log_state[key] = (last_ts, suppressed + 1)
            return
        LOG.info(
            "策略信号被 per-market 速率上限拦截: tier=%s market=%s cap=%d/h suppressed=%d",
            signal.tier,
            signal.market_id[:12] if signal.market_id else "?",
            self._max_signals_per_market_per_hour,
            suppressed,
        )
        self._rate_cap_log_state[key] = (now, 0)

    def _record_per_market_submission(self, signal: StrategySignal) -> None:
        if self._max_signals_per_market_per_hour <= 0:
            return
        if signal.tier not in self._RATE_CAPPED_TIERS:
            return
        if not signal.market_id:
            return
        key = (signal.tier, signal.market_id)
        history = self._signal_history_by_market.setdefault(key, [])
        history.append(time.time())
        cap = self._max_signals_per_market_per_hour * 4
        if cap > 0 and len(history) > cap:
            del history[: len(history) - cap]

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

        source_diversity = len({
            str(source)
            for row in matched_rows
            for source in row.get("sources", [])
            if str(source)
        })
        aligned_rows = [
            row for row in matched_rows
            if (
                (action == "BUY_YES" and row.get("stance") == "bullish")
                or (action == "BUY_NO" and row.get("stance") == "bearish")
            )
        ]
        conflict_rows = [
            row for row in matched_rows
            if (
                (action == "BUY_YES" and row.get("stance") == "bearish")
                or (action == "BUY_NO" and row.get("stance") == "bullish")
            )
        ]
        resonance_score = self._research_resonance_score(
            matched_count=len(matched_rows),
            source_diversity=source_diversity,
            avg_confidence=avg_conf,
            aligned_count=len(aligned_rows),
            conflict_count=len(conflict_rows),
        )
        if resonance_score >= 0.70 and aligned and not mixed:
            size_multiplier *= 1.08
            confidence_delta += 0.04
            reasons.append("research_resonance")
        elif resonance_score <= -0.70 and conflicting and not mixed:
            size_multiplier *= 0.75
            confidence_delta -= 0.05
            reasons.append("conflicting_research_resonance")
            if len(conflict_rows) >= 3 and avg_conf >= 0.78:
                veto = True
                reasons.append("three_signal_conflict_veto")

        if avg_freshness > 6 * 3600:
            size_multiplier *= 0.85
            confidence_delta -= 0.03
            reasons.append("stale_research")

        if len(matched_rows) == 1 and avg_conf < 0.55:
            size_multiplier *= 0.90
            reasons.append("weak_research_coverage")

        size_multiplier = max(0.25, min(1.35, size_multiplier))
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
            "source_diversity": source_diversity,
            "resonance_score": round(resonance_score, 3),
            "size_multiplier": round(size_multiplier, 3),
            "confidence_delta": round(confidence_delta, 3),
            "reasons": reasons,
            "source_counts": source_counts,
        }

    def _research_resonance_score(
        self,
        *,
        matched_count: int,
        source_diversity: int,
        avg_confidence: float,
        aligned_count: int,
        conflict_count: int,
    ) -> float:
        if matched_count <= 0:
            return 0.0
        directional = aligned_count - conflict_count
        direction_strength = directional / max(1, matched_count)
        coverage = min(1.0, matched_count / 3.0)
        diversity = min(1.0, source_diversity / 3.0)
        confidence = max(0.0, min(1.0, avg_confidence))
        return direction_strength * ((coverage * 0.40) + (diversity * 0.25) + (confidence * 0.35))

    def _apply_tail_risk_adjustment(
        self,
        signal: StrategySignal,
        *,
        active_markets: list[Any],
    ) -> dict[str, Any]:
        action = self._resolve_signal_action(signal)
        # T0 STRUCTURAL_ARB is price-locked (Σask<1-fee). Multi-leg arb's
        # PnL doesn't depend on which way the underlying resolves, so the
        # tail-risk overlay is a no-op there.
        # T3 MARKET_MAKING is INCLUDED here even though it has no
        # BUY_YES/BUY_NO action — a maker quote on a "Will US invade Iran"
        # market is exactly the kind of directional, no-fair-value tail
        # exposure the overlay exists to block. The veto branch (below)
        # is what stops those quotes from being placed at all.
        if signal.tier not in {
            StrategyTier.STATISTICAL_ARB,
            StrategyTier.CROSS_PLATFORM,
            StrategyTier.MARKET_MAKING,
        }:
            return {"applied": False, "risk_class": "not_applicable", "size_multiplier": 1.0, "reasons": [], "veto": False}
        # Directional gate applies to T2/T1 only — T3 maker quotes don't
        # carry an action and we still want to classify them.
        if signal.tier != StrategyTier.MARKET_MAKING and action not in {"BUY_YES", "BUY_NO"}:
            return {"applied": False, "risk_class": "not_directional", "size_multiplier": 1.0, "reasons": [], "veto": False}

        market = self._find_market(signal.market_id, active_markets)
        text = self._tail_risk_text(signal, market)
        classification = self._tail_risk_classifier.classify(text)
        # T3 maker quotes are non-directional (profit from spread, not
        # resolution direction). Apply a floor on size_multiplier so T3
        # is never fully zeroed — reduced exposure is enough.
        _T3_SIZE_FLOOR = 0.30
        if signal.tier == StrategyTier.MARKET_MAKING and not classification.veto:
            effective_mult = max(_T3_SIZE_FLOOR, classification.size_multiplier)
            effective_conf_delta = max(-0.3, classification.confidence_delta)
        else:
            effective_mult = classification.size_multiplier
            effective_conf_delta = classification.confidence_delta
        signal.recommended_size_usdc = max(0.0, signal.recommended_size_usdc * effective_mult)
        signal.confidence = max(0.0, min(1.0, signal.confidence + effective_conf_delta))
        effective_veto = bool(classification.veto)
        return {
            "applied": True,
            "risk_class": classification.risk_class,
            "size_multiplier": round(effective_mult, 3),
            "confidence_delta": round(effective_conf_delta, 3),
            "reasons": classification.reasons,
            "veto": effective_veto,
        }

    def _apply_barbell_adjustment(
        self,
        signal: StrategySignal,
        tail_risk: dict[str, Any],
        original_size: float,
    ) -> dict[str, Any]:
        """Maybe relax the tail_risk size discount under the barbell pool.

        Only acts on STATISTICAL_ARB tier (where the tail/data_driven
        split is meaningful). When the policy returns a multiplier
        override, the rule's discount is REPLACED at the original size
        — i.e. we don't multiply discounts. Otherwise the signal is
        left as `_apply_tail_risk_adjustment` produced it.
        """
        if signal.tier != StrategyTier.STATISTICAL_ARB:
            return {
                "applied": False,
                "enabled": self._barbell_policy.enabled,
                "bucket": "not_applicable",
                "reasons": [],
            }
        risk_class = str(tail_risk.get("risk_class", ""))
        # Use the original (pre-adjustment) size to ask the policy
        # "would this fit?"; the policy itself decides whether to
        # override the multiplier.
        tail_exposure = self._t2_class_exposure_usdc.get("tail", 0.0)
        decision = self._barbell_policy.decide(
            risk_class=risk_class,
            signal_size_usdc=original_size,
            current_tail_exposure_usdc=tail_exposure,
        )
        applied = False
        if decision.multiplier_override is not None:
            # Replace whatever tail_risk already applied with the
            # relaxed multiplier, against the *original* size.
            signal.recommended_size_usdc = max(
                0.0, float(original_size) * float(decision.multiplier_override)
            )
            applied = True
        return {
            "applied": applied,
            "enabled": self._barbell_policy.enabled,
            "bucket": decision.bucket,
            "multiplier_override": (
                None
                if decision.multiplier_override is None
                else round(float(decision.multiplier_override), 3)
            ),
            "tail_exposure_before_usdc": round(float(tail_exposure), 4),
            "tail_budget_usdc": round(float(self._barbell_policy.tail_budget_usdc), 4),
            "reasons": list(decision.reasons),
        }

    def _book_barbell_exposure(
        self, signal: StrategySignal, barbell: dict[str, Any]
    ) -> None:
        """Record this submission against the per-class exposure ledger."""
        if signal.tier != StrategyTier.STATISTICAL_ARB:
            return
        bucket = str(barbell.get("bucket", "data_driven"))
        if bucket not in self._t2_class_exposure_usdc:
            return
        amount = max(0.0, float(signal.recommended_size_usdc))
        if amount <= 0:
            return
        self._t2_class_exposure_usdc[bucket] += amount
        if signal.signal_id:
            self._t2_class_signal_ledger[signal.signal_id] = (bucket, amount)
        # Bound ledger size so the dict can't grow unbounded.
        if len(self._t2_class_signal_ledger) > self._MAX_SIGNAL_HISTORY:
            oldest = next(iter(self._t2_class_signal_ledger))
            self._t2_class_signal_ledger.pop(oldest, None)

    def _release_barbell_exposure(self, signal_id: str, amount: float) -> None:
        """Debit a previously-booked entry. Use on settlement of T2 signals."""
        if not signal_id:
            return
        booked = self._t2_class_signal_ledger.pop(signal_id, None)
        if booked is None:
            return
        bucket, booked_amount = booked
        debit = min(booked_amount, max(0.0, float(amount)))
        self._t2_class_exposure_usdc[bucket] = max(
            0.0, self._t2_class_exposure_usdc[bucket] - debit
        )

    def _apply_near_certainty_adjustment(
        self,
        signal: StrategySignal,
        *,
        active_markets: list[Any],
    ) -> dict[str, Any]:
        """Apply (or shadow-log) the near-certainty discount.

        The rule reads the signal's `market_prob` field (set by the
        statistical detector) — that is the price on the side the
        signal is asking the bot to BUY, in [0, 1]. Tier filter mirrors
        tail_risk: only T2 / T1 directional signals (T0 structural arb
        is price-locked; T3 maker is bid-ask symmetric and benefits
        from extreme prices via spread, not from buying them).
        """
        action = self._resolve_signal_action(signal)
        if signal.tier not in {StrategyTier.STATISTICAL_ARB, StrategyTier.CROSS_PLATFORM}:
            return {
                "applied": False,
                "shadow_mode": self._near_certainty_classifier.shadow_mode,
                "risk_zone": "not_applicable",
                "size_multiplier": 1.0,
                "reasons": [],
            }
        if action not in {"BUY_YES", "BUY_NO"}:
            return {
                "applied": False,
                "shadow_mode": self._near_certainty_classifier.shadow_mode,
                "risk_zone": "not_directional",
                "size_multiplier": 1.0,
                "reasons": [],
            }

        market_price = self._resolve_signal_market_price(signal, active_markets, action)
        result = self._near_certainty_classifier.classify(market_price)

        if result.applied and not self._near_certainty_classifier.shadow_mode:
            signal.recommended_size_usdc = max(
                0.0, signal.recommended_size_usdc * result.size_multiplier
            )
            signal.confidence = max(
                0.0, min(1.0, signal.confidence + result.confidence_delta)
            )

        return {
            "applied": result.applied,
            "shadow_mode": result.shadow_mode,
            "risk_zone": result.risk_zone,
            "market_price": (
                None if result.market_price is None else round(result.market_price, 4)
            ),
            "size_multiplier": round(result.size_multiplier, 3),
            "confidence_delta": round(result.confidence_delta, 3),
            "reasons": list(result.reasons),
        }

    def _resolve_signal_market_price(
        self,
        signal: StrategySignal,
        active_markets: list[Any],
        action: str,
    ) -> float | None:
        """Return the price of the side the signal wants to BUY, or None.

        Statistical signals carry `market_prob` in their payload (YES-
        space mid). For BUY_NO the buy-side price is 1 - market_prob.
        For other tiers we fall back to the active-market token list.
        """
        payload = signal.payload or {}
        yes_price = payload.get("market_prob")
        if isinstance(yes_price, (int, float)):
            yes_price = float(yes_price)
            if action == "BUY_NO":
                return max(0.0, min(1.0, 1.0 - yes_price))
            return max(0.0, min(1.0, yes_price))

        market = self._find_market(signal.market_id, active_markets)
        if market is None:
            return None
        tokens = getattr(market, "tokens", []) or []
        target_outcome = "yes" if action == "BUY_YES" else "no"
        for token in tokens:
            outcome = (getattr(token, "outcome", "") or "").strip().lower()
            if outcome == target_outcome:
                price = float(getattr(token, "price", 0.0) or 0.0)
                if 0.0 < price <= 1.0:
                    return price
        return None

    def _record_near_certainty(self, near_certainty: dict[str, Any]) -> None:
        self._near_certainty_stats["evaluated"] += 1
        if not near_certainty.get("applied"):
            return
        zone = near_certainty.get("risk_zone", "")
        shadow = bool(near_certainty.get("shadow_mode"))
        if zone == "high_certainty":
            self._near_certainty_stats["would_apply_high"] += 1
            if not shadow:
                self._near_certainty_stats["applied_high"] += 1
        elif zone == "longshot":
            self._near_certainty_stats["would_apply_longshot"] += 1
            if not shadow:
                self._near_certainty_stats["applied_longshot"] += 1

    def _tail_risk_text(self, signal: StrategySignal, market: Any | None) -> str:
        parts = [signal.description, signal.market_id]
        if market is not None:
            parts.extend([
                getattr(market, "question", ""),
                getattr(market, "event_title", ""),
                getattr(market, "event_slug", ""),
                getattr(market, "slug", ""),
            ])
            raw = getattr(market, "raw", {}) or {}
            for key in ("description", "category", "tags", "game_start_time", "resolutionSource"):
                parts.append(str(raw.get(key, "")))
        return " ".join(part for part in parts if part).lower()

    def _record_tail_risk(self, tail_risk: dict[str, Any]) -> None:
        if not tail_risk.get("applied"):
            return
        self._tail_risk_stats["applied"] += 1
        multiplier = float(tail_risk.get("size_multiplier", 1.0))
        if multiplier < 1.0:
            self._tail_risk_stats["penalized"] += 1
        if tail_risk.get("risk_class") == "high_tail":
            self._tail_risk_stats["high_risk"] += 1
        if tail_risk.get("veto"):
            self._tail_risk_stats["vetoed"] = self._tail_risk_stats.get("vetoed", 0) + 1

    def _cap_signal_size(self, signal: StrategySignal, original_size: float) -> None:
        if original_size <= 0:
            return
        max_size = original_size * self._max_signal_size_multiplier
        if signal.recommended_size_usdc <= max_size:
            return
        signal.recommended_size_usdc = max_size
        signal.payload["risk_size_cap"] = {
            "max_signal_size_multiplier": round(self._max_signal_size_multiplier, 3),
            "base_size_usdc": round(original_size, 8),
            "capped_size_usdc": round(max_size, 8),
        }

    def _apply_drawdown_size_scaling(self, signal: StrategySignal) -> dict[str, Any]:
        if signal.tier not in {StrategyTier.STATISTICAL_ARB, StrategyTier.CROSS_PLATFORM}:
            return {"applied": False, "multiplier": 1.0, "drawdown": round(self._current_drawdown(), 8)}
        drawdown = self._current_drawdown()
        multiplier = self._drawdown_multiplier(drawdown)
        payload = {
            "applied": multiplier < 1.0,
            "blocked": multiplier <= 0.0,
            "multiplier": multiplier,
            "drawdown": round(drawdown, 8),
            "current_equity": round(self._current_equity(), 8),
            "peak_equity": round(self._peak_equity, 8),
        }
        if multiplier <= 0.0:
            signal.recommended_size_usdc = 0.0
            return payload
        if multiplier < 1.0:
            signal.recommended_size_usdc = max(0.0, signal.recommended_size_usdc * multiplier)
        return payload

    @staticmethod
    def _drawdown_multiplier(drawdown: float) -> float:
        if drawdown >= 0.15:
            return 0.0
        if drawdown >= 0.10:
            return 0.25
        if drawdown >= 0.05:
            return 0.50
        if drawdown >= 0.03:
            return 0.75
        return 1.0

    def _current_equity(self) -> float:
        return float(self._bankroll) + sum(float(alloc.realized_pnl) for alloc in self._allocations.values())

    def _current_drawdown(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - self._current_equity()) / self._peak_equity)

    def _refresh_peak_equity(self) -> None:
        self._peak_equity = max(self._peak_equity, self._current_equity())

    def _record_sniper_gate(self, decision: Any) -> None:
        self._sniper_gate_stats["applied"] += 1
        if getattr(decision, "accepted", False):
            self._sniper_gate_stats["accepted"] += 1
        else:
            self._sniper_gate_stats["rejected"] += 1

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
