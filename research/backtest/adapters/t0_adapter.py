"""Adapter for running T0 structural arbitrage in backtests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from polymarket_arb.arbitrage_detector import ArbitrageDetector
from polymarket_arb.models import ArbOpportunity, ArbType, MarketInfo, OrderBookLevel, OrderBookSnapshot, TokenInfo


class InMemoryOrderBookAnalyzer:
    """Lightweight snapshot provider for offline T0 backtests."""

    def __init__(self, snapshots: dict[str, OrderBookSnapshot]):
        self._snapshots = snapshots

    def get_snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        return self._snapshots.get(token_id)

    def get_executable_ask_price(self, token_id: str, target_size: float):
        snap = self._snapshots.get(token_id)
        if snap is None or not snap.asks:
            return None
        total_cost = 0.0
        filled = 0.0
        for level in snap.asks:
            take = min(level.size, target_size - filled)
            total_cost += take * level.price
            filled += take
            if filled >= target_size - 1e-9:
                break
        if filled <= 0:
            return None
        return (total_cost / filled, filled)

    def get_executable_bid_price(self, token_id: str, target_size: float):
        snap = self._snapshots.get(token_id)
        if snap is None or not snap.bids:
            return None
        total_value = 0.0
        filled = 0.0
        for level in snap.bids:
            take = min(level.size, target_size - filled)
            total_value += take * level.price
            filled += take
            if filled >= target_size - 1e-9:
                break
        if filled <= 0:
            return None
        return (total_value / filled, filled)


@dataclass
class T0BacktestAdapter:
    detector: ArbitrageDetector

    @property
    def strategy_name(self) -> str:
        return "t0_structural_arbitrage"

    def detect(self, market: Any):
        return self.detector.scan_binary_market(market)

    @classmethod
    def from_rows(cls, config: Any, row: dict[str, Any]) -> tuple["T0BacktestAdapter", MarketInfo]:
        yes_token_id = row.get("yes_token_id", "yes")
        no_token_id = row.get("no_token_id", "no")
        yes_bid_levels = _levels_from_row(row.get("yes_bid_levels"), row.get("yes_best_bid"), row.get("yes_bid_size"))
        yes_ask_levels = _levels_from_row(row.get("yes_ask_levels"), row.get("yes_best_ask"), row.get("yes_ask_size"))
        yes_snapshot = OrderBookSnapshot(
            token_id=yes_token_id,
            best_bid=row.get("yes_best_bid"),
            best_ask=row.get("yes_best_ask"),
            bids=yes_bid_levels,
            asks=yes_ask_levels,
        )
        no_bid_levels = _levels_from_row(row.get("no_bid_levels"), row.get("no_best_bid"), row.get("no_bid_size"))
        no_ask_levels = _levels_from_row(row.get("no_ask_levels"), row.get("no_best_ask"), row.get("no_ask_size"))
        no_snapshot = OrderBookSnapshot(
            token_id=no_token_id,
            best_bid=row.get("no_best_bid"),
            best_ask=row.get("no_best_ask"),
            bids=no_bid_levels,
            asks=no_ask_levels,
        )
        detector = ArbitrageDetector(
            config,
            InMemoryOrderBookAnalyzer({yes_token_id: yes_snapshot, no_token_id: no_snapshot}),
        )
        market = MarketInfo(
            condition_id=row.get("condition_id", "cond"),
            question=row.get("question", "backtest market"),
            slug=row.get("slug", "backtest-market"),
            tokens=[
                TokenInfo(token_id=yes_token_id, outcome="Yes"),
                TokenInfo(token_id=no_token_id, outcome="No"),
            ],
            active=True,
            closed=False,
            event_id=row.get("event_id", "event"),
        )
        return cls(detector=detector), market

    def to_order_request(self, opp: ArbOpportunity | None) -> dict[str, Any]:
        if opp is None or opp.arb_type != ArbType.BINARY or not opp.legs:
            return {"side": "BUY", "size": 0.0}
        first_leg = opp.legs[0]
        ask_levels = []
        if hasattr(self.detector, "_ob") and hasattr(self.detector._ob, "get_snapshot"):
            snap = self.detector._ob.get_snapshot(first_leg.token_id)
            if snap is not None:
                ask_levels = [(level.price, level.size) for level in snap.asks]
        return {
            "side": "BUY",
            "size": min(leg.size for leg in opp.legs if leg.size > 0) if any(leg.size > 0 for leg in opp.legs) else opp.max_executable_size,
            "ask_levels": ask_levels,
            "available_size": sum(size for _, size in ask_levels) if ask_levels else opp.max_executable_size,
            "best_ask": ask_levels[0][0] if ask_levels else first_leg.execution_price,
        }


def _levels_from_row(levels: Any, best_price: Any, best_size: Any) -> list[OrderBookLevel]:
    parsed: list[OrderBookLevel] = []
    if isinstance(levels, list):
        for item in levels:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            parsed.append(OrderBookLevel(float(item[0]), float(item[1])))
    if parsed:
        return parsed
    if best_price is None:
        return []
    return [OrderBookLevel(float(best_price), float(best_size or 0.0))]
