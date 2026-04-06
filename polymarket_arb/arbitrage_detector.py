"""核心套利检测引擎：扫描二元市场和多结果市场，识别可盈利的套利机会.

套利类型：
1. 二元市场套利 (Binary Arb):
   - 市场有 Yes/No 两个 token
   - 如果 best_ask(Yes) + best_ask(No) < 1.0，则买入双方锁定利润
   - 利润 = 1.0 - (ask_yes + ask_no) - fee

2. 多结果套利 (Multi-Outcome Arb):
   - 一个事件下有 N 个互斥结果（如选举候选人）
   - 如果 sum(best_ask_i for i in outcomes) < 1.0，则买入所有结果
   - 利润 = 1.0 - sum(asks) - fee
   - neg_risk 市场需要特殊处理

两种策略的关键约束:
- 需要足够的订单簿深度（不只看 best ask，还要看能否实际成交）
- 扣除 taker fee（通常 2%）后仍有正利润
- 滑点保护：用 VWAP 而不是 best ask 估算实际成本
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    EventInfo,
    FeeStructure,
    MarketInfo,
    OrderBookSnapshot,
    OrderSide,
)
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer

LOG = logging.getLogger(__name__)

PAYOUT_PER_SHARE = 1.0


class ArbitrageDetector:
    """检测 Polymarket 上的套利机会."""

    def __init__(self, config: ArbConfig, ob_analyzer: OrderBookAnalyzer):
        self._config = config
        self._ob = ob_analyzer
        self._fees = FeeStructure()

    def scan_binary_market(self, market: MarketInfo) -> Optional[ArbOpportunity]:
        """检查二元市场（Yes/No）是否存在套利.

        Polymarket 二元市场有且仅有 2 个 token。
        如果两个 token 的 best ask 之和 < 1.0 - fee，则存在套利。
        """
        if len(market.tokens) != 2:
            return None
        if market.closed or not market.active:
            return None

        token_yes = market.tokens[0]
        token_no = market.tokens[1]

        snap_yes = self._ob.get_snapshot(token_yes.token_id)
        snap_no = self._ob.get_snapshot(token_no.token_id)

        if snap_yes is None or snap_no is None:
            return None
        if snap_yes.best_ask is None or snap_no.best_ask is None:
            return None

        ask_yes = snap_yes.best_ask
        ask_no = snap_no.best_ask

        total_cost = ask_yes + ask_no
        gross_edge = PAYOUT_PER_SHARE - total_cost
        fee = self._fees.estimate_fee(total_cost, num_legs=2)
        net_edge = gross_edge - fee

        if net_edge <= 0:
            return None

        edge_pct = (net_edge / total_cost) * 100 if total_cost > 0 else 0

        if net_edge < self._config.min_edge_usd:
            return None
        if edge_pct < self._config.min_edge_pct:
            return None

        max_size = min(
            snap_yes.best_ask_size,
            snap_no.best_ask_size,
            self._config.max_order_size_usdc / total_cost if total_cost > 0 else 0,
        )

        legs = [
            ArbLeg(
                token_id=token_yes.token_id,
                condition_id=market.condition_id,
                outcome=token_yes.outcome or "Yes",
                side=OrderSide.BUY,
                price=ask_yes,
                size=max_size,
                available_size=snap_yes.best_ask_size,
            ),
            ArbLeg(
                token_id=token_no.token_id,
                condition_id=market.condition_id,
                outcome=token_no.outcome or "No",
                side=OrderSide.BUY,
                price=ask_no,
                size=max_size,
                available_size=snap_no.best_ask_size,
            ),
        ]

        confidence = self._estimate_confidence(net_edge, edge_pct, max_size, total_cost)

        return ArbOpportunity(
            arb_type=ArbType.BINARY,
            event_id=market.event_id,
            event_title=market.question,
            markets=[market],
            total_cost=total_cost,
            guaranteed_payout=PAYOUT_PER_SHARE,
            gross_edge=gross_edge,
            net_edge=net_edge,
            edge_pct=edge_pct,
            legs=legs,
            max_executable_size=max_size,
            confidence=confidence,
        )

    def scan_multi_outcome_event(self, event: EventInfo) -> Optional[ArbOpportunity]:
        """检查多结果事件是否存在套利.

        一个事件下有多个互斥市场，每个市场代表一个结果。
        如果所有结果的 best ask 之和 < 1.0 - fee，则买入所有结果锁定利润。

        neg_risk 市场的特殊处理:
        - neg_risk=True 时，Polymarket 使用补集定价
        - 买入 outcome_i 实际等价于卖出 (1 - outcome_i)
        - 需要用 No token 的 best_bid 来计算等效 ask
        """
        if len(event.markets) < 2:
            return None

        active_markets = [m for m in event.markets if m.active and not m.closed]
        if len(active_markets) < 2:
            return None

        is_neg_risk = any(m.neg_risk for m in active_markets)

        legs: list[ArbLeg] = []
        total_ask_cost = 0.0
        min_available = float("inf")
        all_valid = True

        for market in active_markets:
            if not market.tokens:
                all_valid = False
                break

            if is_neg_risk:
                arb_leg = self._get_neg_risk_leg(market)
            else:
                arb_leg = self._get_standard_leg(market)

            if arb_leg is None:
                all_valid = False
                break

            legs.append(arb_leg)
            total_ask_cost += arb_leg.price
            min_available = min(min_available, arb_leg.available_size)

        if not all_valid or not legs:
            return None

        gross_edge = PAYOUT_PER_SHARE - total_ask_cost
        fee = self._fees.estimate_fee(total_ask_cost, num_legs=len(legs))
        net_edge = gross_edge - fee

        if net_edge <= 0:
            return None

        edge_pct = (net_edge / total_ask_cost) * 100 if total_ask_cost > 0 else 0

        if net_edge < self._config.min_edge_usd:
            return None
        if edge_pct < self._config.min_edge_pct:
            return None

        max_size = min(
            min_available,
            self._config.max_order_size_usdc / total_ask_cost if total_ask_cost > 0 else 0,
        )
        for leg in legs:
            leg.size = max_size

        confidence = self._estimate_confidence(net_edge, edge_pct, max_size, total_ask_cost)

        return ArbOpportunity(
            arb_type=ArbType.MULTI_OUTCOME,
            event_id=event.event_id,
            event_title=event.title,
            markets=active_markets,
            total_cost=total_ask_cost,
            guaranteed_payout=PAYOUT_PER_SHARE,
            gross_edge=gross_edge,
            net_edge=net_edge,
            edge_pct=edge_pct,
            legs=legs,
            max_executable_size=max_size,
            confidence=confidence,
        )

    def _get_standard_leg(self, market: MarketInfo) -> Optional[ArbLeg]:
        """标准市场：买入 Yes token 的 best ask."""
        yes_token = market.tokens[0] if market.tokens else None
        if yes_token is None:
            return None

        snap = self._ob.get_snapshot(yes_token.token_id)
        if snap is None or snap.best_ask is None:
            return None

        return ArbLeg(
            token_id=yes_token.token_id,
            condition_id=market.condition_id,
            outcome=yes_token.outcome or market.outcomes[0] if market.outcomes else "Yes",
            side=OrderSide.BUY,
            price=snap.best_ask,
            size=0,
            available_size=snap.best_ask_size,
        )

    def _get_neg_risk_leg(self, market: MarketInfo) -> Optional[ArbLeg]:
        """neg_risk 市场：通过 No token 的 bid 来等效获得 Yes 头寸.

        在 neg_risk 市场中:
        - 买 Yes @ ask_yes 直接获得
        - 或者等效地：卖 No @ bid_no，成本 = 1 - bid_no
        - 取两者中较优的
        """
        if len(market.tokens) < 2:
            return None

        yes_token = market.tokens[0]
        no_token = market.tokens[1]

        snap_yes = self._ob.get_snapshot(yes_token.token_id)
        snap_no = self._ob.get_snapshot(no_token.token_id)

        effective_ask_via_yes = snap_yes.best_ask if (snap_yes and snap_yes.best_ask) else None
        effective_ask_via_no = (1.0 - snap_no.best_bid) if (snap_no and snap_no.best_bid) else None

        if effective_ask_via_yes is None and effective_ask_via_no is None:
            return None

        if effective_ask_via_yes is not None and effective_ask_via_no is not None:
            if effective_ask_via_yes <= effective_ask_via_no:
                return ArbLeg(
                    token_id=yes_token.token_id,
                    condition_id=market.condition_id,
                    outcome=yes_token.outcome or "Yes",
                    side=OrderSide.BUY,
                    price=effective_ask_via_yes,
                    size=0,
                    available_size=snap_yes.best_ask_size if snap_yes else 0,
                )
            else:
                return ArbLeg(
                    token_id=no_token.token_id,
                    condition_id=market.condition_id,
                    outcome=yes_token.outcome or "Yes",
                    side=OrderSide.SELL,
                    price=effective_ask_via_no,
                    size=0,
                    available_size=snap_no.best_bid_size if snap_no else 0,
                )

        if effective_ask_via_yes is not None:
            return ArbLeg(
                token_id=yes_token.token_id,
                condition_id=market.condition_id,
                outcome=yes_token.outcome or "Yes",
                side=OrderSide.BUY,
                price=effective_ask_via_yes,
                size=0,
                available_size=snap_yes.best_ask_size if snap_yes else 0,
            )

        assert effective_ask_via_no is not None
        return ArbLeg(
            token_id=no_token.token_id,
            condition_id=market.condition_id,
            outcome=yes_token.outcome or "Yes",
            side=OrderSide.SELL,
            price=effective_ask_via_no,
            size=0,
            available_size=snap_no.best_bid_size if snap_no else 0,
        )

    def verify_opportunity_with_depth(
        self, opp: ArbOpportunity, target_size: float
    ) -> Optional[ArbOpportunity]:
        """用 VWAP 重新验证套利机会（考虑滑点）.

        初始扫描用 best ask 快速筛选，此方法用实际可执行深度重新计算。
        """
        total_vwap_cost = 0.0
        verified_legs: list[ArbLeg] = []
        actual_min_size = float("inf")

        for leg in opp.legs:
            result = self._ob.get_executable_ask_price(leg.token_id, target_size)
            if result is None:
                LOG.debug("深度验证失败: token=%s… 无足够深度", leg.token_id[:20])
                return None

            vwap, filled = result
            total_vwap_cost += vwap
            actual_min_size = min(actual_min_size, filled)

            verified_legs.append(ArbLeg(
                token_id=leg.token_id,
                condition_id=leg.condition_id,
                outcome=leg.outcome,
                side=leg.side,
                price=vwap,
                size=min(target_size, filled),
                available_size=filled,
            ))

        gross_edge = PAYOUT_PER_SHARE - total_vwap_cost
        fee = self._fees.estimate_fee(total_vwap_cost, num_legs=len(verified_legs))
        net_edge = gross_edge - fee

        if net_edge <= 0:
            return None

        edge_pct = (net_edge / total_vwap_cost) * 100 if total_vwap_cost > 0 else 0

        return ArbOpportunity(
            arb_type=opp.arb_type,
            event_id=opp.event_id,
            event_title=opp.event_title,
            markets=opp.markets,
            total_cost=total_vwap_cost,
            guaranteed_payout=PAYOUT_PER_SHARE,
            gross_edge=gross_edge,
            net_edge=net_edge,
            edge_pct=edge_pct,
            legs=verified_legs,
            max_executable_size=actual_min_size,
            confidence=self._estimate_confidence(net_edge, edge_pct, actual_min_size, total_vwap_cost),
        )

    def _estimate_confidence(
        self, net_edge: float, edge_pct: float, max_size: float, total_cost: float
    ) -> float:
        """估算套利机会的置信度 (0-1).

        考虑因素：
        - 利润率越高越可信
        - 可执行深度越大越可信
        - 总成本越接近 1.0 越不可信（可能是定价合理只是 spread 大）
        """
        score = 0.0

        if edge_pct >= 5.0:
            score += 0.4
        elif edge_pct >= 2.0:
            score += 0.3
        elif edge_pct >= 1.0:
            score += 0.2
        else:
            score += 0.1

        if max_size >= 100:
            score += 0.3
        elif max_size >= 20:
            score += 0.2
        elif max_size >= 5:
            score += 0.1

        if total_cost < 0.9:
            score += 0.3
        elif total_cost < 0.95:
            score += 0.2
        elif total_cost < 0.98:
            score += 0.1

        return min(1.0, score)


def format_arb_opportunity_zh(opp: ArbOpportunity) -> str:
    """将套利机会格式化为中文可读文本."""
    type_label = "二元套利" if opp.arb_type == ArbType.BINARY else "多结果套利"
    lines = [
        f"🔔 发现{type_label}机会",
        f"事件: {opp.event_title}",
        f"类型: {type_label} ({len(opp.legs)} 条腿)",
        f"总成本: ${opp.total_cost:.4f}",
        f"保证回收: ${opp.guaranteed_payout:.4f}",
        f"毛利: ${opp.gross_edge:.4f}",
        f"净利: ${opp.net_edge:.4f} ({opp.edge_pct:.2f}%)",
        f"最大可执行量: {opp.max_executable_size:.2f} 份",
        f"置信度: {opp.confidence:.0%}",
        "",
        "各腿详情:",
    ]
    for i, leg in enumerate(opp.legs, 1):
        lines.append(
            f"  {i}. {leg.outcome} | {leg.side.value} @ ${leg.price:.4f} | "
            f"深度={leg.available_size:.1f}"
        )
    return "\n".join(lines)
