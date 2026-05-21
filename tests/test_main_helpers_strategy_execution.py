"""Tests for `polymarket_arb.main_helpers.strategy_execution`.

The big happy-path coverage already lives in `tests/test_main_loop.py`
(via the `_execute_strategy_signal` underscore-aliased import). This
file pins the dispatcher's edge cases — early-return reasons, tier
routing, and `ExecutionDelta` shape — at the public-API surface so
the stable-identifier contract stays honest.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from polymarket_arb.dashboard_api import DashboardState
from polymarket_arb.main_helpers.strategy_execution import (
    ExecutionDelta,
    execute_strategy_signal,
)
from polymarket_arb.models import MarketInfo, TokenInfo, TradeRecord, TradeStatus
from polymarket_arb.strategies.maker_strategy import MakerStrategy
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)


# ---------- minimal stubs ----------------------------------------------------


class _Recorder:
    is_enabled = True

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write_event(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


class _DisabledRecorder:
    is_enabled = False

    def write_event(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("write_event must not be called when disabled")


class _Notifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def notify_trade_success(self, **kwargs):
        self.calls.append(("success", kwargs))

    def notify_trade_failure(self, **kwargs):
        self.calls.append(("failure", kwargs))


class _RiskMgrAcceptAll:
    def pre_trade_check(self, _opp, size):
        return True, "", size

    def record_execution(self, *_args, **_kwargs):
        pass


def _config(**overrides):
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


def _binary_market(condition_id: str = "cond-1") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question="Will X happen?",
        slug="will-x",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
    )


def _signal(
    *,
    tier: StrategyTier,
    market_id: str = "cond-1",
    payload: dict | None = None,
    signal_type: str = "test",
    recommended_size_usdc: float = 1.0,
) -> StrategySignal:
    return StrategySignal(
        tier=tier,
        signal_type=signal_type,
        market_id=market_id,
        description="t",
        expected_edge=100.0,
        confidence=0.5,
        recommended_size_usdc=recommended_size_usdc,
        payload=payload or {},
    )


def _run(
    signal: StrategySignal,
    *,
    active_markets: list[MarketInfo] | None = None,
    config=None,
    ob_analyzer=None,
    executor=None,
    risk_mgr=None,
    maker_strategy=None,
    event_recorder=None,
):
    return execute_strategy_signal(
        signal=signal,
        config=config or _config(),
        active_markets=active_markets if active_markets is not None else [_binary_market()],
        ob_analyzer=ob_analyzer or SimpleNamespace(),
        executor=executor or SimpleNamespace(),
        risk_mgr=risk_mgr or _RiskMgrAcceptAll(),
        orchestrator=StrategyOrchestrator(total_bankroll=10.0),
        dash_state=DashboardState(),
        event_recorder=event_recorder or _Recorder(),
        maker_strategy=maker_strategy or MakerStrategy(default_size=1.0),
        notifier=_Notifier(),
    )


# ---------- dispatcher edges -------------------------------------------------


def test_unknown_tier_returns_unsupported() -> None:
    fake_tier = SimpleNamespace(name="UNKNOWN")
    sig = _signal(tier=StrategyTier.STRUCTURAL_ARB)
    sig.tier = fake_tier
    success, reason, delta = _run(sig)
    assert success is False
    assert reason == "unsupported_strategy_tier"
    assert delta == ExecutionDelta()


# ---------- T1 (cross-platform) ----------------------------------------------


def test_cross_platform_live_mode_rejects() -> None:
    sig = _signal(tier=StrategyTier.CROSS_PLATFORM)
    success, reason, delta = _run(sig, config=_config(dry_run=False))
    assert success is False
    assert reason == "cross_platform_live_requires_external_executor"
    assert delta == ExecutionDelta()


def test_cross_platform_dry_run_records_simulated_pair() -> None:
    sig = _signal(
        tier=StrategyTier.CROSS_PLATFORM,
        signal_type="cross_platform_buy",
        recommended_size_usdc=10.0,
        payload={
            "total_cost": 0.50,
            "poly_cost": 0.30,
            "kalshi_cost": 0.20,
            "pair_id": "pair-A",
        },
    )
    rec = _Recorder()
    success, reason, delta = _run(sig, event_recorder=rec)
    assert success is True
    assert reason == ""
    assert delta.simulated_successes == 1
    # one strategy_executions event with trade_count=2
    assert any(
        kind == "strategy_executions" and payload["trade_count"] == 2
        for kind, payload in rec.events
    )


def test_cross_platform_does_not_write_when_recorder_disabled() -> None:
    sig = _signal(
        tier=StrategyTier.CROSS_PLATFORM,
        payload={"total_cost": 0.5, "poly_cost": 0.3, "kalshi_cost": 0.2, "pair_id": "p"},
    )
    success, _reason, _delta = _run(sig, event_recorder=_DisabledRecorder())
    assert success is True


# ---------- T2 (statistical arb) early returns -------------------------------


def test_statistical_arb_market_not_found() -> None:
    sig = _signal(
        tier=StrategyTier.STATISTICAL_ARB,
        market_id="missing-cond",
        payload={"action": "BUY_YES", "deviation": 0.05},
    )
    success, reason, delta = _run(sig, active_markets=[_binary_market("cond-1")])
    assert success is False
    assert reason == "market_not_found"
    assert delta == ExecutionDelta()
    # Diagnostic stamped on the signal so the dashboard can show it.
    assert sig.payload["execution_check"]["reason"] == "market_not_found"


def test_statistical_arb_propagates_build_failure_reason() -> None:
    # The build helper rejects with `unsupported_direction` when the
    # action isn't BUY_YES/BUY_NO. The dispatcher must surface that
    # reason verbatim.
    sig = _signal(
        tier=StrategyTier.STATISTICAL_ARB,
        payload={"action": "HOLD", "deviation": 0.01},
    )
    success, reason, delta = _run(sig)
    assert success is False
    assert reason == "unsupported_direction"
    assert delta == ExecutionDelta()


def test_statistical_arb_blocked_by_risk_manager() -> None:
    class _Reject:
        def pre_trade_check(self, _opp, _size):
            return False, "max_positions_reached", 0.0

        def record_execution(self, *_a, **_kw):
            raise AssertionError("must not record when pre-check failed")

    ob = SimpleNamespace(
        get_snapshot=lambda _t: SimpleNamespace(
            best_ask=0.5,
            best_bid=0.49,
            asks=[SimpleNamespace(price=0.5, size=100.0)],
            bids=[SimpleNamespace(price=0.49, size=100.0)],
        ),
        get_executable_ask_price=lambda _t, _s: (0.5, 2.0),
    )
    sig = _signal(
        tier=StrategyTier.STATISTICAL_ARB,
        payload={"action": "BUY_YES", "deviation": 0.05},
    )
    success, reason, _ = _run(sig, ob_analyzer=ob, risk_mgr=_Reject())
    assert success is False
    assert reason == "max_positions_reached"


def test_statistical_arb_blocked_by_collateral() -> None:
    class _Executor:
        def ensure_sufficient_collateral(self, _amount):
            return False, "insufficient_balance", None

        def execute_arbitrage(self, *_a, **_kw):
            raise AssertionError("must not execute when collateral check failed")

        def is_successful_execution(self, *_a, **_kw):
            return False

    ob = SimpleNamespace(
        get_snapshot=lambda _t: SimpleNamespace(
            best_ask=0.5,
            best_bid=0.49,
            asks=[SimpleNamespace(price=0.5, size=100.0)],
            bids=[SimpleNamespace(price=0.49, size=100.0)],
        ),
        get_executable_ask_price=lambda _t, _s: (0.5, 2.0),
    )
    sig = _signal(
        tier=StrategyTier.STATISTICAL_ARB,
        payload={"action": "BUY_YES", "deviation": 0.05},
    )
    success, reason, _ = _run(sig, ob_analyzer=ob, executor=_Executor())
    assert success is False
    assert reason == "insufficient_balance"


def test_wallet_alpha_candidate_shadow_signal_can_execute_in_dry_run() -> None:
    class _Executor:
        def ensure_sufficient_collateral(self, _amount):
            return True, "", None

        def execute_arbitrage(self, opp, size, **kwargs):
            assert kwargs["virtual_fill_context"]["wallet_address"] == "0xwallet"
            return [
                TradeRecord(
                    trade_id="trade-1",
                    arb_id="arb-1",
                    token_id=opp.legs[0].token_id,
                    condition_id=opp.legs[0].condition_id,
                    side=opp.legs[0].side,
                    price=opp.legs[0].price,
                    size=size,
                    status=TradeStatus.FILLED,
                    fill_price=opp.legs[0].price,
                    fill_size=size,
                    simulated=True,
                )
            ]

        def is_successful_execution(self, _opp, trades):
            return bool(trades)

    ob = SimpleNamespace(
        get_snapshot=lambda _t: SimpleNamespace(
            best_ask=0.5,
            best_bid=0.49,
            asks=[SimpleNamespace(price=0.5, size=100.0)],
            bids=[SimpleNamespace(price=0.49, size=100.0)],
        ),
        get_executable_ask_price=lambda _t, _s: (0.5, 2.0),
    )
    sig = _signal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="wallet_alpha_candidate_buy_yes",
        payload={
            "action": "BUY_YES",
            "deviation": 0.03,
            "wallet_address": "0xwallet",
            "wallet_profile_status": "candidate_unvalidated",
        },
    )

    success, reason, delta = _run(sig, ob_analyzer=ob, executor=_Executor())

    assert success is True
    assert reason == ""
    assert delta.simulated_successes == 1


# ---------- T3 (market making) early returns ---------------------------------


def test_maker_market_not_found() -> None:
    sig = _signal(tier=StrategyTier.MARKET_MAKING, market_id="missing")
    success, reason, _ = _run(sig, active_markets=[_binary_market("cond-1")])
    assert success is False
    assert reason == "market_not_found"


def test_maker_non_binary_market() -> None:
    one_token_market = MarketInfo(
        condition_id="cond-1",
        question="?",
        slug="s",
        tokens=[TokenInfo("only-yes", "Yes")],
    )
    sig = _signal(tier=StrategyTier.MARKET_MAKING)
    success, reason, _ = _run(sig, active_markets=[one_token_market])
    assert success is False
    assert reason == "non_binary_market"


def test_maker_no_executable_side_when_quote_missing() -> None:
    sig = _signal(
        tier=StrategyTier.MARKET_MAKING,
        payload={"quote": {"fair_value": 0.5, "bid_price": 0.0, "ask_price": 0.0}},
    )
    success, reason, _ = _run(sig)
    assert success is False
    assert reason == "maker_no_executable_side"


def test_maker_no_executable_side_when_quote_payload_not_dict() -> None:
    # Defensive against malformed signal.payload["quote"].
    sig = _signal(tier=StrategyTier.MARKET_MAKING, payload={"quote": "not-a-dict"})
    success, reason, _ = _run(sig)
    assert success is False
    assert reason == "maker_no_executable_side"


def test_maker_inventory_sell_releases_t3_orchestrator_exposure() -> None:
    class _Executor:
        def ensure_sufficient_collateral(self, _amount):
            raise AssertionError("sell inventory should not require collateral")

        def submit_limit_order(self, **kwargs):
            return TradeRecord(
                trade_id="trade-1",
                arb_id="arb-1",
                token_id=kwargs["token_id"],
                condition_id=kwargs["condition_id"],
                side=kwargs["side"],
                price=kwargs["price"],
                size=kwargs["size"],
                status=TradeStatus.FILLED,
                fill_price=kwargs["price"],
                fill_size=kwargs["size"],
                simulated=False,
                post_only=True,
                order_type_name="GTC",
                economic_cost=kwargs["price"],
            )

        def is_successful_execution(self, *_args, **_kwargs):
            return True

    class _Risk:
        def pre_trade_check(self, *_args, **_kwargs):
            raise AssertionError("sell inventory should not open new risk")

        def record_execution(self, *_args, **_kwargs):
            raise AssertionError("sell inventory should not increase exposure")

    maker_strategy = MakerStrategy(default_size=1.0)
    maker_strategy.update_inventory("yes-1", "BUY", 2.0)
    orchestrator = StrategyOrchestrator(total_bankroll=10.0)
    buy_signal = _signal(tier=StrategyTier.MARKET_MAKING)
    orchestrator.record_execution(buy_signal, success=True, exposure_amount_usdc=1.0)
    sig = _signal(
        tier=StrategyTier.MARKET_MAKING,
        payload={
            "quote": {
                "fair_value": 0.50,
                "bid_price": 0.49,
                "ask_price": 0.60,
                "bid_size": 1.0,
                "ask_size": 1.0,
            }
        },
    )

    success, reason, _delta = execute_strategy_signal(
        signal=sig,
        config=_config(dry_run=False),
        active_markets=[_binary_market()],
        ob_analyzer=SimpleNamespace(get_snapshot=lambda _token_id: SimpleNamespace(tick_size=0.01)),
        executor=_Executor(),
        risk_mgr=_Risk(),
        orchestrator=orchestrator,
        dash_state=DashboardState(),
        event_recorder=_Recorder(),
        maker_strategy=maker_strategy,
        notifier=_Notifier(),
    )

    assert success is True
    assert reason == ""
    assert orchestrator.get_status()["T3"]["current_exposure"] == pytest.approx(0.4)


# ---------- ExecutionDelta dataclass shape -----------------------------------


def test_execution_delta_defaults_to_zero() -> None:
    d = ExecutionDelta()
    assert d.theoretical_opportunities == 0
    assert d.live_successes == 0
    assert d.simulated_successes == 0
    assert d.live_submissions == 0
    assert d.simulated_submissions == 0
    assert d.live_profit_total == 0.0
    assert d.simulated_profit_total == 0.0


def test_execution_delta_equality_used_by_assertions() -> None:
    assert ExecutionDelta() == ExecutionDelta()
    assert ExecutionDelta(live_successes=1) != ExecutionDelta()
