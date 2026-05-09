"""Tests for `polymarket_arb.main_helpers.signal_collectors`.

These collectors used to live inline in `main_loop.py` and were tested
only indirectly through the run-loop. Pinning the per-tier signal
shape here so future scoring / payload tweaks are explicit instead of
silent regressions for the orchestrator + dashboard.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymarket_arb.main_helpers.signal_collectors import (
    collect_cross_platform_strategy_signals,
    collect_maker_strategy_signals,
    collect_statistical_strategy_signals,
)
from polymarket_arb.models import MarketInfo, OrderBookLevel, OrderBookSnapshot, TokenInfo
from polymarket_arb.strategies.strategy_orchestrator import StrategyTier
from tests.conftest import make_test_config


# --------- shared fixtures / fakes ----------


def _binary_market(
    cid: str = "c1",
    yes_price: float = 0.45,
    *,
    closed: bool = False,
    active: bool = True,
) -> MarketInfo:
    return MarketInfo(
        condition_id=cid,
        question=f"Will {cid} happen by January 1, 2030?",
        slug=cid,
        tokens=[
            TokenInfo(token_id=f"{cid}-yes", outcome="Yes", price=yes_price),
            TokenInfo(token_id=f"{cid}-no", outcome="No", price=1 - yes_price),
        ],
        active=active,
        closed=closed,
        volume_24h=1_000.0,
        liquidity=1_000.0,
    )


def _balanced_snapshot(token_id: str, mid: float = 0.50) -> OrderBookSnapshot:
    """Generous-depth, tight-spread snapshot that passes default T2 gates.

    Default `make_test_config` sets `t2_max_spread_bps=80`, so spread is
    sized as ~2 bps off mid; depth comfortably above the 100-unit floor.
    """
    bid_price = round(mid * 0.999, 4)
    ask_price = round(mid * 1.001, 4)
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=bid_price,
        best_ask=ask_price,
        bids=[OrderBookLevel(price=bid_price, size=500.0)],
        asks=[OrderBookLevel(price=ask_price, size=500.0)],
        tick_size=0.01,
    )


class _StubBookAnalyzer:
    """Implements just the slice of `OrderBookAnalyzer` the collectors call."""

    def __init__(self, snapshots: dict[str, OrderBookSnapshot]):
        self._snapshots = snapshots

    def get_snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        return self._snapshots.get(token_id)


# --------- T1: cross-platform ----------


def test_collect_cross_platform_returns_empty_when_scanner_disabled():
    cfg = make_test_config()
    assert collect_cross_platform_strategy_signals(config=cfg, scanner=None) == []


def test_collect_cross_platform_translates_each_opportunity():
    cfg = make_test_config(default_order_size_usdc=25.0)

    pair = SimpleNamespace(
        polymarket_condition_id="cP",
        event_description="Election outcome event " * 10,
        pair_id="pair-X",
    )
    opp = SimpleNamespace(
        direction="poly_long_kalshi_short",
        pair=pair,
        edge_pct=0.012,
        confidence=0.78,
        poly_cost=0.40,
        kalshi_cost=0.55,
        total_cost=0.95,
        net_edge=0.05,
    )

    class _StubScanner:
        def scan(self):
            return [opp]

    out = collect_cross_platform_strategy_signals(config=cfg, scanner=_StubScanner())
    assert len(out) == 1
    sig = out[0]
    assert sig.tier == StrategyTier.CROSS_PLATFORM
    assert sig.signal_type == "cross_platform_poly_long_kalshi_short"
    assert sig.market_id == "cP"
    assert sig.recommended_size_usdc == 25.0
    assert sig.urgency == 0.9
    # description truncated to 120 chars
    assert len(sig.description) <= 120
    assert sig.payload["pair_id"] == "pair-X"
    assert sig.payload["edge_pct"] == 0.012


# --------- T2: statistical ----------


class _StubDetector:
    """Stand-in for `StatisticalMispricingDetector`."""

    def __init__(self, *, model_prob: float = 0.55):
        self._model_prob = model_prob
        self.analyze_calls: list[dict] = []
        self.estimate_calls: list[dict] = []

    def analyze(self, **kwargs):
        self.analyze_calls.append(kwargs)
        market_price = float(kwargs["market_price"])
        deviation = self._model_prob - market_price
        return SimpleNamespace(
            outcome=kwargs["outcome"],
            model_prob=self._model_prob,
            market_prob=market_price,
            deviation=deviation,
            deviation_pct=deviation / market_price if market_price else 0.0,
            abs_edge=abs(deviation),
            confidence=0.65,
            is_underpriced=deviation > 0,
            signals={"momentum": 0.1},
        )

    def estimate_market_probability(self, **kwargs):
        self.estimate_calls.append(kwargs)
        return SimpleNamespace(model_prob=self._model_prob)


def test_collect_statistical_skips_inactive_and_closed_markets():
    cfg = make_test_config()
    snapshots = {
        "open-yes": _balanced_snapshot("open-yes"),
        "open-no": _balanced_snapshot("open-no"),
    }
    markets = [
        _binary_market("open"),
        _binary_market("closed", closed=True),
        _binary_market("inactive", active=False),
    ]
    out = collect_statistical_strategy_signals(
        config=cfg,
        candidate_markets=markets,
        ob_analyzer=_StubBookAnalyzer(snapshots),
        detector=_StubDetector(),
    )
    # only `open` had snapshots AND was active+open
    assert {sig.market_id for sig in out} == {"open"}


def test_collect_statistical_drops_market_when_quality_gate_fails():
    cfg = make_test_config(t2_max_spread_bps=0.5)  # impossible spread budget
    snapshots = {
        "wide-yes": _balanced_snapshot("wide-yes"),
        "wide-no": _balanced_snapshot("wide-no"),
    }
    detector = _StubDetector()
    out = collect_statistical_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("wide")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        detector=detector,
    )
    assert out == []
    # Quality gate vetoed before the detector was even consulted.
    assert detector.analyze_calls == []


def test_collect_statistical_emits_buy_yes_when_underpriced():
    cfg = make_test_config()
    snapshots = {
        "u-yes": _balanced_snapshot("u-yes", mid=0.40),
        "u-no": _balanced_snapshot("u-no", mid=0.60),
    }
    detector = _StubDetector(model_prob=0.55)
    out = collect_statistical_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("u", yes_price=0.40)],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        detector=detector,
    )
    assert len(out) == 1
    sig = out[0]
    assert sig.signal_type == "statistical_buy_yes"
    assert sig.tier == StrategyTier.STATISTICAL_ARB
    assert sig.payload["model_prob"] == 0.55
    assert sig.payload["deviation"] == pytest.approx(0.15)
    assert sig.payload["quality"]["passes"] is True


def test_collect_statistical_emits_buy_no_when_overpriced():
    cfg = make_test_config()
    snapshots = {
        "o-yes": _balanced_snapshot("o-yes", mid=0.70),
        "o-no": _balanced_snapshot("o-no", mid=0.30),
    }
    detector = _StubDetector(model_prob=0.55)
    out = collect_statistical_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("o", yes_price=0.70)],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        detector=detector,
    )
    assert out[0].signal_type == "statistical_buy_no"


# --------- T3: maker ----------


class _StubMaker:
    def __init__(self, quote):
        self._quote = quote
        self.calls: list[dict] = []

    def compute_quote(self, **kwargs):
        self.calls.append(kwargs)
        return self._quote


def _maker_quote(bid=0.49, ask=0.51, fair=0.50, *, bid_size=10.0, ask_size=12.0):
    return SimpleNamespace(
        bid_price=bid,
        ask_price=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        spread=ask - bid if (bid is not None and ask is not None) else 0.0,
        fair_value=fair,
    )


def test_collect_maker_uses_supplied_fair_value():
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    detector = _StubDetector()  # should NOT be consulted
    maker = _StubMaker(quote=_maker_quote())
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        detector=detector,
    )
    assert len(out) == 1
    sig = out[0]
    assert sig.tier == StrategyTier.MARKET_MAKING
    assert sig.signal_type == "maker_quote"
    # recommended size = max(bid_size, ask_size)
    assert sig.recommended_size_usdc == 12.0
    assert detector.estimate_calls == []  # didn't fall back


def test_collect_maker_falls_back_to_detector_when_no_fair_value():
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    detector = _StubDetector(model_prob=0.50)
    maker = _StubMaker(quote=_maker_quote())
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={},
        detector=detector,
    )
    assert len(out) == 1
    assert detector.estimate_calls != []  # fallback was exercised


def test_collect_maker_returns_empty_when_quote_blank():
    snapshots = {"m-yes": _balanced_snapshot("m-yes")}
    maker = _StubMaker(quote=_maker_quote(bid=None, ask=None))
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
    )
    assert out == []


def test_collect_maker_returns_empty_when_no_fair_value_and_no_detector():
    snapshots = {"m-yes": _balanced_snapshot("m-yes")}
    maker = _StubMaker(quote=_maker_quote())
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={},
        detector=None,
    )
    assert out == []
