"""user 频道 WebSocket：消息归一化、去重、有界队列、成交落地."""

from __future__ import annotations

import json
import threading

import pytest

from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.models import OrderSide, TradeRecord, TradeStatus
from polymarket_arb.user_feed import (
    UserChannelFeed,
    UserOrderEvent,
    normalize_user_message,
    parse_user_messages,
)
from tests.conftest import make_test_config


def _feed(**kwargs) -> UserChannelFeed:
    base = dict(api_key="k", api_secret="s", api_passphrase="p")
    base.update(kwargs)
    return UserChannelFeed(**base)


# --------- 解析 ----------


def test_parse_accepts_object_and_array_frames():
    assert parse_user_messages('{"a": 1}') == [{"a": 1}]
    assert parse_user_messages('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]
    assert parse_user_messages("not json") == []
    assert parse_user_messages('"scalar"') == []


def test_normalize_order_message_carries_cumulative_matched():
    (event,) = normalize_user_message(
        {
            "event_type": "order",
            "type": "UPDATE",
            "id": "o1",
            "asset_id": "tok",
            "market": "cond",
            "side": "BUY",
            "price": "0.42",
            "original_size": "10",
            "size_matched": "4",
            "status": "LIVE",
        }
    )
    assert event.order_id == "o1"
    assert event.cumulative_matched == pytest.approx(4.0)
    assert event.incremental_matched is None
    assert event.token_id == "tok"
    assert event.condition_id == "cond"


def test_normalize_trade_message_emits_one_event_per_maker_order():
    events = normalize_user_message(
        {
            "event_type": "trade",
            "id": "tr1",
            "status": "MATCHED",
            "asset_id": "tok",
            "market": "cond",
            "price": "0.5",
            "size": "7",
            "taker_order_id": "taker-1",
            "maker_orders": [
                {"order_id": "m1", "matched_amount": "3", "price": "0.49"},
                {"order_id": "m2", "matched_amount": "4", "price": "0.50"},
            ],
        }
    )
    by_id = {e.order_id: e for e in events}
    assert set(by_id) == {"m1", "m2", "taker-1"}
    assert by_id["m1"].incremental_matched == pytest.approx(3.0)
    assert by_id["m1"].cumulative_matched is None
    assert by_id["taker-1"].incremental_matched == pytest.approx(7.0)


def test_normalize_ignores_unknown_message_types():
    assert normalize_user_message({"event_type": "book"}) == []
    assert normalize_user_message({}) == []


def test_normalize_order_without_id_is_dropped():
    assert normalize_user_message({"event_type": "order", "type": "UPDATE"}) == []


def test_cancellation_and_failure_flags():
    cancel = UserOrderEvent(order_id="o", event_type="CANCELLATION")
    assert cancel.is_cancel is True
    unmatched = UserOrderEvent(order_id="o", event_type="UPDATE", status="UNMATCHED")
    assert unmatched.is_cancel is True
    failed = UserOrderEvent(order_id="o", event_type="UPDATE", status="FAILED")
    assert failed.is_failure is True


# --------- 队列 / 去重 ----------


def test_frames_are_queued_and_drained_fifo():
    feed = _feed()
    feed._handle_frame(
        json.dumps({"event_type": "order", "type": "UPDATE", "id": "o1", "size_matched": "1"})
    )
    feed._handle_frame(
        json.dumps({"event_type": "order", "type": "UPDATE", "id": "o2", "size_matched": "2"})
    )
    events = feed.drain()
    assert [e.order_id for e in events] == ["o1", "o2"]
    assert feed.drain() == []


def test_duplicate_trade_events_are_dropped():
    """重连重放不能让增量成交被计两次."""
    feed = _feed()
    frame = json.dumps(
        {
            "event_type": "trade",
            "id": "tr1",
            "maker_orders": [{"order_id": "m1", "matched_amount": "3"}],
        }
    )
    feed._handle_frame(frame)
    feed._handle_frame(frame)
    assert len(feed.drain()) == 1
    assert feed.stats()["duplicates"] == 1


def test_queue_is_bounded_and_counts_drops():
    feed = _feed(queue_size=2)
    for i in range(4):
        feed._handle_frame(
            json.dumps(
                {"event_type": "order", "type": "UPDATE", "id": f"o{i}", "size_matched": str(i)}
            )
        )
    assert feed.pending_count() == 2
    assert feed.stats()["dropped"] == 2


def test_wake_event_is_set_on_new_event():
    wake = threading.Event()
    feed = _feed(wake_event=wake)
    feed._handle_frame(
        json.dumps({"event_type": "order", "type": "UPDATE", "id": "o1", "size_matched": "1"})
    )
    assert wake.is_set()


def test_bad_frames_do_not_break_the_stream():
    feed = _feed()
    feed._handle_frame("PING")
    feed._handle_frame("garbage")
    feed._handle_frame(b'{"event_type": "order", "type": "UPDATE", "id": "o1"}')
    assert [e.order_id for e in feed.drain()] == ["o1"]


def test_missing_credentials_refuses_to_start():
    feed = UserChannelFeed(api_key="", api_secret="s", api_passphrase="p")
    assert feed.has_credentials is False
    assert feed.start() is False


def test_subscription_payload_shape():
    feed = _feed(market_provider=lambda: ["c2", "c1", "c1", " "])
    payload = feed._subscription_payload(feed._current_markets())
    assert payload["type"] == "user"
    assert payload["auth"] == {"apiKey": "k", "secret": "s", "passphrase": "p"}
    assert payload["markets"] == ["c1", "c2"]


def test_market_provider_failure_degrades_to_empty():
    def _boom():
        raise RuntimeError("nope")

    feed = _feed(market_provider=_boom)
    assert feed._current_markets() == []


# --------- 落地到 TradeRecord ----------


def _engine(**cfg):
    base = dict(dry_run=False, live_trading_ack=True)
    base.update(cfg)
    return ExecutionEngine(make_test_config(**base), object())


def _trade(order_id="o1", size=10.0, status=TradeStatus.PENDING, fill=None):
    return TradeRecord(
        trade_id=f"t-{order_id}",
        arb_id="a",
        token_id="tok",
        condition_id="cond",
        side=OrderSide.BUY,
        price=0.5,
        size=size,
        status=status,
        order_id=order_id,
        fill_size=fill,
        post_only=True,
        order_type_name="GTC",
    )


def test_cumulative_match_marks_partial_then_filled():
    engine = _engine()
    trade = _trade()
    engine._trade_history.append(trade)

    engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="UPDATE", cumulative_matched=4.0, price=0.5)]
    )
    assert trade.status is TradeStatus.PARTIAL
    assert trade.fill_size == pytest.approx(4.0)

    result = engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="UPDATE", cumulative_matched=10.0, price=0.5)]
    )
    assert trade.status is TradeStatus.FILLED
    assert trade.fill_size == pytest.approx(10.0)
    assert result.changed == [trade]


