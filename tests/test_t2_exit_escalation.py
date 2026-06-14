"""Regression: T2 exit escalation ladder (floor-dump + abandon).

Bug context: a stuck T2 position with a tick_size price bug looped 21+
FAK retries over 14 hours, eating the only RISK_MAX_OPEN_POSITIONS=1
slot and starving every other strategy. Even with the price bug fixed
the same deadlock can recur on a market whose bid simply evaporates.
These tests pin the escape hatch: after _FLOOR_EXIT_AFTER failures we
switch to a $0.01 FAK; after _ABANDON_AFTER failures we release the
risk exposure and drop the in-memory position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from polymarket_arb.models import (
    ArbOpportunity,
    MarketInfo,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderSide,
    PositionSnapshot,
    TokenInfo,
    TradeRecord,
    TradeStatus,
)
from polymarket_arb.strategies.t2_exit_manager import (
    T2ExitManager,
    _ABANDON_AFTER,
    _FLOOR_EXIT_AFTER,
    _FLOOR_PRICE,
)

from tests.conftest import make_test_config


class _StubOB:
    def __init__(self, snapshots: dict[str, OrderBookSnapshot]):
        self._snaps = snapshots

    def get_snapshot(self, token_id: str):
        return self._snaps.get(token_id)


@dataclass
class _FakeRiskManager:
    releases: list[tuple[str, float]] = field(default_factory=list)

    def release_market_exposure(self, condition_id: str, exposure: float) -> None:
        self.releases.append((condition_id, exposure))


class _AlwaysFailExecutor:
    """Every SELL fails — simulates a market with no bid at all price levels."""

    def __init__(self):
        self.calls: list[tuple[float, float]] = []  # (price, size)

    def execute_arbitrage(self, opp: ArbOpportunity, size: float, *, order_type_name: str | None = None, **_kwargs: Any) -> list[TradeRecord]:
        leg = opp.legs[0]
        self.calls.append((float(leg.price), float(size)))
        return [
            TradeRecord(
                trade_id="exit",
                arb_id="exit",
                token_id=leg.token_id,
                condition_id=leg.condition_id,
                side=leg.side,
                price=leg.price,
                size=size,
                status=TradeStatus.FAILED,
                error="fok_not_filled",
            )
        ]

    def is_successful_execution(self, _opp: ArbOpportunity, _trades: list[TradeRecord]) -> bool:
        return False


def _market() -> MarketInfo:
    return MarketInfo(
        condition_id="c1",
        question="Will BTC hit $1m before GTA VI?",
        slug="btc",
        tokens=[
            TokenInfo(token_id="t-yes", outcome="Yes", price=0.51),
            TokenInfo(token_id="t-no", outcome="No", price=0.49),
        ],
    )


def _snap(best_bid: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="t-yes",
        best_bid=best_bid,
        best_ask=best_bid + 0.01,
        bids=[OrderBookLevel(best_bid, 100)],
        asks=[OrderBookLevel(best_bid + 0.01, 100)],
    )


def _fill() -> TradeRecord:
    return TradeRecord(
        trade_id="t",
        arb_id="a",
        token_id="t-yes",
        condition_id="c1",
        side=OrderSide.BUY,
        price=0.51,
        size=15.0,
        status=TradeStatus.FILLED,
        fill_price=0.51,
        fill_size=15.0,
    )


def _make_manager(*, executor, risk_manager):
    cfg = make_test_config(
        t2_stop_loss_bps=10.0,  # tiny threshold so stop-loss fires immediately
        t2_take_profit_capture_pct=10.0,  # effectively disabled
        t2_max_hold_sec=1.0,  # short → time_stop also fires
        t2_optimal_stopping_enabled=False,
        t2_exit_eval_interval_sec=0.0,
    )
    ob = _StubOB({"t-yes": _snap(best_bid=0.50)})
    mgr = T2ExitManager(
        config=cfg,
        executor=executor,
        ob_analyzer=ob,
        risk_manager=risk_manager,
    )
    mgr.register_fills(
        signal_payload={"action": "BUY_YES", "model_prob": 0.55, "deviation": 0.04},
        market=_market(),
        trades=[_fill()],
    )
    # Force the eval gate open between successive evaluate() calls.
    for pos in mgr._positions.values():
        pos.entry_ts = 0.0
    return mgr


def _run_failures(mgr, executor, n: int) -> None:
    """Call evaluate() ``n`` times, clearing retry backoff each pass."""
    for _ in range(n):
        for pos in mgr._positions.values():
            pos.next_exit_retry_ts = 0.0
            pos.last_eval_ts = 0.0
        mgr.evaluate(active_markets=[_market()])


class TestFloorDumpEscalation:
    """After _FLOOR_EXIT_AFTER failures, exit price flips to $0.01."""

    def test_pre_floor_uses_real_bid(self):
        executor = _AlwaysFailExecutor()
        mgr = _make_manager(executor=executor, risk_manager=_FakeRiskManager())
        _run_failures(mgr, executor, 3)
        # First few attempts target the moving bid (here best_bid=0.50).
        assert all(price == 0.50 for price, _ in executor.calls[:3])

    def test_floor_dump_engages_after_threshold(self):
        executor = _AlwaysFailExecutor()
        mgr = _make_manager(executor=executor, risk_manager=_FakeRiskManager())
        _run_failures(mgr, executor, _FLOOR_EXIT_AFTER + 2)
        # Attempts at and beyond _FLOOR_EXIT_AFTER should be at floor price.
        floor_attempts = [price for price, _ in executor.calls[_FLOOR_EXIT_AFTER:]]
        assert floor_attempts  # we ran past the threshold
        assert all(price == _FLOOR_PRICE for price in floor_attempts)


class TestAbandon:
    """After _ABANDON_AFTER failures, position is dropped and exposure released."""

    def test_abandon_releases_exposure_and_drops_position(self):
        executor = _AlwaysFailExecutor()
        risk_mgr = _FakeRiskManager()
        mgr = _make_manager(executor=executor, risk_manager=risk_mgr)
        _run_failures(mgr, executor, _ABANDON_AFTER + 1)
        # Position no longer tracked in-memory.
        assert mgr._positions == {}
        # Exposure release recorded against the right market.
        assert any(cond_id == "c1" and exposure > 0 for cond_id, exposure in risk_mgr.releases)

    def test_abandon_not_triggered_below_threshold(self):
        executor = _AlwaysFailExecutor()
        risk_mgr = _FakeRiskManager()
        mgr = _make_manager(executor=executor, risk_manager=risk_mgr)
        _run_failures(mgr, executor, _FLOOR_EXIT_AFTER + 2)
        # Position should still be tracked — we haven't hit the abandon threshold.
        assert mgr._positions != {}
        # No release call has fired yet (floor dump is still trying).
        assert risk_mgr.releases == []


class TestReconcileWithChain:
    """BUG-B: abandoned positions still on-chain must be re-adopted."""

    def _abandoned_manager(self):
        from polymarket_arb.strategies.t2_exit_manager import _ABANDON_AFTER as _AB
        executor = _AlwaysFailExecutor()
        risk_mgr = _FakeRiskManager()
        mgr = _make_manager(executor=executor, risk_manager=risk_mgr)
        _run_failures(mgr, executor, _AB + 1)
        assert mgr._positions == {}  # abandoned
        assert "t-yes" in mgr._abandoned
        return mgr, executor

    def _chain_pos(self, size: float):
        return PositionSnapshot(
            token_id="t-yes", condition_id="c1", outcome="Yes",
            size=size, avg_price=0.51,
        )

    def test_readopts_position_still_on_chain(self):
        mgr, _ = self._abandoned_manager()
        n = mgr.reconcile_with_chain([self._chain_pos(15.0)])
        assert n == 1
        assert "t-yes" in mgr._positions
        assert "t-yes" not in mgr._abandoned
        pos = mgr._positions["t-yes"]
        assert pos.abandoned is False
        assert pos.exit_failure_count == 0
        assert pos.readopt_count == 1

    def test_prunes_position_gone_from_chain(self):
        mgr, _ = self._abandoned_manager()
        n = mgr.reconcile_with_chain([])  # settled / closed off-chain
        assert n == 0
        assert "t-yes" not in mgr._positions
        assert "t-yes" not in mgr._abandoned

    def test_syncs_size_to_chain_truth(self):
        mgr, _ = self._abandoned_manager()
        # A partial exit landed before giving up: chain shows only 6 left.
        mgr.reconcile_with_chain([self._chain_pos(6.0)])
        assert mgr._positions["t-yes"].size_remaining == pytest.approx(6.0)

    def test_readopt_capped(self):
        from polymarket_arb.strategies.t2_exit_manager import _MAX_READOPT, _ABANDON_AFTER as _AB
        mgr, executor = self._abandoned_manager()
        # Cycle abandon -> readopt repeatedly; after _MAX_READOPT it stays put.
        for _ in range(_MAX_READOPT):
            assert mgr.reconcile_with_chain([self._chain_pos(15.0)]) == 1
            _run_failures(mgr, executor, _AB + 1)  # fails again -> re-abandoned
        # Now at the cap: further reconciles must NOT re-adopt.
        assert mgr.reconcile_with_chain([self._chain_pos(15.0)]) == 0
        assert "t-yes" in mgr._abandoned
        assert "t-yes" not in mgr._positions
