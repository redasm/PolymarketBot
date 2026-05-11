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
import re
import statistics
from typing import Any, Optional

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    EventInfo,
    FeeStructure,
    MarketInfo,
    OrderSide,
)
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer

LOG = logging.getLogger(__name__)

PAYOUT_PER_SHARE = 1.0


def _find_token_by_outcome(market: MarketInfo, outcome: str) -> Any | None:
    expected = outcome.strip().lower()
    return next(
        (
            token
            for token in market.tokens
            if str(token.outcome or "").strip().lower() == expected
        ),
        None,
    )


class ArbitrageDetector:
    """检测 Polymarket 上的套利机会."""

    def __init__(self, config: ArbConfig, ob_analyzer: OrderBookAnalyzer):
        self._config = config
        self._ob = ob_analyzer
        self._fees = FeeStructure(taker_fee_rate=config.polymarket_taker_fee_rate)

    def scan_binary_market(self, market: MarketInfo) -> Optional[ArbOpportunity]:
        """检查二元市场（Yes/No）是否存在套利."""
        if len(market.tokens) != 2:
            return None
        if market.closed or not market.active:
            return None

        token_yes = _find_token_by_outcome(market, "yes")
        token_no = _find_token_by_outcome(market, "no")
        if token_yes is None or token_no is None or token_yes.token_id == token_no.token_id:
            return None

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
        fee = FeeStructure.for_market(
            self._config.polymarket_taker_fee_rate,
            market,
        ).estimate_leg_fees([ask_yes, ask_no])
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
                execution_price=ask_yes,
                economic_cost=ask_yes,
                tick_size=float(getattr(snap_yes, "tick_size", 0.01) or 0.01),
            ),
            ArbLeg(
                token_id=token_no.token_id,
                condition_id=market.condition_id,
                outcome=token_no.outcome or "No",
                side=OrderSide.BUY,
                price=ask_no,
                size=max_size,
                available_size=snap_no.best_ask_size,
                execution_price=ask_no,
                economic_cost=ask_no,
                tick_size=float(getattr(snap_no, "tick_size", 0.01) or 0.01),
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
        """检查多结果事件是否存在套利."""
        if len(event.markets) < 2:
            return None

        active_markets = [m for m in event.markets if m.active and not m.closed]
        if len(active_markets) < 2:
            return None
        if self._is_monotonic_time_ladder(active_markets):
            LOG.debug("跳过非互斥时间梯事件: %s", event.title)
            return None
        if len(active_markets) > self._config.max_multi_outcome_legs:
            LOG.debug(
                "跳过超多腿多结果事件: %s, markets=%d > limit=%d",
                event.title,
                len(active_markets),
                self._config.max_multi_outcome_legs,
            )
            return None

        is_neg_risk = any(m.neg_risk for m in active_markets)
        legs: list[ArbLeg] = []
        total_ask_cost = 0.0
        min_available = float("inf")

        for market in active_markets:
            if not market.tokens:
                return None

            arb_leg = self._get_neg_risk_leg(market) if is_neg_risk else self._get_standard_leg(market)
            if arb_leg is None:
                return None

            legs.append(arb_leg)
            total_ask_cost += arb_leg.economic_cost if arb_leg.economic_cost is not None else arb_leg.price
            min_available = min(min_available, arb_leg.available_size)

        if not legs:
            return None

        # 过滤低流动性"假套利"：多结果市场中大量腿价格极低（<5%），
        # 这类市场深度几乎为零，实际执行时无法成交，是假阳性信号。
        leg_prices = [leg.economic_cost for leg in legs if leg.economic_cost is not None and leg.available_size > 0]
        if leg_prices:
            median_price = statistics.median(leg_prices)
            if median_price < self._config.t0_min_multi_outcome_median_leg_price:
                LOG.debug(
                    "跳过低流动性多结果套利: %s, median_leg_price=%.4f < %.4f",
                    event.title,
                    median_price,
                    self._config.t0_min_multi_outcome_median_leg_price,
                )
                return None

        gross_edge = PAYOUT_PER_SHARE - total_ask_cost
        fee = sum(
            FeeStructure.for_market(
                self._config.polymarket_taker_fee_rate,
                market,
            ).estimate_price_fee(
                float(leg.execution_price if leg.execution_price is not None else leg.price)
            )
            for market, leg in zip(active_markets, legs)
        )
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
        yes_token = _find_token_by_outcome(market, "yes") if market.tokens else None
        if yes_token is None and len(market.tokens) == 1:
            yes_token = market.tokens[0]
        if yes_token is None:
            return None

        snap = self._ob.get_snapshot(yes_token.token_id)
        if snap is None or snap.best_ask is None:
            return None

        return ArbLeg(
            token_id=yes_token.token_id,
            condition_id=market.condition_id,
            outcome=yes_token.outcome or (market.outcomes[0] if market.outcomes else "Yes"),
            side=OrderSide.BUY,
            price=snap.best_ask,
            size=0,
            available_size=snap.best_ask_size,
            execution_price=snap.best_ask,
            economic_cost=snap.best_ask,
            tick_size=float(getattr(snap, "tick_size", 0.01) or 0.01),
        )

    def _get_neg_risk_leg(self, market: MarketInfo) -> Optional[ArbLeg]:
        """neg_risk 市场：通过 No token 的 bid 来等效获得 Yes 头寸."""
        if len(market.tokens) < 2:
            return None

        yes_token = _find_token_by_outcome(market, "yes")
        no_token = _find_token_by_outcome(market, "no")
        if yes_token is None or no_token is None or yes_token.token_id == no_token.token_id:
            return None

        snap_yes = self._ob.get_snapshot(yes_token.token_id)
        snap_no = self._ob.get_snapshot(no_token.token_id)

        effective_ask_via_yes = snap_yes.best_ask if (snap_yes and snap_yes.best_ask is not None) else None
        no_bid = snap_no.best_bid if (snap_no and snap_no.best_bid is not None) else None
        effective_ask_via_no = (1.0 - no_bid) if no_bid is not None else None

        if effective_ask_via_yes is None and effective_ask_via_no is None:
            return None

        if effective_ask_via_yes is not None and (
            effective_ask_via_no is None or effective_ask_via_yes <= effective_ask_via_no
        ):
            return ArbLeg(
                token_id=yes_token.token_id,
                condition_id=market.condition_id,
                outcome=yes_token.outcome or "Yes",
                side=OrderSide.BUY,
                price=effective_ask_via_yes,
                size=0,
                available_size=snap_yes.best_ask_size if snap_yes else 0,
                execution_price=effective_ask_via_yes,
                economic_cost=effective_ask_via_yes,
                tick_size=float(getattr(snap_yes, "tick_size", 0.01) or 0.01) if snap_yes else 0.01,
            )

        if effective_ask_via_no is None or no_bid is None:
            LOG.warning(
                "neg_risk 腿构建失败: market=%s yes_token=%s no_token=%s effective_ask_via_no=%s no_bid=%s",
                market.condition_id[:12],
                yes_token.token_id[:16],
                no_token.token_id[:16],
                effective_ask_via_no,
                no_bid,
            )
            return None
        return ArbLeg(
            token_id=no_token.token_id,
            condition_id=market.condition_id,
            outcome=yes_token.outcome or "Yes",
            side=OrderSide.SELL,
            price=effective_ask_via_no,
            size=0,
            available_size=snap_no.best_bid_size if snap_no else 0,
            execution_price=no_bid,
            economic_cost=effective_ask_via_no,
            tick_size=float(getattr(snap_no, "tick_size", 0.01) or 0.01) if snap_no else 0.01,
        )

    def verify_opportunity_with_depth(
        self, opp: ArbOpportunity, target_size: float
    ) -> Optional[ArbOpportunity]:
        """用 VWAP 重新验证套利机会（考虑滑点）."""
        total_vwap_cost = 0.0
        verified_legs: list[ArbLeg] = []
        actual_min_size = float("inf")

        for leg in opp.legs:
            if leg.side == OrderSide.BUY:
                result = self._ob.get_executable_ask_price(leg.token_id, target_size)
            else:
                result = self._ob.get_executable_bid_price(leg.token_id, target_size)
            if result is None:
                LOG.debug("深度验证失败: token=%s… 无足够深度", leg.token_id[:20])
                return None

            vwap, filled = result
            economic_cost = vwap if leg.side == OrderSide.BUY else (1.0 - vwap)
            total_vwap_cost += economic_cost
            actual_min_size = min(actual_min_size, filled)

            verified_legs.append(
                ArbLeg(
                    token_id=leg.token_id,
                    condition_id=leg.condition_id,
                    outcome=leg.outcome,
                    side=leg.side,
                    price=economic_cost,
                    size=min(target_size, filled),
                    available_size=filled,
                    execution_price=vwap,
                    economic_cost=economic_cost,
                    tick_size=getattr(leg, "tick_size", 0.01),
                )
            )

        gross_edge = PAYOUT_PER_SHARE - total_vwap_cost
        markets_by_condition = {market.condition_id: market for market in opp.markets}
        fee = sum(
            FeeStructure.for_market(
                self._config.polymarket_taker_fee_rate,
                markets_by_condition.get(leg.condition_id),
            ).estimate_price_fee(
                float(leg.execution_price if leg.execution_price is not None else leg.price)
            )
            for leg in verified_legs
        )
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
        """估算套利机会的置信度 (0-1)."""
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

    def _is_monotonic_time_ladder(self, markets: list[MarketInfo]) -> bool:
        if len(markets) < 2:
            return False
        if any(m.neg_risk for m in markets):
            return False
        if any(len(m.tokens) != 2 for m in markets):
            return False

        stems: set[str] = set()
        for market in markets:
            outcomes = {(token.outcome or "").strip().lower() for token in market.tokens}
            if outcomes != {"yes", "no"}:
                return False
            stem = self._extract_deadline_stem(market.question)
            if not stem:
                return False
            stems.add(stem)
        return len(stems) == 1

    def _extract_deadline_stem(self, question: str) -> str:
        normalized = re.sub(r"\s+", " ", (question or "")).strip().rstrip("?").strip()
        lowered = normalized.lower()
        if " by " not in lowered:
            return ""
        stem = re.sub(r"\s+by\s+.+$", "", lowered).strip(" .!?")
        return stem


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
        price_text = f"${leg.execution_price:.4f}"
        if leg.side == OrderSide.SELL and leg.economic_cost is not None:
            price_text += f" (econ=${leg.economic_cost:.4f})"
        lines.append(
            f"  {i}. {leg.outcome} | {leg.side.value} @ {price_text} | "
            f"深度={leg.available_size:.1f}"
        )
    return "\n".join(lines)
