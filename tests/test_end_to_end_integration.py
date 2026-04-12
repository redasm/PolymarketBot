"""End-to-end dry-run integration flow."""

from __future__ import annotations

from polymarket_arb.arbitrage_detector import ArbitrageDetector
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.models import MarketInfo, OrderBookLevel, OrderBookSnapshot, TokenInfo
from polymarket_arb.risk_manager import RiskManager
from tests.conftest import MockOrderBookAnalyzer, make_test_config


def _make_snapshot(token_id: str, best_bid: float, best_ask: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bids=[OrderBookLevel(best_bid, 100)],
        asks=[OrderBookLevel(best_ask, 100)],
    )


def test_end_to_end_dry_run_binary_arb_flow():
    config = make_test_config(dry_run=True)
    snapshots = {
        "yes-token": _make_snapshot(token_id="yes-token", best_bid=0.44, best_ask=0.45),
        "no-token": _make_snapshot(token_id="no-token", best_bid=0.49, best_ask=0.50),
    }
    market = MarketInfo(
        condition_id="c1",
        question="Will BTC go up?",
        slug="btc-up",
        tokens=[
            TokenInfo(token_id="yes-token", outcome="Yes"),
            TokenInfo(token_id="no-token", outcome="No"),
        ],
        active=True,
        closed=False,
        event_id="e1",
    )

    detector = ArbitrageDetector(config, MockOrderBookAnalyzer(snapshots))
    risk_mgr = RiskManager(config)
    executor = ExecutionEngine(config, trading_client=object())

    opp = detector.scan_binary_market(market)
    assert opp is not None

    verified = detector.verify_opportunity_with_depth(opp, target_size=5)
    assert verified is not None

    can_trade, reason, size = risk_mgr.pre_trade_check(verified, proposed_size=5)
    assert can_trade is True
    assert reason == ""

    trades = executor.execute_arbitrage(verified, size)
    assert len(trades) == 2
    assert all(trade.simulated is True for trade in trades)
