"""Tests for `polymarket_arb.main_helpers.signal_helpers`.

These small helpers used to live inline in `main_loop.py`. Pinning their
contract here so future refactors of the strategy / signal pipeline
can't silently break the assumptions the orchestrator + dashboard rely
on (e.g. `execution_check` payload shape, T2 quality gate names).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from polymarket_arb.main_helpers.signal_helpers import (
    apply_maker_fill_to_inventory,
    evaluate_t2_market_quality,
    extract_market_deadline,
    extract_market_temporal_stem,
    find_market_for_signal,
    resolve_strategy_signal_action,
    set_signal_execution_check,
    spread_bps_from_snapshot,
    sum_trade_exposure,
)
from polymarket_arb.models import MarketInfo, OrderSide, TokenInfo, TradeRecord, TradeStatus
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier
from tests.conftest import make_test_config


def _signal(**kwargs) -> StrategySignal:
    defaults = {
        "tier": StrategyTier.STATISTICAL_ARB,
        "signal_type": "t2_buy",
        "market_id": "c1",
        "description": "x",
        "expected_edge": 0.04,
        "confidence": 0.6,
        "recommended_size_usdc": 25.0,
        "urgency": 0.3,
        "payload": {},
    }
    defaults.update(kwargs)
    return StrategySignal(**defaults)


def test_extract_market_temporal_stem_strips_will_by_and_punctuation():
    assert extract_market_temporal_stem("Will BTC > $100k by Jan 1, 2026?") == "btc > $100k"
    assert extract_market_temporal_stem("ETH price ABOVE 5000 before Mar 1, 2027?") == "eth price above 5000"
    assert extract_market_temporal_stem("") == ""
    assert extract_market_temporal_stem("Will it rain ?") == "it rain"


def test_extract_market_deadline_parses_canonical_form():
    assert extract_market_deadline("Will BTC > $100k by January 1, 2026?") == datetime(2026, 1, 1)
    assert extract_market_deadline("ETH > 5000 before March 31, 2027") == datetime(2027, 3, 31)


def test_extract_market_deadline_returns_none_for_invalid():
    assert extract_market_deadline("") is None
    assert extract_market_deadline("Will Lakers win?") is None
    # invalid date
    assert extract_market_deadline("Will X by February 31, 2026?") is None


def test_spread_bps_from_snapshot_handles_missing_data():
    assert spread_bps_from_snapshot(None) is None
    no_bid = SimpleNamespace(best_bid=None, best_ask=0.5, mid=0.5, spread=None)
    assert spread_bps_from_snapshot(no_bid) is None


def test_spread_bps_from_snapshot_computes_when_spread_missing():
    snap = SimpleNamespace(best_bid=0.49, best_ask=0.51, mid=0.5, spread=None)
    bps = spread_bps_from_snapshot(snap)
    # (0.02 / 0.5) * 10_000 = 400 — `pytest.approx` because float subtraction
    # of 0.51 - 0.49 leaves trailing fp noise.
    assert bps == pytest.approx(400.0)


def test_spread_bps_from_snapshot_uses_explicit_spread_if_present():
    snap = SimpleNamespace(best_bid=0.49, best_ask=0.51, mid=0.5, spread=0.02)
    assert spread_bps_from_snapshot(snap) == pytest.approx(400.0)


def test_evaluate_t2_market_quality_passes_when_all_gates_ok():
    cfg = make_test_config(
        t2_max_spread_bps=500.0,
        t2_min_top_depth=10.0,
        t2_max_complement_error_bps=200.0,
    )
    yes = SimpleNamespace(best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02, best_ask_size=20.0)
    no = SimpleNamespace(best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02, best_ask_size=20.0)
    out = evaluate_t2_market_quality(config=cfg, snap=yes, no_snap=no)
    assert out["passes"] is True
    assert out["reasons"] == []
    assert out["yes_spread_bps"] == 400.0
    assert out["complement_error_bps"] == 0.0


def test_evaluate_t2_market_quality_flags_each_gate_independently():
    cfg = make_test_config(
        t2_max_spread_bps=100.0,
        t2_min_top_depth=50.0,
        t2_max_complement_error_bps=10.0,
    )
    yes = SimpleNamespace(best_bid=0.45, best_ask=0.55, mid=0.50, spread=0.10, best_ask_size=5.0)
    no = SimpleNamespace(best_bid=0.45, best_ask=0.55, mid=0.50, spread=0.10, best_ask_size=5.0)
    out = evaluate_t2_market_quality(config=cfg, snap=yes, no_snap=no)
    assert out["passes"] is False
    assert "spread_too_wide" in out["reasons"]
    assert "top_depth_too_low" in out["reasons"]
    # yes 0.50 + no 0.50 = 1.00 -> complement_error 0 bps, so that gate is fine.
    assert "complement_error_too_high" not in out["reasons"]


def test_evaluate_t2_market_quality_flags_complement_error():
    cfg = make_test_config(t2_max_complement_error_bps=10.0)
    yes = SimpleNamespace(best_bid=0.49, best_ask=0.51, mid=0.55, spread=0.02, best_ask_size=100.0)
    no = SimpleNamespace(best_bid=0.49, best_ask=0.51, mid=0.55, spread=0.02, best_ask_size=100.0)
    out = evaluate_t2_market_quality(config=cfg, snap=yes, no_snap=no)
    assert "complement_error_too_high" in out["reasons"]


def test_resolve_strategy_signal_action_explicit_payload_wins():
    sig = _signal(payload={"action": "buy_yes"}, signal_type="ignored")
    assert resolve_strategy_signal_action(sig) == "BUY_YES"


def test_resolve_strategy_signal_action_inferred_from_signal_type():
    assert resolve_strategy_signal_action(_signal(signal_type="t2_BUY_YES")) == "BUY_YES"
    assert resolve_strategy_signal_action(_signal(signal_type="anything_BUY_NO")) == "BUY_NO"
    assert resolve_strategy_signal_action(_signal(signal_type="t2_SELL_YES")) == "SELL_YES"
    assert resolve_strategy_signal_action(_signal(signal_type="t2_SELL_NO")) == "SELL_NO"
    assert resolve_strategy_signal_action(_signal(signal_type="t2_other")) == ""


def _make_market(cid: str) -> MarketInfo:
    return MarketInfo(
        condition_id=cid,
        question="?",
        slug="s",
        tokens=[TokenInfo(token_id=f"{cid}-yes", outcome="Yes")],
    )


def test_find_market_for_signal_exact_match():
    markets = [_make_market("aaaaaaaa1"), _make_market("bbbbbbbb2")]
    assert find_market_for_signal("aaaaaaaa1", markets).condition_id == "aaaaaaaa1"


def test_find_market_for_signal_prefix_match_truncated_id():
    markets = [_make_market("0xabcdef1234567890")]
    # Truncated id (>=8 chars) should still resolve via prefix.
    assert find_market_for_signal("0xabcdef", markets).condition_id == "0xabcdef1234567890"


def test_find_market_for_signal_short_id_does_not_match():
    markets = [_make_market("0xabcdef1234567890")]
    # < 8 chars must NOT collide with random markets.
    assert find_market_for_signal("0xab", markets) is None


def test_find_market_for_signal_returns_none_when_missing():
    assert find_market_for_signal("nope", [_make_market("zzzzzzzz")]) is None


def test_set_signal_execution_check_payload_shape():
    sig = _signal()
    set_signal_execution_check(sig, reason="too_thin", spread_bps=412.0)
    chk = sig.payload["execution_check"]
    assert chk["reason"] == "too_thin"
    assert chk["spread_bps"] == 412.0
    assert "checked_at" in chk


def test_sum_trade_exposure_combines_legs_with_fallbacks():
    trades = [
        SimpleNamespace(
            economic_cost=0.45, fill_size=10.0, simulated=False, price=0.40, size=10.0,
        ),
        SimpleNamespace(
            economic_cost=None, fill_size=None, simulated=False, price=0.50, size=4.0,
        ),  # falls back to price * size
    ]
    assert sum_trade_exposure(trades) == 0.45 * 10.0 + 0.50 * 4.0


def test_sum_trade_exposure_can_skip_simulated():
    trades = [
        SimpleNamespace(
            economic_cost=0.45, fill_size=10.0, simulated=True, price=0.40, size=10.0,
        ),
        SimpleNamespace(
            economic_cost=0.50, fill_size=4.0, simulated=False, price=0.50, size=4.0,
        ),
    ]
    assert sum_trade_exposure(trades, include_simulated=False) == 0.50 * 4.0


def test_apply_maker_fill_to_inventory_idempotent():
    class _Maker:
        def __init__(self):
            self.calls: list[tuple[str, str, float]] = []

        def update_inventory(self, token_id: str, side: str, delta: float) -> None:
            self.calls.append((token_id, side, delta))

    maker = _Maker()
    trade = TradeRecord(
        trade_id="t",
        arb_id="a",
        token_id="tk",
        condition_id="c",
        side=OrderSide.BUY,
        price=0.5,
        size=10.0,
        status=TradeStatus.PARTIAL,
        fill_size=4.0,
        post_only=True,
    )
    delta = apply_maker_fill_to_inventory(maker, trade)
    assert delta == 4.0
    assert maker.calls == [("tk", "BUY", 4.0)]
    assert trade.inventory_accounted_size == 4.0

    # second call after the same fill — no double counting.
    delta2 = apply_maker_fill_to_inventory(maker, trade)
    assert delta2 == 0.0
    assert maker.calls == [("tk", "BUY", 4.0)]

    # increment fill, expect only the diff to be applied.
    trade.fill_size = 7.0
    delta3 = apply_maker_fill_to_inventory(maker, trade)
    assert delta3 == 3.0
    assert maker.calls[-1] == ("tk", "BUY", 3.0)
    assert trade.inventory_accounted_size == 7.0


def test_apply_maker_fill_to_inventory_skips_non_post_only():
    class _Maker:
        def update_inventory(self, *args):
            raise AssertionError("must not be called for non-post-only trades")

    trade = TradeRecord(
        trade_id="t",
        arb_id="a",
        token_id="tk",
        condition_id="c",
        side=OrderSide.SELL,
        price=0.5,
        size=10.0,
        status=TradeStatus.FILLED,
        fill_size=10.0,
        post_only=False,
    )
    assert apply_maker_fill_to_inventory(_Maker(), trade) == 0.0
