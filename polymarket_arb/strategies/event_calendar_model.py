"""Event-time baseline pricing for markets with known catalysts."""

from __future__ import annotations

from dataclasses import dataclass

from polymarket_arb.models import FeeStructure, MarketInfo
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier


@dataclass(frozen=True)
class EventPricingInput:
    market: MarketInfo
    market_price: float
    baseline_probability: float
    confidence: float
    time_to_event_sec: float
    taker_fee_rate: float = 0.05


class EventCalendarModel:
    """Compare market price with a precomputed event baseline."""

    def __init__(
        self,
        *,
        min_edge_bps: float = 300.0,
        min_confidence: float = 0.70,
        max_time_to_event_sec: float = 24 * 3600.0,
        default_size_usdc: float = 10.0,
    ) -> None:
        self._min_edge_bps = float(min_edge_bps)
        self._min_confidence = float(min_confidence)
        self._max_time_to_event_sec = float(max_time_to_event_sec)
        self._default_size_usdc = float(default_size_usdc)

    def evaluate(self, item: EventPricingInput) -> StrategySignal | None:
        if item.time_to_event_sec < 0 or item.time_to_event_sec > self._max_time_to_event_sec:
            return None
        if item.confidence < self._min_confidence:
            return None

        market_price = self._bounded_probability(item.market_price)
        baseline = self._bounded_probability(item.baseline_probability)
        raw_edge = baseline - market_price
        fee = FeeStructure.for_market(item.taker_fee_rate, item.market).estimate_price_fee(market_price)
        net_edge = raw_edge - fee
        edge_bps = net_edge * 10_000.0
        if edge_bps < self._min_edge_bps:
            return None

        return StrategySignal(
            tier=StrategyTier.STATISTICAL_ARB,
            signal_type="event_calendar_buy_yes",
            market_id=item.market.condition_id,
            description=f"event baseline prices YES above market for {item.market.question}",
            expected_edge=round(edge_bps, 6),
            confidence=float(item.confidence),
            recommended_size_usdc=self._default_size_usdc,
            urgency=self._urgency_from_time(item.time_to_event_sec),
            payload={
                "action": "BUY_YES",
                "market_price": market_price,
                "baseline_probability": baseline,
                "fee_per_share": round(fee, 8),
                "time_to_event_sec": float(item.time_to_event_sec),
            },
        )

    @staticmethod
    def _bounded_probability(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _urgency_from_time(time_to_event_sec: float) -> float:
        if time_to_event_sec <= 3600:
            return 0.95
        if time_to_event_sec <= 6 * 3600:
            return 0.80
        return 0.60
