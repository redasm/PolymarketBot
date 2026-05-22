"""Tests for the rolling-`p_t` provider that feeds T2ExitManager.

The provider's contract: given a `(market, token_id)`, return either a
fresh model probability in TOKEN-SPACE (P(this token resolves to 1)),
or None when the inputs aren't usable. The exit manager handles None by
falling back to the entry-time probability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from polymarket_arb.main_helpers.t2_model_prob import build_t2_model_prob_provider
from polymarket_arb.models import (
    MarketInfo,
    OrderBookLevel,
    OrderBookSnapshot,
    TokenInfo,
)


@dataclass
class _StubEstimate:
    model_prob: float
    market_prob: float = 0.50
    deviation: float = 0.0
    deviation_pct: float = 0.0
    confidence: float = 0.7
    signals: dict[str, float] | None = None
    market_id: str = "c1"
    outcome: str = "YES"


class _StubDetector:
    def __init__(self, *, yes_prob: float, raise_on_call: bool = False):
        self._yes_prob = yes_prob
        self._raise = raise_on_call
        self.calls: list[dict[str, Any]] = []

    def estimate_market_probability(
        self,
        *,
        market_id: str,
        outcome: str,
        market_price: float,
        bids_total_size: float,
        asks_total_size: float,
        mid_price: float,
        related_market_prices: dict[str, Any] | None = None,
    ) -> _StubEstimate:
        self.calls.append(
            {
                "market_id": market_id,
                "outcome": outcome,
                "market_price": market_price,
                "bids_total_size": bids_total_size,
                "asks_total_size": asks_total_size,
                "mid_price": mid_price,
            }
        )
        if self._raise:
            raise RuntimeError("simulated detector failure")
        return _StubEstimate(model_prob=self._yes_prob)


class _StubOB:
    def __init__(self, snapshots: dict[str, OrderBookSnapshot]):
        self._snaps = snapshots

    def get_snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        return self._snaps.get(token_id)


def _market(yes_token_id: str = "t-yes", no_token_id: str = "t-no") -> MarketInfo:
    return MarketInfo(
        condition_id="c1",
        question="Will BTC > $100k by EOY?",
        slug="btc",
        tokens=[
            TokenInfo(token_id=yes_token_id, outcome="Yes", price=0.62),
            TokenInfo(token_id=no_token_id, outcome="No", price=0.38),
        ],
    )


def _snap(token_id: str, *, best_bid: float, best_ask: float | None = None) -> OrderBookSnapshot:
    if best_ask is None:
        best_ask = best_bid + 0.02
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bids=[OrderBookLevel(best_bid, 100)],
        asks=[OrderBookLevel(best_ask, 100)],
    )


def test_provider_returns_yes_prob_for_yes_token():
    detector = _StubDetector(yes_prob=0.62)
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.60)})
    provider = build_t2_model_prob_provider(
        statistical_detector=detector, ob_analyzer=ob
    )

    prob = provider(_market(), "t-yes")

    assert prob == 0.62
    assert detector.calls[0]["outcome"] == "YES"
    assert detector.calls[0]["market_id"] == "c1"


def test_provider_inverts_to_no_space_for_no_token():
    detector = _StubDetector(yes_prob=0.62)
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.60)})
    provider = build_t2_model_prob_provider(
        statistical_detector=detector, ob_analyzer=ob
    )

    prob = provider(_market(), "t-no")

    # Token is NO → terminal payoff is P(NO) = 1 - P(YES) = 0.38
    assert prob == 0.38


def test_provider_returns_none_when_no_snapshot():
    detector = _StubDetector(yes_prob=0.62)
    ob = _StubOB({})  # empty
    provider = build_t2_model_prob_provider(
        statistical_detector=detector, ob_analyzer=ob
    )

    assert provider(_market(), "t-yes") is None
    assert detector.calls == []  # detector not invoked


def test_provider_returns_none_when_detector_raises():
    detector = _StubDetector(yes_prob=0.5, raise_on_call=True)
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})
    provider = build_t2_model_prob_provider(
        statistical_detector=detector, ob_analyzer=ob
    )

    assert provider(_market(), "t-yes") is None


def test_provider_clamps_extreme_yes_prob_to_unit_interval():
    detector = _StubDetector(yes_prob=1.5)  # bogus
    ob = _StubOB({"t-yes": _snap("t-yes", best_bid=0.50)})
    provider = build_t2_model_prob_provider(
        statistical_detector=detector, ob_analyzer=ob
    )

    assert provider(_market(), "t-yes") == 1.0
