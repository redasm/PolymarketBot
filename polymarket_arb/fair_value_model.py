"""Fair Value 定价模型：基于对数正态分布计算二元结果的公允概率.

核心场景：Polymarket UPDOWN 市场（如 "BTC 15分钟后是否高于参考价"）。

数学模型（几何布朗运动）:
    S_end = S_now × exp((μ - σ²/2)×τ + σ√τ×Z), Z ~ N(0,1)
    P(S_end > ref_px) = Φ(z)
    z = [ln(S_now / ref_px) + (μ - σ²/2)×τ] / (σ√τ)

对短窗口（15分钟）μ ≈ 0：
    z ≈ ln(S_now / ref_px) / (σ√τ)

与原有 BayesianPriceModel 的区别:
  BayesianPriceModel: 以市场价为先验 + OBI/动量信号做贝叶斯更新 → 适合一般市场
  FairValueModel: 用现货价 + 波动率 做精确定价 → 适合有现货锚定的 UPDOWN 市场

两者互补：FairValueModel 的输出可以作为 BayesianPriceModel 的更强先验。

移植自 mlmodelpoly/fair_model.py，去除了外部配置依赖。
"""

from __future__ import annotations

import logging
import math
from typing import Optional

LOG = logging.getLogger(__name__)

MIN_SIGMA = 0.0001
MIN_TAU_SEC = 1.0


def _standard_normal_cdf(x: float) -> float:
    """标准正态累积分布: Φ(x) = (1 + erf(x/√2)) / 2."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def compute_fair_updown(
    s_now: float,
    ref_px: float,
    sigma_15m: float,
    tau_sec: float,
    window_sec: float = 900.0,
    drift: float = 0.0,
) -> dict:
    """计算 UPDOWN 市场的公允概率.

    Args:
        s_now: 当前现货价格
        ref_px: 窗口开始时的参考价格
        sigma_15m: 15分钟波动率（log return 标准差，按 √15 缩放）
        tau_sec: 距窗口结束的剩余秒数
        window_sec: 总窗口长度（默认 900 = 15分钟）
        drift: 漂移项调整（默认 0）

    Returns:
        {"fair_up": float, "fair_down": float, "z_score": float, "inputs": dict}

    Edge cases:
        sigma 过小 → 退化为方向判断
        tau 过小 → 价格几乎不会变动
    """
    if s_now <= 0 or ref_px <= 0:
        return {"fair_up": 0.5, "fair_down": 0.5, "z_score": 0.0, "inputs": {"reason": "invalid_prices"}}

    if sigma_15m is None or sigma_15m < MIN_SIGMA:
        if s_now > ref_px:
            return {"fair_up": 0.6, "fair_down": 0.4, "z_score": None, "inputs": {"reason": "sigma_too_low"}}
        if s_now < ref_px:
            return {"fair_up": 0.4, "fair_down": 0.6, "z_score": None, "inputs": {"reason": "sigma_too_low"}}
        return {"fair_up": 0.5, "fair_down": 0.5, "z_score": 0.0, "inputs": {"reason": "sigma_too_low"}}

    if tau_sec < MIN_TAU_SEC:
        fair_up = 0.95 if s_now > ref_px else (0.05 if s_now < ref_px else 0.5)
        return {
            "fair_up": fair_up,
            "fair_down": 1.0 - fair_up,
            "z_score": None,
            "inputs": {"reason": "tau_too_small"},
        }

    tau_norm = tau_sec / window_sec
    log_ratio = math.log(s_now / ref_px)
    sigma_scaled = max(MIN_SIGMA, sigma_15m * math.sqrt(tau_norm))
    z_score = (log_ratio + drift * tau_norm) / sigma_scaled

    fair_up = _standard_normal_cdf(z_score)
    fair_down = 1.0 - fair_up

    return {
        "fair_up": round(fair_up, 4),
        "fair_down": round(fair_down, 4),
        "z_score": round(z_score, 3),
        "inputs": {
            "s_now": s_now,
            "ref_px": ref_px,
            "sigma_15m": sigma_15m,
            "tau_sec": tau_sec,
            "tau_norm": round(tau_norm, 3),
            "log_ratio": round(log_ratio, 6),
        },
    }


def compute_edge_bps(fair: float, market: float) -> Optional[float]:
    """计算模型价格与市场价格之间的 edge（基点）.

    正值 = 市场低估（fair > market），应买入。
    """
    if fair is None or market is None:
        return None
    return (fair - market) * 10_000


def compute_general_fair_value(
    market_price: float,
    *,
    obi_score: float = 0.0,
    momentum_score: float = 0.0,
    cross_market_deviation: float = 0.0,
    spot_fair: Optional[float] = None,
    obi_weight: float = 0.10,
    momentum_weight: float = 0.08,
    cross_weight: float = 0.15,
    spot_weight: float = 0.40,
) -> float:
    """通用公允价值估算：融合多信号源.

    当 spot_fair 可用时（UPDOWN 市场），它的权重最高；
    当不可用时退化为纯贝叶斯更新（与原 BayesianPriceModel 类似）。

    Args:
        market_price: 当前市场隐含概率 (0-1)
        obi_score: 订单簿不平衡 (-1 to 1)
        momentum_score: 价格动量
        cross_market_deviation: 跨市场偏差
        spot_fair: 来自 compute_fair_updown 的公允概率（如果有）
        obi_weight..spot_weight: 各信号权重

    Returns:
        综合公允概率 (0-1)
    """
    if market_price <= 0 or market_price >= 1:
        return market_price

    log_odds = math.log(market_price / (1.0 - market_price))

    log_odds += obi_weight * obi_score
    log_odds += momentum_weight * momentum_score
    log_odds += cross_weight * cross_market_deviation

    if spot_fair is not None and 0 < spot_fair < 1:
        spot_log_odds = math.log(spot_fair / (1.0 - spot_fair))
        market_log_odds = math.log(market_price / (1.0 - market_price))
        spot_signal = spot_log_odds - market_log_odds
        log_odds += spot_weight * spot_signal

    posterior = 1.0 / (1.0 + math.exp(-log_odds))
    return max(0.001, min(0.999, posterior))
