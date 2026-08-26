"""Adapter for offline T2 statistical arbitrage markout backtests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from research.backtest.features import summarize_binary_microstructure
from research.backtest.execution_model.base import estimate_binary_clob_fee
from polymarket_arb.strategies.statistical_model import ProbabilityEstimate, StatisticalMispricingDetector


@dataclass
class T2BacktestSignal:
    market_id: str
    action: str
    edge_bps: float
    confidence: float
    model_prob: float
    market_prob: float
    entry_price: float
    entry_notional_usdc: float
    available_size: float


@dataclass
class T2BacktestAdapter:
    detector: StatisticalMispricingDetector
    max_spread_bps: float | None = None
    min_top_depth: float | None = None
    max_complement_error_bps: float | None = None

    @property
    def strategy_name(self) -> str:
        return "t2_statistical_arbitrage"

    def detect(self, row: dict[str, Any], *, order_size_usdc: float) -> T2BacktestSignal | None:
        yes_mid = _mid(row.get("yes_best_bid"), row.get("yes_best_ask"))
        if yes_mid is None:
            return None
        if not self._passes_quality_filters(row):
            return None

        estimate = self.detector.analyze(
            market_id=str(row.get("condition_id") or ""),
            outcome="YES",
            market_price=yes_mid,
            bids_total_size=float(row.get("yes_bid_size") or 0.0),
            asks_total_size=float(row.get("yes_ask_size") or 0.0),
            mid_price=yes_mid,
        )
        if estimate is None:
            return None

        action = "BUY_YES" if estimate.is_underpriced else "BUY_NO"
        entry_price = float(row.get("yes_best_ask") if action == "BUY_YES" else row.get("no_best_ask") or 0.0)
        available_size = float(row.get("yes_ask_size") if action == "BUY_YES" else row.get("no_ask_size") or 0.0)
        if entry_price <= 0:
            return None

        return T2BacktestSignal(
            market_id=str(row.get("condition_id") or ""),
            action=action,
            edge_bps=estimate.abs_edge * 10_000.0,
            confidence=estimate.confidence,
            model_prob=estimate.model_prob,
            market_prob=estimate.market_prob,
            entry_price=entry_price,
            entry_notional_usdc=order_size_usdc,
            available_size=available_size,
        )

    def to_order_request(self, signal: T2BacktestSignal, row: dict[str, Any]) -> dict[str, Any]:
        token_prefix = "yes" if signal.action == "BUY_YES" else "no"
        ask_price = float(row.get(f"{token_prefix}_best_ask") or signal.entry_price)
        ask_size = float(row.get(f"{token_prefix}_ask_size") or signal.available_size)
        ask_levels = _levels_from_row(row.get(f"{token_prefix}_ask_levels"), ask_price, ask_size)
        size = signal.entry_notional_usdc / ask_price if ask_price > 0 else 0.0
        return {
            "side": "BUY",
            "size": min(size, sum(level[1] for level in ask_levels) if ask_levels else ask_size),
            "ask_levels": ask_levels if ask_levels else [(ask_price, ask_size)],
            "available_size": sum(level[1] for level in ask_levels) if ask_levels else ask_size,
            "best_ask": ask_price,
        }

    def realized_pnl(
        self,
        signal: T2BacktestSignal,
        *,
        execution_price: float,
        filled_size: float,
        entry_fees: float,
        exit_row: dict[str, Any] | None,
        exit_fee_rate: float,
    ) -> tuple[float, dict[str, float | None]]:
        if exit_row is None:
            return 0.0, {"exit_price": None, "markout_bps": None}

        prefix = "yes" if signal.action == "BUY_YES" else "no"
        exit_price = _executable_bid_price(
            exit_row.get(f"{prefix}_best_bid"),
            exit_row.get(f"{prefix}_bid_size"),
            exit_row.get(f"{prefix}_bid_levels"),
            filled_size,
        )
        if exit_price is None:
            return 0.0, {"exit_price": None, "markout_bps": None}

        gross = (float(exit_price) - execution_price) * filled_size
        exit_fee = estimate_binary_clob_fee(float(exit_price), filled_size, exit_fee_rate)
        pnl = gross - entry_fees - exit_fee
        markout_bps = ((float(exit_price) - execution_price) / execution_price) * 10_000.0 if execution_price > 0 else None
        return pnl, {"exit_price": float(exit_price), "markout_bps": markout_bps}

    def _passes_quality_filters(self, row: dict[str, Any]) -> bool:
        features = summarize_binary_microstructure(row)
        yes_spread = features.get("yes_spread_bps")
        no_spread = features.get("no_spread_bps")
        complement_error = features.get("complement_error_bps")
        if self.max_spread_bps is not None:
            if yes_spread is None or no_spread is None:
                return False
            if max(float(yes_spread), float(no_spread)) > self.max_spread_bps:
                return False
        if self.max_complement_error_bps is not None:
            if complement_error is None or float(complement_error) > self.max_complement_error_bps:
                return False
        if self.min_top_depth is not None:
            yes_ask_size = float(row.get("yes_ask_size") or 0.0)
            no_ask_size = float(row.get("no_ask_size") or 0.0)
            if min(yes_ask_size, no_ask_size) < self.min_top_depth:
                return False
        return True


def build_t2_adapter(
    *,
    min_deviation: float = 0.005,
    min_confidence: float = 0.1,
    max_spread_bps: float | None = None,
    min_top_depth: float | None = None,
    max_complement_error_bps: float | None = None,
) -> T2BacktestAdapter:
    return T2BacktestAdapter(
        detector=StatisticalMispricingDetector(
            min_deviation=min_deviation,
            min_confidence=min_confidence,
        ),
        max_spread_bps=max_spread_bps,
        min_top_depth=min_top_depth,
        max_complement_error_bps=max_complement_error_bps,
    )


def _mid(bid: Any, ask: Any) -> float | None:
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2.0


def _levels_from_row(levels: Any, best_price: float, best_size: float) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    if isinstance(levels, list):
        for item in levels:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            parsed.append((float(item[0]), float(item[1])))
    if parsed:
        return parsed
    return [(best_price, best_size)] if best_price > 0 and best_size > 0 else []


def _executable_bid_price(
    best_bid: Any,
    best_size: Any,
    levels: Any,
    target_size: float,
) -> float | None:
    """Return sell VWAP at executable bids; never mark exits at midpoint."""
    try:
        fallback_size = float(best_size or 0.0)
        fallback_price = float(best_bid) if best_bid is not None else 0.0
    except (TypeError, ValueError):
        return None
    bid_levels = _levels_from_row(levels, fallback_price, fallback_size)
    remaining = max(0.0, float(target_size))
    if remaining <= 0 or not bid_levels:
        return None
    value = 0.0
    filled = 0.0
    for price, size in bid_levels:
        take = min(max(0.0, float(size)), remaining - filled)
        if take <= 0:
            continue
        value += take * float(price)
        filled += take
        if filled >= remaining - 1e-9:
            break
    return value / filled if filled > 0 else None
