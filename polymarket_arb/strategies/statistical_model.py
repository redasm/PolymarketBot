"""统计模型定价引擎：用概率模型识别 mispricing.

这是利润最大化的核心：不是等结构性套利出现（所有人都在做），
而是在市场价格偏离真实概率时主动交易。

分层模型:

Layer 1 — 基准模型 (Naive Bayesian)
  用历史赔率变化和事件特征建立先验概率，
  当新信息到达时更新后验。

Layer 2 — 市场微结构信号
  - 订单簿不平衡 (bid/ask imbalance)
  - 大单检测
  - 成交速率变化
  - 价格动量

Layer 3 — 跨市场信号
  - 相关市场的价格变动（如："Trump wins" 和 "Republican wins"）
  - 宏观事件冲击（利率决议、GDP 数据等）

定价偏差 = P(model) - P(market)
当 |偏差| > threshold 时:
  偏差 > 0 → 市场低估 → 买入
  偏差 < 0 → 市场高估 → 卖出

Kelly 公式决定仓位大小，threshold 控制触发灵敏度。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from polymarket_arb.confidence import confidence_from_signal_strength
from polymarket_arb.fair_value_model import compute_general_fair_value

LOG = logging.getLogger(__name__)


@dataclass
class ProbabilityEstimate:
    """概率估计结果."""

    market_id: str
    outcome: str
    model_prob: float  # 模型估计的真实概率
    market_prob: float  # 市场隐含概率 (= 价格)
    deviation: float  # model - market
    deviation_pct: float  # deviation / market
    confidence: float  # 模型对自身估计的置信度 (0-1)
    signals: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def is_underpriced(self) -> bool:
        return self.deviation > 0

    @property
    def is_overpriced(self) -> bool:
        return self.deviation < 0

    @property
    def abs_edge(self) -> float:
        return abs(self.deviation)


class OrderBookImbalanceSignal:
    """订单簿不平衡信号：bid_depth / ask_depth 的偏离.

    原理:
    如果 bid 侧深度远大于 ask 侧 → 买压大于卖压 → 价格可能上涨
    反之亦然。

    这个信号的预测力在 1-5 分钟的时间尺度上最强。
    """

    @staticmethod
    def compute(bids_total_size: float, asks_total_size: float, levels: int = 5) -> float:
        """计算 OBI (Order Book Imbalance) 指标.

        Returns:
            -1.0 到 1.0 之间的值。
            > 0 表示买压占优（价格可能上涨）
            < 0 表示卖压占优（价格可能下跌）
        """
        total = bids_total_size + asks_total_size
        if total <= 0:
            return 0.0
        return (bids_total_size - asks_total_size) / total


class MomentumSignal:
    """价格动量信号：短期价格趋势.

    基于最近 N 个价格变化的方向和幅度。
    """

    def __init__(self, window: int = 20):
        self._window = window
        self._prices: dict[str, list[tuple[float, float]]] = {}  # token_id -> [(ts, mid_price)]
        self._max_tracked_tokens = max(200, window * 20)

    def record(self, token_id: str, mid_price: float) -> None:
        now = time.time()
        history = self._prices.setdefault(token_id, [])
        history.append((now, mid_price))
        if len(history) > self._window * 2:
            self._prices[token_id] = history[-self._window:]
        if len(self._prices) > self._max_tracked_tokens:
            oldest_token = min(
                self._prices.items(),
                key=lambda item: item[1][-1][0] if item[1] else float("inf"),
            )[0]
            if oldest_token != token_id:
                self._prices.pop(oldest_token, None)

    def compute(self, token_id: str) -> float:
        """计算动量分数.

        Returns:
            > 0 上涨趋势, < 0 下跌趋势, 0 无数据
        """
        history = self._prices.get(token_id, [])
        if len(history) < 3:
            return 0.0

        recent = history[-self._window:]
        if len(recent) < 2:
            return 0.0

        first_price = recent[0][1]
        last_price = recent[-1][1]
        if first_price <= 0:
            return 0.0

        return (last_price - first_price) / first_price


class BayesianPriceModel:
    """贝叶斯概率模型.

    将市场价格视为先验，用额外信号更新后验。

    P(event | signals) ∝ P(signals | event) × P(event)

    其中 P(event) = market_price（先验）
    P(signals | event) 由各信号的条件似然估计。

    当有来自 FairValueModel 的现货锚定（spot_fair）时，
    委托给 compute_general_fair_value 做更精确的融合。
    """

    def __init__(
        self,
        obi_weight: float = 0.10,
        momentum_weight: float = 0.08,
        cross_market_weight: float = 0.15,
        spot_weight: float = 0.40,
        min_deviation_threshold: float = 0.05,
    ):
        self._obi_weight = obi_weight
        self._momentum_weight = momentum_weight
        self._cross_weight = cross_market_weight
        self._spot_weight = spot_weight
        self._min_threshold = min_deviation_threshold
        self._momentum = MomentumSignal()

    def estimate(
        self,
        market_price: float,
        *,
        obi_score: float = 0.0,
        momentum_score: float = 0.0,
        cross_market_deviation: float = 0.0,
        spot_fair: Optional[float] = None,
    ) -> float:
        """综合信号估算后验概率.

        Args:
            market_price: 当前市场价 (0-1)
            obi_score: 订单簿不平衡 (-1 to 1)
            momentum_score: 动量信号
            cross_market_deviation: 相关市场的价格偏差
            spot_fair: 来自 FairValueModel 的现货锚定概率（如果可用）

        Returns:
            模型估计的概率 (0-1)
        """
        # Extreme-price contracts have asymmetric downside. Treat short-term
        # OBI/momentum as less reliable near 0/1 instead of pressing harder.
        microstructure_multiplier = self._microstructure_multiplier(market_price)
        return compute_general_fair_value(
            market_price,
            obi_score=obi_score * microstructure_multiplier,
            momentum_score=momentum_score * microstructure_multiplier,
            cross_market_deviation=cross_market_deviation,
            spot_fair=spot_fair,
            obi_weight=self._obi_weight,
            momentum_weight=self._momentum_weight,
            cross_weight=self._cross_weight,
            spot_weight=self._spot_weight,
        )

    @staticmethod
    def _microstructure_multiplier(market_price: float) -> float:
        price = max(0.0, min(1.0, float(market_price)))
        return max(0.25, 1.0 - min(0.75, abs(price - 0.5) * 1.5))


class StatisticalMispricingDetector:
    """统计模型 mispricing 检测器.

    整合多个信号源，输出综合概率估计和 mispricing 判定。
    """

    def __init__(
        self,
        model: Optional[BayesianPriceModel] = None,
        min_deviation: float = 0.05,
        min_confidence: float = 0.5,
    ):
        self._model = model or BayesianPriceModel()
        self._min_deviation = min_deviation
        self._min_confidence = min_confidence
        self._momentum = MomentumSignal()

    @staticmethod
    def _normalize_signal_strength(value: float) -> float:
        return min(1.0, abs(float(value)))

    def analyze(
        self,
        market_id: str,
        outcome: str,
        market_price: float,
        bids_total_size: float,
        asks_total_size: float,
        mid_price: Optional[float] = None,
        related_market_prices: Optional[dict[str, Any]] = None,
    ) -> Optional[ProbabilityEstimate]:
        """分析单个市场是否存在 mispricing.

        Returns:
            ProbabilityEstimate 或 None（无显著偏差时）
        """
        estimate = self.estimate_market_probability(
            market_id=market_id,
            outcome=outcome,
            market_price=market_price,
            bids_total_size=bids_total_size,
            asks_total_size=asks_total_size,
            mid_price=mid_price,
            related_market_prices=related_market_prices,
        )
        deviation = estimate.deviation
        confidence = estimate.confidence

        if abs(deviation) < self._min_deviation:
            return None
        if confidence < self._min_confidence:
            return None

        return estimate

    def estimate_market_probability(
        self,
        *,
        market_id: str,
        outcome: str,
        market_price: float,
        bids_total_size: float,
        asks_total_size: float,
        mid_price: Optional[float] = None,
        related_market_prices: Optional[dict[str, Any]] = None,
    ) -> ProbabilityEstimate:
        if mid_price is not None:
            self._momentum.record(market_id, mid_price)

        obi = OrderBookImbalanceSignal.compute(bids_total_size, asks_total_size)
        momentum = self._momentum.compute(market_id)
        cross_dev = self._compute_cross_market_signal(market_id, market_price, related_market_prices)
        microstructure_multiplier = self._model._microstructure_multiplier(market_price)

        model_prob = self._model.estimate(
            market_price,
            obi_score=obi,
            momentum_score=momentum,
            cross_market_deviation=cross_dev,
        )

        deviation = model_prob - market_price
        deviation_pct = deviation / market_price if market_price > 0 else 0

        components = [
            self._normalize_signal_strength(obi * microstructure_multiplier),
            self._normalize_signal_strength(momentum * microstructure_multiplier),
        ]
        if abs(cross_dev) > 0:
            components.append(min(1.0, abs(cross_dev) * 4.0))
        active = [value for value in components if value > 0]
        signal_strength = (sum(active) / len(active)) if active else 0.0
        confidence = confidence_from_signal_strength(signal_strength, scale=1.2)
        if len(active) == 1:
            confidence = min(confidence, 0.7)

        return ProbabilityEstimate(
            market_id=market_id,
            outcome=outcome,
            model_prob=model_prob,
            market_prob=market_price,
            deviation=deviation,
            deviation_pct=deviation_pct,
            confidence=confidence,
            signals={
                "obi": obi,
                "momentum": momentum,
                "cross_market": cross_dev,
                "microstructure_multiplier": microstructure_multiplier,
            },
        )

    def _compute_cross_market_signal(
        self,
        market_id: str,
        market_price: float,
        related: Optional[dict[str, Any]],
    ) -> float:
        """从相关市场价格计算偏差信号.

        例: "Trump wins election" 和 "Republican wins election"
        如果 P(Trump) = 0.60 但 P(Republican) = 0.55，
        逻辑上 P(Trump) <= P(Republican)，存在矛盾 → 套利信号。
        """
        if not related:
            return 0.0

        deviations: list[float] = []
        for related_id, raw in related.items():
            if isinstance(raw, dict):
                related_price = float(raw.get("price", 0.0) or 0.0)
                relation = str(raw.get("relation", "peer")).lower()
                weight = float(raw.get("weight", 1.0) or 1.0)
            else:
                related_price = float(raw or 0.0)
                relation = "peer"
                weight = 1.0

            if related_id == market_id or related_price <= 0 or related_price >= 1:
                continue

            # peer: 同主题市场价格更高 => 当前市场更可能被低估（正信号）
            if relation == "peer":
                deviations.append((related_price - market_price) * weight)
                continue

            # upper_bound: 当前概率应 <= 相关市场价格（例如更早 deadline <= 更晚 deadline）
            if relation == "upper_bound":
                if market_price > related_price:
                    deviations.append((related_price - market_price) * weight)
                continue

            # lower_bound: 当前概率应 >= 相关市场价格（例如更晚 deadline >= 更早 deadline）
            if relation == "lower_bound":
                if market_price < related_price:
                    deviations.append((related_price - market_price) * weight)
                continue

        if not deviations:
            return 0.0

        avg = sum(deviations) / len(deviations)
        return max(-0.35, min(0.35, avg))


def format_mispricing_zh(est: ProbabilityEstimate) -> str:
    direction = "被低估 📈" if est.is_underpriced else "被高估 📉"
    lines = [
        f"📊 统计偏差信号: {est.outcome} {direction}",
        f"市场价: {est.market_prob:.2%} → 模型估值: {est.model_prob:.2%}",
        f"偏差: {est.deviation:+.2%} ({est.deviation_pct:+.1%})",
        f"置信度: {est.confidence:.0%}",
        f"信号: OBI={est.signals.get('obi', 0):.2f} "
        f"动量={est.signals.get('momentum', 0):.2f} "
        f"跨市={est.signals.get('cross_market', 0):.2f}",
    ]
    return "\n".join(lines)
