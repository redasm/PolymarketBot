"""Tests for `polymarket_arb.main_helpers.directional_opportunity`.

Existing happy-path coverage lives in `tests/test_main_loop.py` (it
uses the underscore-aliased import). This file adds focused coverage
of every rejection path so the stable-identifier contract documented
in the module docstring stays honest.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymarket_arb.main_helpers.directional_opportunity import (
    build_directional_opportunity_from_signal,
)
from polymarket_arb.models import MarketInfo, TokenInfo
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier


# ---------- shared fixtures ---------------------------------------------------


def _make_signal(
    *,
    action: str = "BUY_YES",
    recommended_size_usdc: float = 1.0,
    deviation: float = 0.02,
    expected_edge: float = 200.0,
) -> StrategySignal:
    return StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type=f"statistical_{action.lower()}",
        market_id="cond-1",
        description="test",
        expected_edge=expected_edge,
        confidence=0.7,
        recommended_size_usdc=recommended_size_usdc,
        payload={"action": action, "deviation": deviation},
    )


def _make_market(num_tokens: int = 2) -> MarketInfo:
    tokens = [TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")]
    if num_tokens == 1:
        tokens = tokens[:1]
    return MarketInfo(
        condition_id="cond-1",
        question="Will X happen?",
        slug="will-x",
        tokens=tokens,
    )


def _make_config(**overrides) -> SimpleNamespace:
    """Minimal config stub matching the attributes the helper reads."""
    base = dict(
        dry_run=True,
        polymarket_taker_fee_rate=0.02,
        live_max_orderbook_snapshot_age_sec=2.0,
        live_min_ws_hit_ratio=0.8,
        live_min_net_edge_usd=0.001,
        live_min_net_edge_bps=10.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _snap(
    best_ask: float | None = 0.5,
    best_bid: float = 0.49,
    *,
    timestamp: float | None = None,
):
    return SimpleNamespace(
        best_ask=best_ask,
        best_bid=best_bid,
        asks=[SimpleNamespace(price=best_ask or 0.0, size=100.0)],
        bids=[SimpleNamespace(price=best_bid, size=100.0)],
        timestamp=timestamp,
    )


def _ob_analyzer(
    *,
    snapshot=None,
    executable: tuple[float, float] | None = (0.5, 2.0),
    feed_health: dict | None = None,
):
    return SimpleNamespace(
        get_snapshot=lambda _tok: snapshot if snapshot is not None else _snap(),
        get_executable_ask_price=lambda _tok, _size: executable,
        feed_health=(lambda **_: feed_health) if feed_health is not None else None,
    )


# ---------- rejection paths --------------------------------------------------


def test_unsupported_direction_rejected() -> None:
    signal = _make_signal(action="HOLD")
    opp, size, reason = build_directional_opportunity_from_signal(
        config=_make_config(),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert opp is None and size == 0.0 and reason == "unsupported_direction"
    assert signal.payload["execution_check"]["reason"] == "unsupported_direction"


def test_non_binary_market_rejected() -> None:
    signal = _make_signal()
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(),
        signal=signal,
        market=_make_market(num_tokens=1),
        ob_analyzer=_ob_analyzer(),
    )
    assert opp is None and reason == "non_binary_market"
    assert signal.payload["execution_check"]["reason"] == "non_binary_market"


@pytest.mark.parametrize("best_ask", [None, 0.0, -0.1])
def test_missing_best_ask_rejected(best_ask) -> None:
    signal = _make_signal()
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(snapshot=_snap(best_ask=best_ask)),
    )
    assert opp is None and reason == "missing_best_ask"


def test_non_positive_notional_rejected() -> None:
    signal = _make_signal(recommended_size_usdc=0.0)
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert opp is None and reason == "non_positive_notional"


def test_insufficient_depth_rejected() -> None:
    signal = _make_signal()
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(executable=None),
    )
    assert opp is None and reason == "insufficient_depth"


def test_zero_fillable_size_rejected() -> None:
    signal = _make_signal()
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(executable=(0.5, 0.0)),
    )
    assert opp is None and reason == "zero_fillable_size"


def test_edge_below_fee_rejected() -> None:
    # gross_edge = 0.001 from deviation, fee at 50% mid ≈ fee_rate*0.25
    # With fee_rate=0.02 → fee ≈ 0.005 > 0.001
    signal = _make_signal(deviation=0.001, expected_edge=10.0)
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert opp is None and reason == "edge_below_fee"


def test_orderbook_feed_unhealthy_rejected_in_live_mode() -> None:
    signal = _make_signal()
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(dry_run=False),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(feed_health={"healthy": False, "reason": "stale_snapshot"}),
    )
    assert opp is None and reason == "orderbook_feed_unhealthy"
    assert signal.payload["execution_check"]["feed_health_reason"] == "stale_snapshot"


def test_orderbook_feed_health_skipped_in_dry_run() -> None:
    signal = _make_signal()
    # Even with feed_health returning unhealthy, dry_run=True should
    # short-circuit the health check and proceed to a successful build.
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(dry_run=True),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(feed_health={"healthy": False, "reason": "ws_down"}),
    )
    assert opp is not None and reason == ""


# ---------- happy path --------------------------------------------------------


def test_happy_path_builds_opportunity_with_diagnostic_payload() -> None:
    signal = _make_signal(deviation=0.05, recommended_size_usdc=1.0)
    opp, size, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == ""
    assert opp is not None
    assert size == pytest.approx(2.0)  # 1 USDC / 0.50 ask
    check = signal.payload["execution_check"]
    assert check["reason"] == ""
    assert check["fee_model"] == "clob_binary_fee_rate_x_price_x_1_minus_price"
    assert opp.legs[0].outcome == "Yes"
    assert opp.legs[0].token_id == "yes-1"


def test_snapshot_age_is_none_when_snapshot_timestamp_missing() -> None:
    signal = _make_signal(deviation=0.05, recommended_size_usdc=1.0)
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(snapshot=_snap(timestamp=0.0)),
    )
    assert opp is not None and reason == ""
    assert signal.payload["execution_check"]["snapshot_age_sec"] is None


def test_happy_path_buy_no_picks_no_token() -> None:
    signal = _make_signal(action="BUY_NO", deviation=0.05)
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == ""
    assert opp is not None
    assert opp.legs[0].outcome == "No"
    assert opp.legs[0].token_id == "no-1"


# ---------- UPDOWN (T2 Phase 2) directions ----------------------------------


def _make_updown_market() -> MarketInfo:
    return MarketInfo(
        condition_id="cond-1",
        question="Bitcoin Up or Down - 8:00PM-8:15PM ET",
        slug="btc-updown-15m-1780272000",
        tokens=[TokenInfo("up-1", "Up"), TokenInfo("down-1", "Down")],
    )


def _make_updown_signal(action: str, token_id: str, deviation: float = 0.30) -> StrategySignal:
    return StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type=action.lower(),
        market_id="cond-1",
        description="updown test",
        expected_edge=deviation * 10_000.0,
        confidence=0.62,
        recommended_size_usdc=1.0,
        payload={"action": action, "token_id": token_id, "deviation": deviation},
    )


def test_updown_buy_up_resolves_up_token_by_id() -> None:
    signal = _make_updown_signal("BUY_UP", "up-1")
    opp, size, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_updown_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == "" and opp is not None
    assert opp.legs[0].outcome == "Up"
    assert opp.legs[0].token_id == "up-1"


def test_updown_buy_down_resolves_down_token_by_id() -> None:
    signal = _make_updown_signal("BUY_DOWN", "down-1")
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_updown_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == "" and opp is not None
    assert opp.legs[0].outcome == "Down"
    assert opp.legs[0].token_id == "down-1"


def test_updown_falls_back_to_outcome_label_when_token_id_absent() -> None:
    signal = _make_updown_signal("BUY_UP", token_id="")  # no explicit id
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_updown_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == "" and opp is not None
    assert opp.legs[0].token_id == "up-1"  # resolved by "up" outcome label


def test_updown_token_unresolved_when_no_match() -> None:
    signal = _make_updown_signal("BUY_UP", token_id="ghost")
    # market has no "up" outcome token to fall back to
    market = MarketInfo(
        condition_id="cond-1", question="q", slug="btc-updown-15m-1",
        tokens=[TokenInfo("a", "Foo"), TokenInfo("b", "Bar")],
    )
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(),
        signal=signal,
        market=market,
        ob_analyzer=_ob_analyzer(),
    )
    assert opp is None and reason == "updown_token_unresolved"


# ---------- gross-edge anchored to fill price (Bug #3) -----------------------


def test_gross_edge_anchored_to_ask_with_model_prob() -> None:
    # model_prob (YES fair) = 0.55, fill price = 0.50 -> ask-anchored edge
    # is 0.05, NOT the inflated mid-based deviation of 0.06.
    signal = _make_signal(action="BUY_YES", deviation=0.06)
    signal.payload["model_prob"] = 0.55
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == "" and opp is not None
    assert signal.payload["execution_check"]["gross_edge"] == pytest.approx(0.05)


def test_buy_no_uses_complement_model_prob() -> None:
    # Standard T2 stores YES fair; a BUY_NO must price the NO token at the
    # complement (1 - 0.30 = 0.70). Fill at 0.50 -> gross edge 0.20.
    signal = _make_signal(action="BUY_NO", deviation=0.06)
    signal.payload["model_prob"] = 0.30
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == "" and opp is not None
    assert opp.legs[0].outcome == "No"
    assert signal.payload["execution_check"]["gross_edge"] == pytest.approx(0.20)


def test_updown_model_prob_not_complemented() -> None:
    # UPDOWN stores the chosen side's fair value -> no complement.
    # model_prob 0.62, fill 0.50 -> gross edge 0.12.
    signal = _make_updown_signal("BUY_UP", token_id="up-1")
    signal.payload["model_prob"] = 0.62
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_updown_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == "" and opp is not None
    assert signal.payload["execution_check"]["gross_edge"] == pytest.approx(0.12)


def test_legacy_deviation_path_unchanged() -> None:
    # No model_prob in payload -> legacy abs(deviation) behaviour preserved.
    signal = _make_signal(action="BUY_YES", deviation=0.05)
    assert "model_prob" not in signal.payload
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert reason == "" and opp is not None
    assert signal.payload["execution_check"]["gross_edge"] == pytest.approx(0.05)


def test_model_prob_below_fill_rejected_as_edge_below_fee() -> None:
    # fair (0.50) == fill (0.50) -> gross edge 0, net edge < 0 once fee
    # applies -> rejected before building an opportunity.
    signal = _make_signal(action="BUY_YES", deviation=0.10)
    signal.payload["model_prob"] = 0.50
    opp, _, reason = build_directional_opportunity_from_signal(
        config=_make_config(polymarket_taker_fee_rate=0.02),
        signal=signal,
        market=_make_market(),
        ob_analyzer=_ob_analyzer(),
    )
    assert opp is None and reason == "edge_below_fee"

