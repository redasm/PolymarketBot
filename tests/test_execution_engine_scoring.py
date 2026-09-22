"""ExecutionEngine 的 /orders-scoring 与按 id 撤单路径."""

from __future__ import annotations

import pytest

from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.models import OrderSide, TradeRecord, TradeStatus
from tests.conftest import make_test_config


class _ScoringClient:
    def __init__(self, result=None, *, error=None, supports_scoring=True):
        self._result = result if result is not None else {}
        self._error = error
        self.batches: list[list[str]] = []
        self.cancelled: list[str] = []
        if not supports_scoring:
            # 老客户端没有这个方法时必须整体跳过而不是崩。
            del self.are_orders_scoring

    def are_orders_scoring(self, params):
        ids = list(params.orderIds)
        self.batches.append(ids)
        if self._error is not None:
            raise self._error
        return {"data": {oid: self._result.get(oid, False) for oid in ids}}

    def cancel(self, order_id):
        self.cancelled.append(order_id)


class _NoScoringClient:
    def cancel(self, order_id):  # pragma: no cover - never reached
        pass


def _engine(client, **cfg):
    base = dict(dry_run=False, live_trading_ack=True)
    base.update(cfg)
    return ExecutionEngine(make_test_config(**base), client)


def _trade(order_id, *, post_only=True, order_type="GTC", status=TradeStatus.PENDING):
    return TradeRecord(
        trade_id=f"t-{order_id}",
        arb_id="a",
        token_id="tok",
        condition_id="cond",
        side=OrderSide.BUY,
        price=0.5,
        size=10.0,
        status=status,
        order_id=order_id,
        post_only=post_only,
        order_type_name=order_type,
    )


# --------- live_maker_trades ----------


def test_live_maker_trades_only_returns_resting_post_only_gtc():
    engine = _engine(_ScoringClient())
    engine._trade_history.extend(
        [
            _trade("o1"),
            _trade("o2", post_only=False),
            _trade("o3", order_type="FOK"),
            _trade("o4", status=TradeStatus.FILLED),
            _trade("o5", status=TradeStatus.PARTIAL),
        ]
    )
    assert [t.order_id for t in engine.live_maker_trades()] == ["o1", "o5"]


def test_live_maker_trades_skips_simulated_and_id_less():
    engine = _engine(_ScoringClient())
    simulated = _trade("o1")
    simulated.simulated = True
    engine._trade_history.extend([simulated, _trade(None)])
    assert engine.live_maker_trades() == []


# --------- check_orders_scoring ----------


def test_check_orders_scoring_maps_ids():
    client = _ScoringClient({"o1": True})
    engine = _engine(client)
    assert engine.check_orders_scoring(["o1", "o2"]) == {"o1": True, "o2": False}


def test_check_orders_scoring_is_noop_in_dry_run():
    client = _ScoringClient({"o1": True})
    engine = _engine(client, dry_run=True)
    assert engine.check_orders_scoring(["o1"]) == {}
    assert client.batches == []


def test_check_orders_scoring_handles_client_without_support():
    engine = _engine(_NoScoringClient())
    assert engine.check_orders_scoring(["o1"]) == {}


def test_check_orders_scoring_failure_returns_unknown_not_false():
    """批次失败必须留成"未知"（缺 key），不能退化成"未计分"."""
    engine = _engine(_ScoringClient(error=RuntimeError("boom")))
    assert engine.check_orders_scoring(["o1", "o2"]) == {}


def test_check_orders_scoring_chunks_large_batches():
    ids = [f"o{i}" for i in range(0, 185)]
    client = _ScoringClient({oid: True for oid in ids})
    engine = _engine(client)
    out = engine.check_orders_scoring(ids)
    assert len(out) == 185
    assert [len(batch) for batch in client.batches] == [80, 80, 25]


def test_check_orders_scoring_ignores_blank_ids():
    client = _ScoringClient()
    engine = _engine(client)
    assert engine.check_orders_scoring([None, ""]) == {}
    assert client.batches == []


# --------- cancel_maker_orders_by_id ----------


def test_cancel_by_id_only_touches_matching_resting_makers():
    client = _ScoringClient()
    engine = _engine(client)
    engine._trade_history.extend([_trade("o1"), _trade("o2"), _trade("o3", post_only=False)])
    cancelled = engine.cancel_maker_orders_by_id(["o1", "o3"], reason="not_scoring")
    assert [t.order_id for t in cancelled] == ["o1"]
    assert client.cancelled == ["o1"]
    assert cancelled[0].status is TradeStatus.CANCELLED
    assert cancelled[0].error == "not_scoring"


def test_cancel_by_id_is_noop_in_dry_run():
    client = _ScoringClient()
    engine = _engine(client, dry_run=True)
    engine._trade_history.append(_trade("o1"))
    assert engine.cancel_maker_orders_by_id(["o1"], reason="x") == []
    assert client.cancelled == []


def test_cancel_by_id_skips_orders_that_fail_to_cancel():
    class _FailingCancel(_ScoringClient):
        def cancel(self, order_id):
            raise RuntimeError("venue down")

    engine = _engine(_FailingCancel())
    trade = _trade("o1")
    engine._trade_history.append(trade)
    assert engine.cancel_maker_orders_by_id(["o1"], reason="x") == []
    assert trade.status is TradeStatus.PENDING
