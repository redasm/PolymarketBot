"""Logical relationship signals for related Polymarket markets."""

from __future__ import annotations

from dataclasses import dataclass, field

from polymarket_arb.models import MarketInfo
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier


@dataclass(frozen=True)
class RelationRule:
    subject_market_id: str
    bound_market_id: str
    relation_type: str
    min_violation_bps: float = 200.0
    max_size_usdc: float | None = None
    tags: tuple[str, ...] = ()


class LogicalConstraintDetector:
    """Detect deterministic probability-order violations.

    `subject_lte_bound` means P(subject) should be less than or equal to
    P(bound). When subject is richer than its bound by enough bps, the bound is
    the cleaner buy candidate for a long-only Polymarket bot.
    """

    def __init__(
        self,
        rules: list[RelationRule] | None = None,
        *,
        default_size_usdc: float = 10.0,
        tier: StrategyTier = StrategyTier.STATISTICAL_ARB,
    ) -> None:
        self._rules = list(rules or [])
        self._default_size_usdc = float(default_size_usdc)
        self._tier = tier

    def detect(
        self,
        *,
        markets: dict[str, MarketInfo],
        yes_prices: dict[str, float],
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        for rule in self._rules:
            subject_price = yes_prices.get(rule.subject_market_id)
            bound_price = yes_prices.get(rule.bound_market_id)
            if subject_price is None or bound_price is None:
                continue
            if rule.relation_type != "subject_lte_bound":
                continue

            violation_bps = (float(subject_price) - float(bound_price)) * 10_000.0
            if violation_bps < rule.min_violation_bps:
                continue

            market = markets.get(rule.bound_market_id)
            description = (
                f"logical constraint violation: {rule.subject_market_id} "
                f"priced above {rule.bound_market_id}"
            )
            signals.append(
                StrategySignal(
                    tier=self._tier,
                    signal_type="logical_constraint_buy_bound",
                    market_id=rule.bound_market_id,
                    description=description,
                    expected_edge=round(violation_bps, 6),
                    confidence=self._confidence_from_violation(violation_bps),
                    recommended_size_usdc=rule.max_size_usdc or self._default_size_usdc,
                    urgency=0.7,
                    payload={
                        "action": "BUY_YES",
                        "relation_type": rule.relation_type,
                        "subject_market_id": rule.subject_market_id,
                        "bound_market_id": rule.bound_market_id,
                        "subject_price": float(subject_price),
                        "bound_price": float(bound_price),
                        "violation_bps": round(violation_bps, 6),
                        "bound_question": getattr(market, "question", ""),
                        "tags": list(rule.tags),
                    },
                )
            )
        return signals

    @staticmethod
    def _confidence_from_violation(violation_bps: float) -> float:
        return min(0.95, 0.60 + max(0.0, violation_bps - 200.0) / 2000.0)
