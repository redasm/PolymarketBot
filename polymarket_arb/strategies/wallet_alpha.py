"""Wallet alpha scoring based on lagged follow performance."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WalletProfile:
    wallet_address: str
    trade_count: int
    realized_roi: float
    lagged_follow_roi: float
    max_drawdown: float
    concentration_score: float
    category_edges: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class WalletAlphaDecision:
    accepted: bool
    confidence: float
    size_multiplier: float
    reasons: list[str] = field(default_factory=list)


class WalletAlphaScorer:
    """Accept wallets only when delayed following still has edge."""

    def __init__(
        self,
        *,
        min_trades: int = 30,
        min_lagged_roi: float = 0.04,
        max_concentration: float = 0.35,
        max_drawdown: float = 0.35,
    ) -> None:
        self._min_trades = int(min_trades)
        self._min_lagged_roi = float(min_lagged_roi)
        self._max_concentration = float(max_concentration)
        self._max_drawdown = float(max_drawdown)

    def evaluate(self, profile: WalletProfile, *, category: str | None = None) -> WalletAlphaDecision:
        reasons: list[str] = []
        if profile.trade_count < self._min_trades:
            reasons.append("insufficient_trades")
        if profile.lagged_follow_roi < self._min_lagged_roi:
            reasons.append("lagged_roi_below_min")
        if profile.concentration_score > self._max_concentration:
            reasons.append("concentration_too_high")
        if profile.max_drawdown > self._max_drawdown:
            reasons.append("drawdown_too_high")

        category_edge = profile.category_edges.get(category or "", profile.lagged_follow_roi)
        if category is not None and category_edge < self._min_lagged_roi:
            reasons.append("category_edge_below_min")

        if reasons:
            return WalletAlphaDecision(accepted=False, confidence=0.0, size_multiplier=0.0, reasons=reasons)

        confidence = self._confidence(profile, category_edge)
        size_multiplier = min(1.25, 0.75 + confidence * 0.5)
        return WalletAlphaDecision(
            accepted=True,
            confidence=confidence,
            size_multiplier=size_multiplier,
            reasons=["lagged_alpha_confirmed"],
        )

    def _confidence(self, profile: WalletProfile, category_edge: float) -> float:
        sample_score = min(1.0, profile.trade_count / max(1, self._min_trades * 2))
        edge_score = min(1.0, category_edge / max(1e-9, self._min_lagged_roi * 3))
        drawdown_score = max(0.0, 1.0 - profile.max_drawdown / max(1e-9, self._max_drawdown))
        concentration_score = max(0.0, 1.0 - profile.concentration_score / max(1e-9, self._max_concentration))
        return round(
            0.45 + (sample_score * 0.20) + (edge_score * 0.20) + (drawdown_score * 0.10) + (concentration_score * 0.05),
            6,
        )