def test_cumulative_match_never_goes_backwards():
    """乱序到达的旧事件不能把成交量改小."""
    engine = _engine()
    trade = _trade(fill=6.0)
    trade.status = TradeStatus.PARTIAL
    engine._trade_history.append(trade)
    engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="UPDATE", cumulative_matched=2.0)]
    )
    assert trade.fill_size == pytest.approx(6.0)


def test_incremental_match_accumulates_with_vwap():
    engine = _engine()
    trade = _trade()
    engine._trade_history.append(trade)
    engine.apply_user_channel_events(
        [
            UserOrderEvent(order_id="o1", event_type="TRADE", incremental_matched=4.0, price=0.40),
            UserOrderEvent(order_id="o1", event_type="TRADE", incremental_matched=4.0, price=0.60),
        ]
    )
    assert trade.fill_size == pytest.approx(8.0)
    assert trade.fill_price == pytest.approx(0.50)


def test_fill_size_is_clamped_to_requested_size():
    engine = _engine()
    trade = _trade(size=10.0)
    engine._trade_history.append(trade)
    engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="TRADE", incremental_matched=99.0, price=0.5)]
    )
    assert trade.fill_size == pytest.approx(10.0)
    assert trade.status is TradeStatus.FILLED


def test_cancellation_is_terminal_even_with_partial_fill():
    """部分成交后撤单必须落到 CANCELLED，否则敞口永远不释放."""
    engine = _engine()
    trade = _trade(fill=3.0)
    trade.status = TradeStatus.PARTIAL
    engine._trade_history.append(trade)
    engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="CANCELLATION")]
    )
    assert trade.status is TradeStatus.CANCELLED
    assert trade.fill_size == pytest.approx(3.0)


def test_failure_status_marks_trade_failed():
    engine = _engine()
    trade = _trade()
    engine._trade_history.append(trade)
    engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="UPDATE", status="FAILED")]
    )
    assert trade.status is TradeStatus.FAILED


def test_events_for_unknown_or_settled_orders_are_ignored():
    engine = _engine()
    filled = _trade("o1", status=TradeStatus.FILLED, fill=10.0)
    engine._trade_history.append(filled)
    result = engine.apply_user_channel_events(
        [
            UserOrderEvent(order_id="o1", event_type="TRADE", incremental_matched=5.0),
            UserOrderEvent(order_id="unknown", event_type="TRADE", incremental_matched=5.0),
        ]
    )
    assert result.polled == []
    assert result.changed == []
    assert filled.fill_size == pytest.approx(10.0)


def test_dry_run_ignores_user_events():
    engine = _engine(dry_run=True)
    trade = _trade()
    engine._trade_history.append(trade)
    result = engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="TRADE", incremental_matched=5.0)]
    )
    assert result.polled == [] and result.changed == []
    assert trade.status is TradeStatus.PENDING


def test_unchanged_event_is_not_reported_as_changed():
    engine = _engine()
    trade = _trade(fill=4.0)
    trade.status = TradeStatus.PARTIAL
    trade.fill_price = 0.5
    engine._trade_history.append(trade)
    result = engine.apply_user_channel_events(
        [UserOrderEvent(order_id="o1", event_type="UPDATE", cumulative_matched=4.0, price=0.5)]
    )
    assert result.polled == [trade]
    assert result.changed == []


def test_two_identical_orders_both_reach_the_changed_list():
    """回归：TradeRecord 是带默认 __eq__ 的 dataclass.

    用 `trade not in changed` 去重时，同市场同价同量的两笔挂单会被判为
    相等，其中一笔的成交就永远到不了 maker 库存与退出管理器。
    """
    engine = _engine()
    first = _trade("o1")
    second = _trade("o2")
    # 除 order_id / trade_id 外字段完全一致
    second.trade_id = first.trade_id
    engine._trade_history.extend([first, second])

    result = engine.apply_user_channel_events(
        [
            UserOrderEvent(order_id="o1", event_type="UPDATE", cumulative_matched=10.0),
            UserOrderEvent(order_id="o2", event_type="UPDATE", cumulative_matched=10.0),
        ]
    )
    assert len(result.changed) == 2
    assert {id(t) for t in result.changed} == {id(first), id(second)}
