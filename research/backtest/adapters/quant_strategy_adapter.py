"""Backtest adapters for opt-in quant strategy signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from polymarket_arb.models import MarketInfo, TokenInfo
from polymarket_arb.strategies.event_calendar_model import EventCalendarModel, EventPricingInput
from polymarket_arb.strategies.logical_constraints import LogicalConstraintDetector, RelationRule
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier
from polymarket_arb.strategies.wallet_alpha import WalletAlphaScorer, WalletProfile


@dataclass
class LogicalConstraintBacktestAdapter:
    rules: list[RelationRule]
    order_size_usdc: float = 10.0

    @property
    def strategy_name(self) -> str:
        return "logical_constraint"

    def detect_many(self, rows: list[dict[str, Any]]) -> list[StrategySignal]:
        markets = {_market_id(row): _market_from_row(row) for row in rows}
        yes_prices = {
            _market_id(row): price
            for row in rows
            if (price := _yes_mid_or_price(row)) is not None
        }
        detector = LogicalConstraintDetector(
            rules=self.rules,
            default_size_usdc=self.order_size_usdc,
        )
        return detector.detect(markets=markets, yes_prices=yes_prices)

    def to_order_request(self, signal: StrategySignal, row: dict[str, Any]) -> dict[str, Any]:
        return _yes_buy_order(signal, row)


@dataclass
class EventCalendarBacktestAdapter:
    order_size_usdc: float = 10.0
    taker_fee_rate: float = 0.05
    model: EventCalendarModel = field(init=False)

    def __post_init__(self) -> None:
        self.model = EventCalendarModel(default_size_usdc=self.order_size_usdc)

    @property
    def strategy_name(self) -> str:
        return "event_calendar"

    def detect(self, row: dict[str, Any]) -> StrategySignal | None:
        market_price = _yes_mid_or_price(row)
        baseline = _float_or_none(row.get("baseline_probability") or row.get("event_baseline_probability"))
        confidence = _float_or_none(row.get("confidence") or row.get("event_confidence"))
        time_to_event = _float_or_none(row.get("time_to_event_sec") or row.get("seconds_to_event"))
        if market_price is None or baseline is None or confidence is None or time_to_event is None:
            return None
        return self.model.evaluate(
            EventPricingInput(
                market=_market_from_row(row),
                market_price=market_price,
                baseline_probability=baseline,
                confidence=confidence,
                time_to_event_sec=time_to_event,
                taker_fee_rate=self.taker_fee_rate,
            )
        )

    def to_order_request(self, signal: StrategySignal, row: dict[str, Any]) -> dict[str, Any]:
        return _yes_buy_order(signal, row)


@dataclass
class WalletAlphaBacktestAdapter:
    profiles: dict[str, WalletProfile]
    order_size_usdc: float = 10.0
    scorer: WalletAlphaScorer = field(default_factory=WalletAlphaScorer)

    @property
    def strategy_name(self) -> str:
        return "wallet_alpha"

    def detect(self, row: dict[str, Any]) -> StrategySignal | None:
        wallet = str(row.get("wallet_address") or "")
        action = str(row.get("action") or "BUY_YES").upper()
        profile = self.profiles.get(wallet)
        if profile is None or action not in {"BUY_YES", "BUY_NO"}:
            return None
        category = str(row.get("category") or "")
        decision = self.scorer.evaluate(profile, category=category or None)
        if not decision.accepted:
            return None
        market = _market_from_row(row)
        return StrategySignal(
            tier=StrategyTier.STATISTICAL_ARB,
            signal_type=f"wallet_alpha_{action.lower()}",
            market_id=market.condition_id,
            description=f"wallet alpha follow {wallet[:10]} on {market.question[:80]}",
            expected_edge=max(0.0, profile.lagged_follow_roi) * 10_000.0,
            confidence=decision.confidence,
            recommended_size_usdc=self.order_size_usdc,
            urgency=0.65,
            payload={
                "action": action,
                "wallet_address": wallet,
                "category": category,
                "lagged_follow_roi": profile.lagged_follow_roi,
                "wallet_reasons": list(decision.reasons),
            },
        )

    def to_order_request(self, signal: StrategySignal, row: dict[str, Any]) -> dict[str, Any]:
        if signal.payload.get("action") == "BUY_NO":
            return _buy_order(signal, row, prefix="no")
        return _yes_buy_order(signal, row)


def _market_from_row(row: dict[str, Any]) -> MarketInfo:
    market_id = _market_id(row)
    return MarketInfo(
        condition_id=market_id,
        question=str(row.get("question") or "backtest market"),
        slug=str(row.get("slug") or market_id),
        tokens=[
            TokenInfo(
                token_id=str(row.get("yes_token_id") or f"{market_id}-yes"),
                outcome="Yes",
                price=float(row.get("yes_price") or row.get("price") or 0.0),
            ),
            TokenInfo(
                token_id=str(row.get("no_token_id") or f"{market_id}-no"),
                outcome="No",
                price=float(row.get("no_price") or 0.0),
            ),
        ],
        liquidity=float(row.get("liquidity") or 0.0),
        volume_24h=float(row.get("volume_24h") or 0.0),
        event_id=str(row.get("event_id") or ""),
        raw=dict(row),
    )


def _market_id(row: dict[str, Any]) -> str:
    return str(row.get("condition_id") or row.get("market_id") or "")


def _yes_mid_or_price(row: dict[str, Any]) -> float | None:
    bid = _float_or_none(row.get("yes_best_bid"))
    ask = _float_or_none(row.get("yes_best_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return _float_or_none(row.get("yes_price") or row.get("price"))


def _yes_buy_order(signal: StrategySignal, row: dict[str, Any]) -> dict[str, Any]:
    return _buy_order(signal, row, prefix="yes")


def _buy_order(signal: StrategySignal, row: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    best_ask = float(row.get(f"{prefix}_best_ask") or 0.0)
    available_size = float(row.get(f"{prefix}_ask_size") or 0.0)
    size = signal.recommended_size_usdc / best_ask if best_ask > 0 else 0.0
    return {
        "side": "BUY",
        "size": min(size, available_size) if available_size > 0 else size,
        "best_ask": best_ask,
        "available_size": available_size,
        "ask_levels": [(best_ask, available_size)] if best_ask > 0 and available_size > 0 else [],
    }


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
