"""Edge 决策引擎：融合 BookStore + VolEstimator + FairValueModel 产生交易信号.

取代原来分散在 statistical_model / arbitrage_detector 中的信号逻辑，
提供统一的决策接口。

职责划分:
  EdgeEngine: 计算 edge score / direction / confidence，决定是否交易
  StrategyOrchestrator: 分配资金、排序执行
  ExecutionEngine: 下单

信号流:
  BookStore.snapshot()  ──┐
  VolEstimator.snapshot() ─┼─→ EdgeEngine.evaluate() → EdgeDecision
  FairValueModel          ──┘                              │
                                                           ↓
                                               StrategyOrchestrator.submit_signal()

移植自 mlmodelpoly/edge_engine.py，精简掉 TAAPI/Binance 特有逻辑，
适配本项目的通用预测市场场景。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.fair_value_model import compute_edge_bps, compute_general_fair_value
from polymarket_arb.utils_time import now_ms
from polymarket_arb.volatility_estimator import VolEstimator

LOG = logging.getLogger(__name__)


@dataclass
class EdgeDecision:
    """Edge 决策结果."""

    ts_ms: int
    market_id: str
    direction: str  # "BUY_YES" | "BUY_NO" | "NONE"
    edge_bps: float  # 基点 edge
    fair_value: float  # 模型公允概率
    market_price: float  # 市场价格
    confidence: float  # 0-1
    veto: bool = False
    reasons: list[str] = field(default_factory=list)
    veto_reasons: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ts_ms": self.ts_ms,
            "market_id": self.market_id,
            "direction": self.direction,
            "edge_bps": round(self.edge_bps, 1),
            "fair_value": round(self.fair_value, 4),
            "market_price": round(self.market_price, 4),
            "confidence": round(self.confidence, 3),
            "veto": self.veto,
            "reasons": self.reasons,
            "veto_reasons": self.veto_reasons if self.veto_reasons else None,
        }


class EdgeEngine:
    """融合订单簿、波动率和公允价值的交易 edge 决策引擎.

    Args:
        min_edge_bps: 最低 edge 阈值（基点）
        min_depth: 最低可用深度（USDC）
        max_spread_bps: 最大可接受 spread（基点）
        min_confidence: 最低置信度
    """

    def __init__(
        self,
        min_edge_bps: float = 100.0,
        min_depth: float = 50.0,
        max_spread_bps: float = 500.0,
        min_confidence: float = 0.4,
    ) -> None:
        self.min_edge_bps = min_edge_bps
        self.min_depth = min_depth
        self.max_spread_bps = max_spread_bps
        self.min_confidence = min_confidence
        self._last_decision: Optional[EdgeDecision] = None

    def evaluate(
        self,
        book_store: EnhancedBookStore,
        vol_estimator: Optional[VolEstimator] = None,
        *,
        spot_fair_up: Optional[float] = None,
        momentum_yes: float = 0.0,
        momentum_no: float = 0.0,
        cross_market_dev: float = 0.0,
    ) -> EdgeDecision:
        """评估当前市场状态，产生交易决策.

        Args:
            book_store: 双侧订单簿
            vol_estimator: 波动率估算器（可选，用于置信度调整）
            spot_fair_up: 来自 FairValueModel 的 UP 公允概率（可选）
            momentum_yes / momentum_no: 动量信号
            cross_market_dev: 跨市场偏差信号

        Returns:
            EdgeDecision
        """
        ts = now_ms()
        snap = book_store.snapshot()
        reasons: list[str] = []
        veto_reasons: list[str] = []
        payload: dict[str, Any] = {}

        if not snap["connected"]:
            return self._veto_decision(ts, snap, ["ws_disconnected"])

        yes_data = snap["yes"]
        no_data = snap["no"]

        yes_mid = yes_data.get("mid")
        no_mid = no_data.get("mid")
        if yes_mid is None or no_mid is None:
            return self._veto_decision(ts, snap, ["no_mid_price"])

        yes_spread_bps = yes_data.get("spread_bps")
        no_spread_bps = no_data.get("spread_bps")

        yes_imbalance = yes_data.get("imbalance") or 0.0
        no_imbalance = no_data.get("imbalance") or 0.0

        payload["yes_mid"] = yes_mid
        payload["no_mid"] = no_mid
        payload["yes_spread_bps"] = yes_spread_bps
        payload["no_spread_bps"] = no_spread_bps
        payload["yes_imbalance"] = yes_imbalance
        payload["no_imbalance"] = no_imbalance

        yes_fair = compute_general_fair_value(
            yes_mid,
            obi_score=yes_imbalance,
            momentum_score=momentum_yes,
            cross_market_deviation=cross_market_dev,
            spot_fair=spot_fair_up,
        )
        no_fair = compute_general_fair_value(
            no_mid,
            obi_score=no_imbalance,
            momentum_score=momentum_no,
            cross_market_deviation=-cross_market_dev,
            spot_fair=(1.0 - spot_fair_up) if spot_fair_up is not None else None,
        )

        yes_edge = compute_edge_bps(yes_fair, yes_mid) or 0.0
        no_edge = compute_edge_bps(no_fair, no_mid) or 0.0

        payload["yes_fair"] = round(yes_fair, 4)
        payload["no_fair"] = round(no_fair, 4)
        payload["yes_edge_bps"] = round(yes_edge, 1)
        payload["no_edge_bps"] = round(no_edge, 1)

        direction = "NONE"
        chosen_edge = 0.0
        chosen_fair = 0.5
        chosen_market = 0.5

        if yes_edge > no_edge and yes_edge > 0:
            direction = "BUY_YES"
            chosen_edge = yes_edge
            chosen_fair = yes_fair
            chosen_market = yes_mid
            reasons.append("yes_underpriced")

            if yes_spread_bps is not None and yes_spread_bps > self.max_spread_bps:
                veto_reasons.append("yes_spread_too_wide")
            yes_depth = yes_data.get("ask_depth_top5", 0)
            if yes_depth < self.min_depth:
                veto_reasons.append("yes_low_depth")

        elif no_edge > 0:
            direction = "BUY_NO"
            chosen_edge = no_edge
            chosen_fair = no_fair
            chosen_market = no_mid
            reasons.append("no_underpriced")

            if no_spread_bps is not None and no_spread_bps > self.max_spread_bps:
                veto_reasons.append("no_spread_too_wide")
            no_depth = no_data.get("ask_depth_top5", 0)
            if no_depth < self.min_depth:
                veto_reasons.append("no_low_depth")

        if chosen_edge < self.min_edge_bps:
            direction = "NONE"
            chosen_edge = 0.0
            reasons.clear()

        confidence = self._compute_confidence(
            edge_bps=chosen_edge,
            imbalance=yes_imbalance if direction == "BUY_YES" else no_imbalance,
            vol_estimator=vol_estimator,
        )
        if confidence < self.min_confidence and direction != "NONE":
            veto_reasons.append("low_confidence")

        veto = bool(veto_reasons) and direction != "NONE"
        if veto:
            direction = "NONE"
            chosen_edge = 0.0

        decision = EdgeDecision(
            ts_ms=ts,
            market_id=snap.get("market_id", ""),
            direction=direction,
            edge_bps=chosen_edge,
            fair_value=chosen_fair,
            market_price=chosen_market,
            confidence=confidence,
            veto=veto,
            reasons=reasons,
            veto_reasons=veto_reasons,
            payload=payload,
        )
        self._last_decision = decision
        return decision

    def get_last_decision(self) -> Optional[EdgeDecision]:
        return self._last_decision

    def _compute_confidence(
        self,
        edge_bps: float,
        imbalance: float,
        vol_estimator: Optional[VolEstimator],
    ) -> float:
        """综合 edge 大小、OBI 和波动率计算置信度."""
        conf = min(1.0, edge_bps / 500.0)

        conf += 0.1 * abs(imbalance)

        if vol_estimator is not None:
            vol_snap = vol_estimator.snapshot()
            fast_vol = vol_snap.get("sigma_fast_15m")
            slow_vol = vol_snap.get("sigma_slow_15m")
            if fast_vol and slow_vol and slow_vol > 0:
                vol_ratio = fast_vol / slow_vol
                if vol_ratio > 2.0:
                    conf *= 0.7
                elif vol_ratio < 0.8:
                    conf *= 1.1

        return max(0.0, min(1.0, conf))

    def _veto_decision(self, ts: int, snap: dict, veto_reasons: list[str]) -> EdgeDecision:
        return EdgeDecision(
            ts_ms=ts,
            market_id=snap.get("market_id", ""),
            direction="NONE",
            edge_bps=0.0,
            fair_value=0.5,
            market_price=0.5,
            confidence=0.0,
            veto=True,
            veto_reasons=veto_reasons,
        )
