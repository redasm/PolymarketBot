"""Kelly Criterion 最优仓位计算.

核心思想:
  固定比例下注（如每次下 $10）不是最优的。
  Kelly 公式给出了 **长期复合增长率最大化** 的下注比例。

经典 Kelly 公式（二元赌注）:
  f* = (p * b - q) / b
  其中:
    p = 胜率（你的模型估计的真实概率）
    q = 1 - p（败率）
    b = 赔率（net odds, 赢时净赚多少倍）

对于 Polymarket 套利:
  - 结构性套利: p ≈ 1.0（几乎确定赢），f* ≈ 全仓
    但考虑执行风险（滑点、部分成交、网络延迟），实际 p < 1.0
  - 统计套利: p 来自贝叶斯模型，不确定性更高

实践中我们使用 **Fractional Kelly** (half-Kelly 或 quarter-Kelly):
  f_actual = fraction * f*
  fraction 通常取 0.25-0.5，原因:
    1. 模型概率 p 本身有不确定性
    2. Kelly 假设无限次重复，单次破产风险不为零
    3. 减半后方差降低 75%，收益只降低 25%（非常划算的交换）

高级: 多腿 Kelly（组合优化）
  当同时有多个套利机会时，需要解组合 Kelly:
  max Σ_i log(1 + f_i * edge_i)  subject to  Σ f_i <= 1
  用凸优化或贪心近似。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger(__name__)


@dataclass
class KellyResult:
    """Kelly 计算结果."""

    raw_fraction: float  # 原始 Kelly 比例 (0-1)
    adjusted_fraction: float  # 调整后的比例 (乘以 kelly_fraction)
    optimal_size_usdc: float  # 推荐下注金额
    expected_growth_rate: float  # 预期对数增长率
    edge: float  # edge = p*b - q
    bankroll: float  # 当前资金
    warning_reason: str = ""


def kelly_binary(
    win_prob: float,
    net_odds: float,
    bankroll: float,
    *,
    kelly_fraction: float = 0.25,
    max_bet_pct: float = 0.10,
    min_bet_usdc: float = 1.0,
) -> KellyResult:
    """经典二元 Kelly 公式.

    Args:
        win_prob: 胜率 (0-1)。对结构性套利约 0.95-0.99，对统计套利看模型。
        net_odds: 净赔率。下注 $1 赢时净赚 $b。
            例: 买入总成本 $0.97, 回收 $1.00 → b = 0.03/0.97 ≈ 0.031
        bankroll: 当前可用资金 (USDC)
        kelly_fraction: Kelly 缩放因子。0.25 = quarter-Kelly（保守推荐）。
        max_bet_pct: 单笔最大占资金比例（安全上限）
        min_bet_usdc: 最小下注额

    Returns:
        KellyResult 包含最优下注额和相关统计
    """
    if win_prob <= 0 or win_prob >= 1 or net_odds <= 0 or bankroll <= 0:
        return KellyResult(0, 0, 0, 0, 0, bankroll, warning_reason="invalid_inputs")

    p = win_prob
    q = 1.0 - p
    b = net_odds

    edge = p * b - q
    if edge <= 0:
        return KellyResult(0, 0, 0, 0, edge, bankroll, warning_reason="non_positive_edge")

    raw_f = edge / b  # f* = (pb - q) / b
    raw_f = max(0.0, min(1.0, raw_f))

    adjusted_f = raw_f * kelly_fraction
    adjusted_f = min(adjusted_f, max_bet_pct)

    optimal_usdc = adjusted_f * bankroll
    effective_fraction = adjusted_f
    warning_reason = ""
    if optimal_usdc < min_bet_usdc:
        LOG.info(
            "Kelly 建议仓位低于最小下注额，已抑制下单: bankroll=%.2f adjusted_f=%.4f size=%.4f min_bet=%.2f",
            bankroll,
            adjusted_f,
            optimal_usdc,
            min_bet_usdc,
        )
        optimal_usdc = 0.0
        effective_fraction = 0.0
        warning_reason = "below_min_bet"

    if effective_fraction >= 1.0:
        LOG.warning(
            "Kelly 调整后仓位达到满仓，expected_growth_rate 退化为 -inf: win_prob=%.4f net_odds=%.4f",
            win_prob,
            net_odds,
        )
        growth_rate = float("-inf")
        warning_reason = warning_reason or "full_bankroll_risk"
    elif effective_fraction <= 0:
        growth_rate = 0.0
    else:
        growth_rate = p * math.log(1 + effective_fraction * b) + q * math.log(1 - effective_fraction)

    return KellyResult(
        raw_fraction=raw_f,
        adjusted_fraction=adjusted_f,
        optimal_size_usdc=optimal_usdc,
        expected_growth_rate=growth_rate,
        edge=edge,
        bankroll=bankroll,
        warning_reason=warning_reason,
    )


def kelly_for_structural_arb(
    net_edge_per_share: float,
    total_cost_per_share: float,
    bankroll: float,
    *,
    execution_success_prob: float = 0.95,
    kelly_fraction: float = 0.25,
    max_bet_pct: float = 0.10,
) -> KellyResult:
    """结构性套利专用的 Kelly 计算.

    结构性套利理论上 p=1.0（必赢），但实际执行中存在风险:
    - 订单簿变化导致滑点
    - 部分腿执行失败
    - 网络延迟导致价格移动
    - 市场关闭/结算异常

    所以 win_prob = execution_success_prob < 1.0
    """
    if total_cost_per_share <= 0 or net_edge_per_share <= 0:
        return KellyResult(0, 0, 0, 0, 0, bankroll, warning_reason="invalid_inputs")

    net_odds = net_edge_per_share / total_cost_per_share

    loss_on_fail = total_cost_per_share

    adjusted_odds = net_edge_per_share / loss_on_fail if loss_on_fail > 0 else net_odds

    return kelly_binary(
        win_prob=execution_success_prob,
        net_odds=adjusted_odds,
        bankroll=bankroll,
        kelly_fraction=kelly_fraction,
        max_bet_pct=max_bet_pct,
    )


def kelly_for_statistical_arb(
    model_prob: float,
    market_price: float,
    bankroll: float,
    *,
    model_uncertainty: float = 0.1,
    kelly_fraction: float = 0.25,
    max_bet_pct: float = 0.05,
) -> Optional[KellyResult]:
    """统计套利（概率模型驱动）的 Kelly 计算.

    Args:
        model_prob: 模型估计的真实概率
        market_price: 当前市场价格 (= 隐含概率)
        bankroll: 可用资金
        model_uncertainty: 模型不确定性（0-1），用于缩放 kelly_fraction
        kelly_fraction: 基础 Kelly 缩放
        max_bet_pct: 最大单笔比例

    Returns:
        KellyResult 或 None（无 edge 时）
    """
    if market_price <= 0 or market_price >= 1:
        return None

    if model_prob > market_price:
        win_prob = model_prob
        net_odds = (1.0 - market_price) / market_price
    elif model_prob < market_price:
        win_prob = 1.0 - model_prob
        net_odds = market_price / (1.0 - market_price)
    else:
        return None

    uncertainty_adjusted_fraction = kelly_fraction * (1.0 - model_uncertainty)

    result = kelly_binary(
        win_prob=win_prob,
        net_odds=net_odds,
        bankroll=bankroll,
        kelly_fraction=uncertainty_adjusted_fraction,
        max_bet_pct=max_bet_pct,
    )

    if result.edge <= 0:
        return None

    return result


def kelly_multi_opportunity(
    opportunities: list[tuple[float, float, float]],
    bankroll: float,
    kelly_fraction: float = 0.25,
) -> list[float]:
    """多个同时机会的 Kelly 分配（贪心近似）.

    Args:
        opportunities: [(win_prob, net_odds, max_size_usdc), ...]
        bankroll: 总资金
        kelly_fraction: 缩放因子

    Returns:
        每个机会的推荐下注额列表
    """
    scored = []
    for i, (p, b, max_sz) in enumerate(opportunities):
        edge = p * b - (1 - p)
        if edge > 0:
            raw_f = edge / b
            adjusted_f = max(0.0, min(1.0, raw_f * kelly_fraction))
            growth = p * math.log(1 + adjusted_f * b) + (1 - p) * math.log(1 - adjusted_f)
            scored.append((growth, i, adjusted_f, max_sz))

    scored.sort(reverse=True)

    allocations = [0.0] * len(opportunities)
    remaining = bankroll

    for _, idx, adjusted_f, max_sz in scored:
        alloc = min(adjusted_f * remaining, max_sz, remaining * 0.2)
        if alloc < 1.0:
            continue
        allocations[idx] = alloc
        remaining -= alloc
        if remaining <= 0:
            break

    return allocations
