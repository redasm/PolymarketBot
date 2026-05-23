"""Rolling-`p_t` provider for T2 exit manager.

The T2 exit manager's Bellman policy uses a `terminal_prob` parameter
that fixes the value-function. Freezing this at entry means information
arriving after entry — e.g. order-flow imbalance shifts, momentum
changes, related-market price moves — can't update the optimal stop
threshold. That is exactly the case Article 1 ("加上马尔可夫" section)
warns against: "如果新消息让你更新 p_t，就重新跑一次后向归纳".

This module bridges the `StatisticalMispricingDetector` (which already
fuses OBI + momentum + cross-market into a single YES-space probability)
to the exit manager. The provider:

  1. Looks up the position's market and token outcome.
  2. Reads the current orderbook snapshot from the same `ob_analyzer`
     the entry-side detector used.
  3. Calls `estimate_market_probability(outcome="YES", market_price=mid)`
     (matching the entry-side call in `signal_collectors`).
  4. Converts the YES-space estimate to TOKEN-space (P(NO) = 1 - P(YES)).

If anything is missing (snapshot, mid, detector exception), returns
`None` — the exit manager falls back to the entry-time prob, which is
the legacy behaviour.
"""

from __future__ import annotations

import logging
from typing import Callable

from polymarket_arb.models import MarketInfo
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.strategies.statistical_model import (
    StatisticalMispricingDetector,
)

LOG = logging.getLogger(__name__)


def build_t2_model_prob_provider(
    *,
    statistical_detector: StatisticalMispricingDetector,
    ob_analyzer: OrderBookAnalyzer,
) -> Callable[[MarketInfo, str], float | None]:
    """Return a callable suitable for `T2ExitManager(model_prob_provider=...)`.

    The returned function is pure: same orderbook state → same answer.
    Cheap to call (the detector is just arithmetic over imbalance /
    momentum windows), safe to invoke once per open position per
    evaluate() cycle.
    """

    def _provider(market: MarketInfo, token_id: str) -> float | None:
        yes_token = _find_yes_token(market)
        if yes_token is None:
            return None
        yes_snap = ob_analyzer.get_snapshot(yes_token.token_id)
        if yes_snap is None or yes_snap.mid is None:
            return None

        bids_total = sum(level.size for level in yes_snap.bids[:5])
        asks_total = sum(level.size for level in yes_snap.asks[:5])
        try:
            estimate = statistical_detector.estimate_market_probability(
                market_id=market.condition_id,
                outcome="YES",
                market_price=float(yes_snap.mid),
                bids_total_size=bids_total,
                asks_total_size=asks_total,
                mid_price=float(yes_snap.mid),
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug(
                "statistical_detector.estimate_market_probability failed for %s: %s",
                market.condition_id[:12],
                exc,
            )
            return None

        yes_prob = max(0.0, min(1.0, float(estimate.model_prob)))
        # Convert to TOKEN-space: if the held token is NO, the position
        # pays out P(NO) = 1 - P(YES). The Bellman policy expects
        # terminal_prob in the same space as the held token's price.
        outcome = _token_outcome(market, token_id)
        if outcome.strip().lower() == "no":
            return 1.0 - yes_prob
        return yes_prob

    return _provider


def _find_yes_token(market: MarketInfo):
    for token in market.tokens:
        if (token.outcome or "").strip().lower() == "yes":
            return token
    return market.tokens[0] if market.tokens else None


def _token_outcome(market: MarketInfo, token_id: str) -> str:
    for token in market.tokens:
        if token.token_id == token_id:
            return token.outcome or ""
    return ""
