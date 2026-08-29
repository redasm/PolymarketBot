"""Tests for `polymarket_arb.main_helpers.signal_collectors`.

These collectors used to live inline in `main_loop.py` and were tested
only indirectly through the run-loop. Pinning the per-tier signal
shape here so future scoring / payload tweaks are explicit instead of
silent regressions for the orchestrator + dashboard.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from polymarket_arb.main_helpers.flow_aggregator import FlowAggregator
from polymarket_arb.main_helpers.signal_collectors import (
    collect_cross_platform_strategy_signals,
    collect_event_calendar_strategy_signals,
    collect_logical_constraint_strategy_signals,
    collect_maker_strategy_signals,
    collect_statistical_strategy_signals,
    collect_wallet_alpha_strategy_signals,
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


# --------- new quant strategies ----------


def test_collect_logical_constraints_returns_empty_without_rules():
    cfg = make_test_config()
    snapshots = {"a-yes": _balanced_snapshot("a-yes"), "b-yes": _balanced_snapshot("b-yes")}

    out = collect_logical_constraint_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("a"), _binary_market("b")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        rules=[],
    )

    assert out == []


def test_collect_logical_constraints_uses_yes_mid_prices_from_books():
    cfg = make_test_config(default_order_size_usdc=11.0)
    snapshots = {
        "candidate-yes": _balanced_snapshot("candidate-yes", mid=0.62),
        "party-yes": _balanced_snapshot("party-yes", mid=0.55),
    }

    out = collect_logical_constraint_strategy_signals(
        config=cfg,
        candidate_markets=[
            _binary_market("candidate", yes_price=0.62),
            _binary_market("party", yes_price=0.55),
        ],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        rules=[
            {
                "subject_market_id": "candidate",
                "bound_market_id": "party",
                "relation_type": "subject_lte_bound",
                "min_violation_bps": 200,
            }
        ],
        input_metadata={"source": "file", "sha256": "abc"},
    )

    assert len(out) == 1
    assert out[0].signal_type == "logical_constraint_directional_buy_bound"
    assert out[0].market_id == "party"
    assert out[0].recommended_size_usdc == 11.0
    assert out[0].payload["quant_input"]["name"] == "logical_constraints"
    assert out[0].payload["quant_input"]["sha256"] == "abc"


def test_collect_logical_constraints_rejects_expired_rule_schema():
    cfg = make_test_config(default_order_size_usdc=11.0)
    snapshots = {
        "candidate-yes": _balanced_snapshot("candidate-yes", mid=0.62),
        "party-yes": _balanced_snapshot("party-yes", mid=0.55),
    }

    out = collect_logical_constraint_strategy_signals(
        config=cfg,
        candidate_markets=[
            _binary_market("candidate", yes_price=0.62),
            _binary_market("party", yes_price=0.55),
        ],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        rules={
            "schema_version": 1,
            "generated_at": time.time() - 7200,
            "expires_at": time.time() - 3600,
            "rules": [
                {
                    "subject_market_id": "candidate",
                    "bound_market_id": "party",
                    "relation_type": "subject_lte_bound",
                    "min_violation_bps": 200,
                }
            ],
        },
    )

    assert out == []


def test_collect_event_calendar_uses_explicit_baseline_metadata():
    cfg = make_test_config(default_order_size_usdc=9.0)
    market = _binary_market("event")
    snapshots = {"event-yes": _balanced_snapshot("event-yes", mid=0.45)}

    out = collect_event_calendar_strategy_signals(
        config=cfg,
        candidate_markets=[market],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        baselines={
            "event": {
                "event_baseline_probability": 0.55,
                "event_confidence": 0.80,
                "time_to_event_sec": 3600,
                "generated_at": time.time(),
            }
        },
        input_metadata={"source": "file", "sha256": "def"},
    )

    assert len(out) == 1
    assert out[0].signal_type == "event_calendar_buy_yes"
    assert out[0].market_id == "event"
    assert out[0].recommended_size_usdc == 9.0
    assert out[0].payload["quant_input"]["name"] == "event_baselines"
    assert out[0].payload["quant_input"]["sha256"] == "def"


def test_collect_event_calendar_requires_fresh_snapshot_even_when_token_price_exists():
    cfg = make_test_config(default_order_size_usdc=9.0)
    market = _binary_market("event")
    market.tokens[0].price = 0.45

    out = collect_event_calendar_strategy_signals(
        config=cfg,
        candidate_markets=[market],
        ob_analyzer=_StubBookAnalyzer({}),
        baselines={
            "event": {
                "baseline_probability": 0.55,
                "confidence": 0.80,
                "time_to_event_sec": 3600,
                "generated_at": time.time(),
            }
        },
    )

    assert out == []


def test_collect_event_calendar_ignores_market_raw_baseline_metadata():
    cfg = make_test_config(default_order_size_usdc=9.0)
    market = _binary_market("event")
    market.raw.update(
        {
            "event_baseline_probability": 0.55,
            "event_confidence": 0.80,
            "time_to_event_sec": 3600,
        }
    )

    out = collect_event_calendar_strategy_signals(
        config=cfg,
        candidate_markets=[market],
        ob_analyzer=_StubBookAnalyzer({"event-yes": _balanced_snapshot("event-yes", mid=0.45)}),
    )

    assert out == []


def test_collect_event_calendar_returns_empty_without_baseline_metadata():
    cfg = make_test_config()

    out = collect_event_calendar_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("event")],
        ob_analyzer=_StubBookAnalyzer({"event-yes": _balanced_snapshot("event-yes", mid=0.45)}),
    )

    assert out == []


def test_collect_wallet_alpha_converts_accepted_observation_to_signal():
    cfg = make_test_config(default_order_size_usdc=7.0)
    market = _binary_market("weather")
    profiles = {
        "0xgood": {
            "trade_count": 40,
            "realized_roi": 0.25,
            "lagged_follow_roi": 0.08,
            "max_drawdown": 0.10,
            "concentration_score": 0.20,
            "category_edges": {"weather": 0.09},
        }
    }
    observations = [
        {
            "wallet_address": "0xgood",
            "market_id": "weather",
            "category": "weather",
            "action": "BUY_YES",
            "observed_size_usdc": 100.0,
        }
    ]

    out = collect_wallet_alpha_strategy_signals(
        config=cfg,
        candidate_markets=[market],
        profiles=profiles,
        observations=observations,
        input_metadata={
            "profiles": {"source": "file", "sha256": "profiles"},
            "observations": {"source": "file", "sha256": "observations"},
        },
    )

    assert len(out) == 1
    assert out[0].signal_type == "wallet_alpha_buy_yes"
    assert out[0].market_id == "weather"
    assert out[0].recommended_size_usdc > 7.0
    assert out[0].payload["wallet_size_multiplier"] > 1.0
    assert out[0].payload["wallet_address"] == "0xgood"
    assert out[0].payload["quant_input"]["name"] == "wallet_alpha"
    assert out[0].payload["quant_input"]["profiles"]["sha256"] == "profiles"
    assert out[0].payload["quant_input"]["observations"]["sha256"] == "observations"


def test_collect_wallet_alpha_rejects_expired_profile_schema():
    cfg = make_test_config(default_order_size_usdc=7.0)
    market = _binary_market("weather")
    profiles = {
        "schema_version": 1,
        "generated_at": time.time() - 7200,
        "expires_at": time.time() - 3600,
        "wallets": {
            "0xgood": {
                "trade_count": 40,
                "realized_roi": 0.25,
                "lagged_follow_roi": 0.08,
                "max_drawdown": 0.10,
                "concentration_score": 0.20,
                "category_edges": {"weather": 0.09},
            }
        },
    }

    out = collect_wallet_alpha_strategy_signals(
        config=cfg,
        candidate_markets=[market],
        profiles=profiles,
        observations=[{"wallet_address": "0xgood", "market_id": "weather", "action": "BUY_YES"}],
    )

    assert out == []


def test_collect_wallet_alpha_rejects_unfollowable_wallet_profile():
    cfg = make_test_config()
    profiles = {
        "0xflash": {
            "trade_count": 4,
            "realized_roi": 2.0,
            "lagged_follow_roi": -0.05,
            "max_drawdown": 0.60,
            "concentration_score": 0.90,
            "category_edges": {"crypto": 2.0},
        }
    }

    out = collect_wallet_alpha_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("crypto")],
        profiles=profiles,
        observations=[
            {
                "wallet_address": "0xflash",
                "market_id": "crypto",
                "category": "crypto",
                "action": "BUY_YES",
            }
        ],
    )

    assert out == []


def test_collect_wallet_alpha_can_emit_unvalidated_candidate_only_in_dry_run():
    cfg = make_test_config(dry_run=True, wallet_alpha_candidate_shadow_enabled=True)

    out = collect_wallet_alpha_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("m1")],
        profiles={},
        observations=[
            {
                "wallet_address": "0xcandidate",
                "market_id": "m1",
                "category": "macro",
                "action": "BUY_YES",
                "observed_size_usdc": 12,
            }
        ],
    )

    assert len(out) == 1
    assert out[0].signal_type == "wallet_alpha_candidate_buy_yes"
    assert out[0].payload["wallet_profile_status"] == "candidate_unvalidated"
    assert out[0].payload["deviation"] > 0
    assert out[0].expected_edge > 0


def test_collect_wallet_alpha_candidate_shadow_is_disabled_in_live_mode():
    cfg = make_test_config(
        dry_run=False,
        live_trading_ack=True,
        portfolio_sync_enabled=True,
        polymarket_taker_fee_rate=0.02,
        wallet_alpha_candidate_shadow_enabled=True,
    )

    out = collect_wallet_alpha_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("m1")],
        profiles={},
        observations=[{"wallet_address": "0xcandidate", "market_id": "m1", "action": "BUY_YES"}],
    )

    assert out == []


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


def test_collect_statistical_uses_event_baseline_as_entry_timing_gate():
    cfg = make_test_config()
    snapshots = {
        "llm-yes": _balanced_snapshot("llm-yes", mid=0.40),
        "llm-no": _balanced_snapshot("llm-no", mid=0.60),
    }
    detector = _StubDetector(model_prob=0.55)

    out = collect_statistical_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("llm", yes_price=0.40)],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        detector=detector,
        event_baselines={
            "llm": {
                "baseline_probability": 0.20,
                "confidence": 0.92,
                "time_to_event_sec": 86_400,
            }
        },
    )

    assert out == []
    summary = collect_statistical_strategy_signals.last_skip_summary
    assert summary["reasons"]["event_baseline_conflict"] == 1


def test_collect_statistical_boosts_aligned_event_baseline_signal():
    cfg = make_test_config(default_order_size_usdc=20.0)
    snapshots = {
        "llm-yes": _balanced_snapshot("llm-yes", mid=0.40),
        "llm-no": _balanced_snapshot("llm-no", mid=0.60),
    }
    detector = _StubDetector(model_prob=0.55)

    out = collect_statistical_strategy_signals(
        config=cfg,
        candidate_markets=[_binary_market("llm", yes_price=0.40)],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        detector=detector,
        event_baselines={
            "llm": {
                "baseline_probability": 0.70,
                "confidence": 0.90,
                "time_to_event_sec": 86_400,
            }
        },
    )

    assert len(out) == 1
    sig = out[0]
    assert sig.signal_type == "statistical_buy_yes"
    assert sig.payload["model_prob"] > 0.55
    assert sig.payload["quant_timing"]["source"] == "event_baselines"
    assert sig.payload["quant_timing"]["reason"] == "event_baseline_aligned"
    assert sig.recommended_size_usdc > 20.0


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


def test_collect_maker_records_queue_position_telemetry():
    snapshots = {
        "m-yes": OrderBookSnapshot(
            token_id="m-yes",
            best_bid=0.49,
            best_ask=0.51,
            bids=[
                OrderBookLevel(price=0.49, size=25.0),
                OrderBookLevel(price=0.48, size=100.0),
            ],
            asks=[
                OrderBookLevel(price=0.51, size=40.0),
                OrderBookLevel(price=0.52, size=100.0),
            ],
            tick_size=0.01,
        )
    }
    maker = _StubMaker(quote=_maker_quote(bid=0.49, ask=0.51))

    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
    )

    queue = out[0].payload["queue_position"]
    assert queue["bid_ahead_size"] == 25.0
    assert queue["ask_ahead_size"] == 40.0
    assert queue["telemetry_only"] is True


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


def test_collect_maker_attaches_flow_bias_when_aggregator_has_data():
    import time as _time

    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote())
    agg = FlowAggregator(window_sec=3600.0, min_trades=1, strong_threshold=0.55)
    now = _time.time()
    agg.record_trade(condition_id="m", taker_bought_yes=True, shares=70.0, ts=now - 30)
    agg.record_trade(condition_id="m", taker_bought_yes=False, shares=30.0, ts=now - 20)

    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        flow_aggregator=agg,
    )

    assert len(out) == 1
    bias = out[0].payload.get("flow_bias")
    assert bias is not None
    assert bias["taker_yes_share"] == pytest.approx(0.70)
    assert bias["lean"] == "yes"
    assert bias["is_stable"] is True


def test_collect_maker_applies_event_time_toxic_flow_spread_multiplier():
    now = time.time()
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote())

    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        event_baselines={
            "m": {
                "baseline_probability": 0.50,
                "confidence": 0.80,
                "time_to_event_sec": 20 * 60,
                "generated_at": now,
            }
        },
    )

    assert len(out) == 1
    assert maker.calls[0]["spread_multiplier"] == pytest.approx(1.5)
    assert out[0].payload["event_time_toxicity"]["applied"] is True
    assert out[0].payload["event_time_toxicity"]["size_multiplier"] == pytest.approx(0.75)
    assert out[0].recommended_size_usdc == pytest.approx(9.0)


def test_collect_maker_omits_flow_bias_when_aggregator_has_no_data():
    snapshots = {"m-yes": _balanced_snapshot("m-yes")}
    maker = _StubMaker(quote=_maker_quote())
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)

    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        flow_aggregator=agg,
    )

    assert len(out) == 1
    assert "flow_bias" not in out[0].payload


# --------- T3: 流动性奖励带 ----------


def _reward_cfg(delta: float, min_size: float = 0.0):
    return SimpleNamespace(reward_delta=delta, rewards_min_size=min_size)


def test_maker_passes_reward_delta_to_compute_quote():
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote())
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        reward_config_provider=lambda cid: _reward_cfg(0.03),
    )
    assert len(out) == 1
    assert maker.calls[0]["reward_delta"] == pytest.approx(0.03)
    band = out[0].payload["reward_band"]
    assert band["incentivized"] is True
    assert band["delta"] == pytest.approx(0.03)
    assert band["band_lo"] == pytest.approx(0.47)
    assert band["band_hi"] == pytest.approx(0.53)
    assert band["bid_in_band"] is True
    assert band["ask_in_band"] is True


def test_maker_without_provider_keeps_legacy_behaviour():
    """没有 provider 时 δ 必须是 None —— 等价于历史的"不知道奖励带"."""
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote())
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
    )
    assert maker.calls[0]["reward_delta"] is None
    assert out[0].payload["reward_band"] == {"delta": 0.0, "incentivized": False}


def test_maker_reward_provider_failure_does_not_block_quote():
    def _boom(_cid):
        raise RuntimeError("rewards api down")

    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote())
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        reward_config_provider=_boom,
    )
    assert len(out) == 1
    assert maker.calls[0]["reward_delta"] is None


def test_maker_flags_quote_outside_reward_band():
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote(bid=0.40, ask=0.60))
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        reward_config_provider=lambda cid: _reward_cfg(0.01),
    )
    band = out[0].payload["reward_band"]
    assert band["bid_in_band"] is False
    assert band["ask_in_band"] is False


def test_maker_records_min_size_eligibility():
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote(bid_size=5.0, ask_size=6.0))
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        reward_config_provider=lambda cid: _reward_cfg(0.03, min_size=50.0),
    )
    band = out[0].payload["reward_band"]
    assert band["min_size"] == pytest.approx(50.0)
    assert band["min_size_ok"] is False


def test_maker_rewards_only_skips_unincentivized_markets():
    snapshots = {"m-yes": _balanced_snapshot("m-yes", mid=0.50)}
    maker = _StubMaker(quote=_maker_quote())
    out = collect_maker_strategy_signals(
        candidate_markets=[_binary_market("m")],
        ob_analyzer=_StubBookAnalyzer(snapshots),
        maker_strategy=maker,
        fair_values_by_market={"m": 0.50},
        reward_config_provider=lambda cid: None,
        rewards_only=True,
    )
    assert out == []
    assert maker.calls == []
    summary = collect_maker_strategy_signals.last_skip_summary
    assert summary["reasons"].get("no_reward_band") == 1
