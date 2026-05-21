"""High-conviction gate for directional strategy signals.

The gate is intentionally conservative: it does not create signals and it
does not touch structural arbitrage or maker quoting. It only blocks or sizes
directional signals whose edge, confidence, liquidity, or risk metadata is too
weak for live execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier


@dataclass(frozen=True)
class SniperGateConfig:
    min_net_edge_bps: float = 500.0
    min_confidence: float = 0.75
    min_liquidity: float = 0.0
    min_volume_24h: float = 0.0
    max_correlation_score: float = 0.80
    require_max_loss_defined: bool = False
    applicable_tiers: frozenset[StrategyTier] = field(
        default_factory=lambda: frozenset({StrategyTier.CROSS_PLATFORM, StrategyTier.STATISTICAL_ARB})
    )


@dataclass(frozen=True)
class SniperGateDecision:
    accepted: bool
    size_multiplier: float = 1.0
    reasons: list[str] = field(default_factory=list)


class SniperGate:
    """Reject weak directional signals before capital allocation."""

    def __init__(self, config: SniperGateConfig | None = None) -> None:
        self._config = config or SniperGateConfig()

    def evaluate(self, signal: StrategySignal, *, market: Any | None = None) -> SniperGateDecision:
        if signal.tier not in self._config.applicable_tiers:
            return SniperGateDecision(accepted=True, reasons=["not_applicable"])

        reasons: list[str] = []
        if float(signal.expected_edge) < self._config.min_net_edge_bps:
            reasons.append("edge_below_min")
        if float(signal.confidence) < self._config.min_confidence:
            reasons.append("confidence_below_min")

        if market is not None:
            liquidity = float(getattr(market, "liquidity", 0.0) or 0.0)
            volume = float(getattr(market, "volume_24h", 0.0) or 0.0)
            if liquidity < self._config.min_liquidity:
                reasons.append("liquidity_below_min")
            if volume < self._config.min_volume_24h:
                reasons.append("volume_below_min")

        payload = signal.payload or {}
        correlation = float(payload.get("correlation_score", 0.0) or 0.0)
        if correlation > self._config.max_correlation_score:
            reasons.append("correlation_too_crowded")
        if self._config.require_max_loss_defined and not payload.get("max_loss_usdc"):
            reasons.append("max_loss_missing")

        if reasons:
            return SniperGateDecision(accepted=False, size_multiplier=0.0, reasons=reasons)

        return SniperGateDecision(accepted=True, size_multiplier=1.0, reasons=["accepted"])
