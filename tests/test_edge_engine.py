"""EdgeEngine 单元测试：方向判定、veto 逻辑、置信度."""

import pytest

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.edge_engine import EdgeEngine
from polymarket_arb.utils_time import now_ms
from polymarket_arb.volatility_estimator import VolEstimator


def _seed_store(
    store: EnhancedBookStore,
    yes_bids: list[tuple[float, float]],
    yes_asks: list[tuple[float, float]],
    no_bids: list[tuple[float, float]],
    no_asks: list[tuple[float, float]],
) -> None:
    ts = now_ms()
    store.set_market("test_market", "0xyes", "0xno")
    store.set_connected(True)
    store.update_yes(yes_bids, yes_asks, ts)
    store.update_no(no_bids, no_asks, ts)


class TestEdgeEngineDirection:
    def test_buy_yes_when_yes_underpriced(self):
        """YES mid 低、spot_fair_up 高 → 应输出 BUY_YES."""
        store = EnhancedBookStore()
        _seed_store(
            store,
            yes_bids=[(0.38, 500)], yes_asks=[(0.42, 500)],
            no_bids=[(0.55, 500)], no_asks=[(0.60, 500)],
        )
        engine = EdgeEngine(min_edge_bps=50, min_depth=10, max_spread_bps=1000)
        decision = engine.evaluate(store, spot_fair_up=0.65)

        assert decision.direction == "BUY_YES"
        assert decision.edge_bps > 0
        assert decision.veto is False

    def test_buy_no_when_no_underpriced(self):
        """NO mid 低、spot_fair_up 高（即 fair_down 低）→ 应输出 BUY_NO."""
        store = EnhancedBookStore()
        _seed_store(
            store,
            yes_bids=[(0.70, 500)], yes_asks=[(0.73, 500)],
            no_bids=[(0.23, 500)], no_asks=[(0.25, 500)],
        )
        engine = EdgeEngine(min_edge_bps=50, min_depth=10, max_spread_bps=2000)
        decision = engine.evaluate(store, spot_fair_up=0.55)

        assert decision.direction == "BUY_NO"
        assert decision.edge_bps > 0

    def test_none_when_edge_below_threshold(self):
        """edge 不够大 → NONE."""
        store = EnhancedBookStore()
        _seed_store(
            store,
            yes_bids=[(0.49, 500)], yes_asks=[(0.51, 500)],
            no_bids=[(0.49, 500)], no_asks=[(0.51, 500)],
        )
        engine = EdgeEngine(min_edge_bps=500, min_depth=10)
        decision = engine.evaluate(store)
        assert decision.direction == "NONE"


class TestEdgeEngineVeto:
    def test_veto_on_disconnected(self):
        """WS 断开 → veto."""
        store = EnhancedBookStore()
        store.set_market("m1", "0xyes", "0xno")
        engine = EdgeEngine()
        decision = engine.evaluate(store)
        assert decision.veto is True
        assert "ws_disconnected" in decision.veto_reasons

    def test_veto_on_wide_spread(self):
        """spread 过宽 → veto."""
        store = EnhancedBookStore()
        _seed_store(
            store,
            yes_bids=[(0.20, 500)], yes_asks=[(0.80, 500)],
            no_bids=[(0.20, 500)], no_asks=[(0.80, 500)],
        )
        engine = EdgeEngine(min_edge_bps=10, max_spread_bps=100, min_depth=10)
        decision = engine.evaluate(store, spot_fair_up=0.90)
        assert decision.direction == "NONE"

    def test_veto_on_low_depth(self):
        """深度不足 → veto."""
        store = EnhancedBookStore()
        _seed_store(
            store,
            yes_bids=[(0.35, 1)], yes_asks=[(0.40, 1)],
            no_bids=[(0.55, 1)], no_asks=[(0.60, 1)],
        )
        engine = EdgeEngine(min_edge_bps=50, min_depth=100, max_spread_bps=2000)
        decision = engine.evaluate(store, spot_fair_up=0.70)
        assert decision.direction == "NONE"


class TestEdgeEngineConfidence:
    def test_confidence_between_0_and_1(self):
        store = EnhancedBookStore()
        _seed_store(
            store,
            yes_bids=[(0.38, 500)], yes_asks=[(0.42, 500)],
            no_bids=[(0.55, 500)], no_asks=[(0.60, 500)],
        )
        engine = EdgeEngine(min_edge_bps=50, min_depth=10, max_spread_bps=1000)
        decision = engine.evaluate(store, spot_fair_up=0.65)
        assert 0.0 <= decision.confidence <= 1.0

    def test_vol_spike_reduces_confidence(self):
        """波动率飙升 → 置信度应更低."""
        store = EnhancedBookStore()
        _seed_store(
            store,
            yes_bids=[(0.38, 500)], yes_asks=[(0.42, 500)],
            no_bids=[(0.55, 500)], no_asks=[(0.60, 500)],
        )

        vol_normal = VolEstimator(fast_minutes=5, slow_minutes=20, min_bars=3)
        vol_spike = VolEstimator(fast_minutes=5, slow_minutes=20, min_bars=3)

        base_closes = [100_000 + i * 10 for i in range(30)]
        spike_closes = list(base_closes)
        spike_closes[-3:] = [100_500, 101_200, 102_000]

        for i, px in enumerate(base_closes):
            vol_normal.update_1m_close(px, ts_ms=i * 60_000)
        for i, px in enumerate(spike_closes):
            vol_spike.update_1m_close(px, ts_ms=i * 60_000)

        engine = EdgeEngine(min_edge_bps=50, min_depth=10, max_spread_bps=1000, min_confidence=0.0)

        d_normal = engine.evaluate(store, vol_normal, spot_fair_up=0.65)
        d_spike = engine.evaluate(store, vol_spike, spot_fair_up=0.65)

        assert d_spike.confidence <= d_normal.confidence
