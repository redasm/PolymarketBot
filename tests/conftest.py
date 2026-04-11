"""共享 fixture：构造 mock 对象、常用测试数据."""

from __future__ import annotations

import pytest

from polymarket_arb.models import (
    MarketInfo,
    OrderBookLevel,
    OrderBookSnapshot,
    TokenInfo,
)


@pytest.fixture()
def make_snapshot():
    """工厂 fixture：快速构造 OrderBookSnapshot."""

    def _make(
        token_id: str = "0xabc",
        best_bid: float | None = 0.48,
        best_ask: float | None = 0.52,
        bids: list[tuple[float, float]] | None = None,
        asks: list[tuple[float, float]] | None = None,
    ) -> OrderBookSnapshot:
        bid_levels = [OrderBookLevel(p, s) for p, s in (bids or [(best_bid, 100)])] if best_bid is not None else []
        ask_levels = [OrderBookLevel(p, s) for p, s in (asks or [(best_ask, 100)])] if best_ask is not None else []
        return OrderBookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bid_levels,
            asks=ask_levels,
        )

    return _make


@pytest.fixture()
def binary_market(make_snapshot):
    """构造一个标准二元市场 + 关联的 OrderBookSnapshot 映射."""
    market = MarketInfo(
        condition_id="cond_1",
        question="Will it rain tomorrow?",
        slug="will-it-rain",
        tokens=[
            TokenInfo(token_id="0xyes", outcome="Yes", price=0.45),
            TokenInfo(token_id="0xno", outcome="No", price=0.50),
        ],
        active=True,
        closed=False,
        event_id="evt_1",
    )
    snapshots = {
        "0xyes": make_snapshot(token_id="0xyes", best_bid=0.44, best_ask=0.45),
        "0xno": make_snapshot(token_id="0xno", best_bid=0.49, best_ask=0.50),
    }
    return market, snapshots


class MockOrderBookAnalyzer:
    """用预置快照替代真实 CLOB 调用的 mock."""

    def __init__(self, snapshots: dict[str, OrderBookSnapshot]):
        self._snaps = snapshots

    def get_snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        return self._snaps.get(token_id)

    def get_executable_ask_price(self, token_id: str, target_size: float):
        snap = self._snaps.get(token_id)
        if snap is None or not snap.asks:
            return None
        total_cost = 0.0
        filled = 0.0
        for level in snap.asks:
            take = min(level.size, target_size - filled)
            total_cost += take * level.price
            filled += take
            if filled >= target_size - 1e-9:
                break
        if filled <= 0:
            return None
        return (total_cost / filled, filled)
