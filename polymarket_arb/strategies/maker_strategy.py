"""Maker 策略：用限价单提供流动性，赚取 spread + 流动性奖励.

为什么 Maker 策略是利润最大化的关键:

1. 费率优势:
   Taker fee = 2%, Maker fee = 0% (甚至有 rebate)
   如果你只做 taker，每笔套利先亏 2%
   如果你做 maker，费率为 0 → 相当于 edge 提升 2 个百分点

2. 流动性奖励:
   Polymarket 对在 δ (rewards_max_spread/2) 范围内挂单给予积分激励
   这是额外收入来源

3. 信息优势变现:
   当你有概率模型时，不是等 mispricing 出现再吃单
   而是主动在模型认为的 fair price 两侧挂单
   → 长期来看，你的挂单成交后有正期望

策略:
  fair_value = P(model)  # 你的模型估计的真实概率
  spread = 根据波动率和竞争环境动态计算
  bid_price = fair_value - spread/2
  ask_price = fair_value + spread/2

  约束:
  - bid/ask 都在激励带 [mid - δ, mid + δ] 内 → 赚激励
  - spread 至少覆盖逆向选择成本
  - 持仓偏斜时倾斜报价（inventory skew）

风险:
  - 逆向选择: 知情交易者总是在你报价的不利方向吃你
  - 库存风险: 一侧持续成交导致单边持仓
  - 做市不等于套利，有方向性风险
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from polymarket_arb.volatility_estimator import VolEstimator

LOG = logging.getLogger(__name__)


@dataclass
class QuoteUpdate:
    """做市报价."""

    token_id: str
    condition_id: str
    bid_price: Optional[float]
    ask_price: Optional[float]
    bid_size: float
    ask_size: float
    spread: float
    fair_value: float
    reason: str = ""


class DynamicSpreadCalculator:
    """动态 spread 计算.

    Spread = base_spread + volatility_spread + inventory_spread

    - base_spread: 最小利润要求（至少覆盖 tick）
    - volatility_spread: 由 VolEstimator 提供多尺度波动率
    - inventory_spread: 持仓偏斜时倾斜
    """

    def __init__(
        self,
        base_spread_ticks: float = 2.0,
        inventory_skew_factor: float = 0.5,
        vol_estimator: Optional[VolEstimator] = None,
    ):
        self._base_ticks = base_spread_ticks
        self._inv_skew = inventory_skew_factor
        self._vol_estimator = vol_estimator

    def set_vol_estimator(self, vol_estimator: VolEstimator) -> None:
        self._vol_estimator = vol_estimator

    def compute_spread(
        self,
        token_id: str,
        tick_size: float,
        inventory: float = 0.0,
        max_spread: Optional[float] = None,
    ) -> tuple[float, float]:
        """计算报价 spread.

        Returns:
            (bid_offset, ask_offset) — fair value 到 bid/ask 的距离
            bid_price = fair_value - bid_offset
            ask_price = fair_value + ask_offset
        """
        base = self._base_ticks * tick_size

        vol = self._get_volatility()
        vol_spread = vol * 2.0

        half = (base + vol_spread) / 2.0

        bid_offset = half + self._inv_skew * max(0, inventory) * tick_size
        ask_offset = half + self._inv_skew * max(0, -inventory) * tick_size

        if max_spread is not None:
            max_half = max_spread / 2.0
            bid_offset = min(bid_offset, max_half)
            ask_offset = min(ask_offset, max_half)

        bid_offset = max(bid_offset, tick_size)
        ask_offset = max(ask_offset, tick_size)

        return (bid_offset, ask_offset)

    def _get_volatility(self) -> float:
        """从 VolEstimator 获取波动率，如果不可用则返回保守默认值."""
        if self._vol_estimator is None:
            return 0.01

        snap = self._vol_estimator.snapshot()
        sigma = snap.get("sigma_blend_15m") or snap.get("sigma_fast_15m")
        if sigma is not None and sigma > 0:
            return sigma
        return 0.01


class MakerStrategy:
    """做市策略：在 fair value 两侧挂单.

    与纯套利不同，做市策略是连续运行的：
    - 根据模型 fair value 和 spread 计算 bid/ask
    - 持续维护挂单
    - 成交后更新库存和报价
    """

    def __init__(
        self,
        spread_calc: Optional[DynamicSpreadCalculator] = None,
        default_size: float = 10.0,
        max_inventory: float = 100.0,
        reward_delta: float = 0.0,
        flow_inventory_weight: float = 0.5,
    ):
        self._spread_calc = spread_calc or DynamicSpreadCalculator()
        self._default_size = default_size
        self._max_inventory = max_inventory
        self._reward_delta = reward_delta
        # How much aggregated taker flow counts as "synthetic inventory"
        # when steering quotes. 0.0 disables flow bias entirely (legacy
        # behaviour); 1.0 lets a fully one-sided market move the quote
        # the same amount as having a fully-loaded inventory book.
        self._flow_inventory_weight = max(0.0, float(flow_inventory_weight))
        self._inventory: dict[str, float] = {}

    def compute_quote(
        self,
        token_id: str,
        condition_id: str,
        fair_value: float,
        tick_size: float,
        *,
        mid_price: Optional[float] = None,
        reward_delta: Optional[float] = None,
        flow_bias_yes_share: Optional[float] = None,
        spread_multiplier: float = 1.0,
    ) -> Optional[QuoteUpdate]:
        """计算做市报价.

        Args:
            token_id: token 标识
            condition_id: 市场 condition
            fair_value: 模型估计的公允价值 (0-1)
            tick_size: 最小价格变动
            mid_price: 订单簿中间价（用于锚定）
            reward_delta: 激励半宽（挂在此范围内获得奖励）
            flow_bias_yes_share: share-weighted taker_yes_share over the
                active flow window (0.0–1.0; 0.5 = neutral). When ``>0.5``
                takers are mostly buying YES — i.e. they want to be
                served *out of my YES ask*. That's the side where I
                expect to keep getting filled, so I want:
                  • more ask depth (bigger ask_size), and
                  • a tighter bid so I don't accidentally absorb the
                    illiquid NO-buy side.
                Both effects fall out for free if I drive the existing
                inventory-skew math with a synthetic +LONG signal,
                because `compute_spread` already widens the bid on a
                LONG book and the size-skew block already grows the
                ask on positive inventory. ``None`` disables the
                signal entirely (legacy behaviour).
        """
        if fair_value <= 0 or fair_value >= 1:
            return None

        inv = self._inventory.get(token_id, 0.0)
        effective_inv = inv
        if (
            flow_bias_yes_share is not None
            and self._flow_inventory_weight > 0
            and self._max_inventory > 0
        ):
            # Map taker_yes_share [0,1] → bias_signed [-1,+1].
            # YES-lean (>0.5) feeds a positive synthetic inventory,
            # which steers spread + size exactly like "preparing to
            # serve the YES-buy side"; NO-lean does the mirror.
            bias_signed = (float(flow_bias_yes_share) - 0.5) * 2.0
            flow_synth_inv = bias_signed * self._max_inventory * self._flow_inventory_weight
            effective_inv = inv + flow_synth_inv
        bid_off, ask_off = self._spread_calc.compute_spread(
            token_id, tick_size, inventory=effective_inv
        )
        multiplier = max(1.0, float(spread_multiplier))
        bid_off *= multiplier
        ask_off *= multiplier

        raw_bid = fair_value - bid_off
        raw_ask = fair_value + ask_off

        bid_price = self._align_to_tick(raw_bid, tick_size, round_down=True)
        ask_price = self._align_to_tick(raw_ask, tick_size, round_down=False)

        if bid_price <= 0 or bid_price >= 1:
            bid_price = None
        if ask_price <= 0 or ask_price >= 1:
            ask_price = None

        if bid_price is not None and ask_price is not None and bid_price >= ask_price:
            ask_price = bid_price + tick_size

        delta = reward_delta or self._reward_delta
        if delta > 0 and mid_price is not None:
            reward_lo = mid_price - delta
            reward_hi = mid_price + delta
            if bid_price is not None and bid_price < reward_lo:
                bid_price = self._align_to_tick(reward_lo, tick_size, round_down=False)
            if ask_price is not None and ask_price > reward_hi:
                ask_price = self._align_to_tick(reward_hi, tick_size, round_down=True)

        bid_sz = self._default_size
        ask_sz = self._default_size
        # Size skew also uses `effective_inv` so flow bias contributes
        # to "post bigger on the side we want filled, smaller on the
        # side that's being adversely selected".
        if effective_inv > 0:
            ask_sz = min(self._default_size * 1.5, self._default_size + effective_inv * 0.5)
        elif effective_inv < 0:
            bid_sz = min(self._default_size * 1.5, self._default_size + abs(effective_inv) * 0.5)

        # The hard inventory cap still uses *real* inventory only — we
        # never want flow alone to force the bot to stop posting on one
        # side, only real on-book positions earn that.
        if abs(inv) >= self._max_inventory:
            if inv > 0:
                bid_price = None
                bid_sz = 0
            else:
                ask_price = None
                ask_sz = 0

        spread = (ask_price - bid_price) if (bid_price is not None and ask_price is not None) else 0

        return QuoteUpdate(
            token_id=token_id,
            condition_id=condition_id,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_sz,
            ask_size=ask_sz,
            spread=spread,
            fair_value=fair_value,
        )

    def update_inventory(self, token_id: str, side: str, size: float) -> None:
        """成交后更新库存."""
        current = self._inventory.get(token_id, 0.0)
        if side.upper() == "BUY":
            self._inventory[token_id] = current + size
        else:
            self._inventory[token_id] = current - size

    def get_inventory(self, token_id: str) -> float:
        return self._inventory.get(token_id, 0.0)

    @staticmethod
    def _align_to_tick(price: float, tick: float, round_down: bool = True) -> float:
        if tick <= 0:
            return price
        if round_down:
            return math.floor(price / tick) * tick
        else:
            return math.ceil(price / tick) * tick
