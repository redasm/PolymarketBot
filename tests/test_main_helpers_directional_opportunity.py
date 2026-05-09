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


def _snap(best_ask: float | None = 0.5, best_bid: float = 0.49):
    return SimpleNamespace(
        best_ask=best_ask,
        best_bid=best_bid,
        asks=[SimpleNamespace(price=best_ask or 0.0, size=100.0)],
        bids=[SimpleNamespace(price=best_bid, size=100.0)],
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
