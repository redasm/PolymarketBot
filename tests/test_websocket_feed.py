"""WebSocketFeed 单元测试：快照与增量同步到 EnhancedBookStore."""

from __future__ import annotations

import json

import pytest

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.edge_engine import EdgeEngine
from polymarket_arb.websocket_feed import OrderBookMirror, WebSocketFeed


def test_book_and_price_change_keep_enhanced_store_fresh():
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, enhanced_store=store)

    feed._handle_message(json.dumps({
        "type": "book",
        "asset_id": "yes",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
    }))
    feed._handle_message(json.dumps({
        "type": "book",
        "asset_id": "no",
        "bids": [{"price": "0.57", "size": "100"}],
        "asks": [{"price": "0.60", "size": "100"}],
    }))

    before_mid = store.get_yes_mid()
    feed._handle_message(json.dumps({
        "type": "price_change",
        "changes": [
            {"asset_id": "yes", "side": "sell", "price": "0.41", "size": "100"},
            {"asset_id": "yes", "side": "sell", "price": "0.42", "size": "0"},
        ],
    }))

    assert before_mid == pytest.approx(0.41, abs=1e-9)
    assert store.get_yes_mid() == pytest.approx(0.405, abs=1e-9)


def test_disconnect_causes_edge_engine_veto():
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, enhanced_store=store)

    feed._handle_message(json.dumps({
        "type": "book",
        "asset_id": "yes",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
    }))
    feed._handle_message(json.dumps({
        "type": "book",
        "asset_id": "no",
        "bids": [{"price": "0.57", "size": "100"}],
        "asks": [{"price": "0.60", "size": "100"}],
    }))

    store.set_connected(False)
    decision = EdgeEngine().evaluate(store)

    assert decision.veto is True
    assert "ws_disconnected" in decision.veto_reasons


def test_price_change_supports_price_changes_field():
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, enhanced_store=store)

    feed._handle_message(json.dumps({
        "event_type": "book",
        "asset_id": "yes",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
    }))
    feed._handle_message(json.dumps({
        "event_type": "book",
        "asset_id": "no",
        "bids": [{"price": "0.57", "size": "100"}],
        "asks": [{"price": "0.60", "size": "100"}],
    }))

    feed._handle_message(json.dumps({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "yes", "side": "SELL", "price": "0.41", "size": "100"},
            {"asset_id": "yes", "side": "SELL", "price": "0.42", "size": "0"},
        ],
    }))

    assert store.get_yes_mid() == pytest.approx(0.405, abs=1e-9)


def test_price_change_supports_single_change_dict_field():
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, enhanced_store=store)

    feed._handle_message(json.dumps({
        "type": "book",
        "asset_id": "yes",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
    }))
    feed._handle_message(json.dumps({
        "type": "price_change",
        "changes": {"asset_id": "yes", "side": "sell", "price": "0.41", "size": "100"},
    }))

    assert store.get_yes_mid() == pytest.approx(0.405, abs=1e-9)


def test_price_change_ignores_malformed_change_items():
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, enhanced_store=store)

    feed._handle_message(json.dumps({
        "type": "book",
        "asset_id": "yes",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
    }))
    feed._handle_message(json.dumps({
        "type": "price_change",
        "changes": [
            ["bad-item"],
            {"asset_id": "yes", "side": "sell", "price": "0.41", "size": "100"},
        ],
    }))

    assert store.get_yes_mid() == pytest.approx(0.405, abs=1e-9)


def test_apply_snapshot_supports_list_price_levels():
    mirror = OrderBookMirror()

    mirror.apply_snapshot(
        "yes",
        bids=[["0.40", "100"], ["0.39", "50"]],
        asks=[["0.42", "120"], ["0.43", "40"]],
    )

    snap = mirror.get("yes")
    assert snap is not None
    assert snap.best_bid == pytest.approx(0.40, abs=1e-9)
    assert snap.best_ask == pytest.approx(0.42, abs=1e-9)


def test_handle_message_supports_list_payload():
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, enhanced_store=store)

    feed._handle_message(json.dumps([
        {
            "event_type": "book",
            "asset_id": "yes",
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.42", "size": "100"}],
        },
        {
            "event_type": "book",
            "asset_id": "no",
            "bids": [{"price": "0.57", "size": "100"}],
            "asks": [{"price": "0.60", "size": "100"}],
        },
    ]))

    assert store.get_yes_mid() == pytest.approx(0.41, abs=1e-9)
    assert store.get_no_mid() == pytest.approx(0.585, abs=1e-9)


def test_reconnect_jitter_stays_within_expected_range():
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)

    delay = feed._with_reconnect_jitter(10.0)

    assert 10.0 <= delay <= 13.0


def test_last_trade_price_forwards_to_trade_callback():
    captured: list[dict] = []
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, trade_callback=captured.append)

    feed._handle_message(json.dumps({
        "event_type": "last_trade_price",
        "asset_id": "yes-tok",
        "side": "BUY",
        "size": "100",
        "price": "0.42",
        "timestamp": "1740000000000",
    }))

    assert len(captured) == 1
    assert captured[0]["asset_id"] == "yes-tok"
    assert captured[0]["side"] == "BUY"


def test_last_trade_price_without_callback_is_safe_no_op():
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)

    feed._handle_message(json.dumps({
        "type": "last_trade_price",
        "asset_id": "yes-tok",
        "side": "BUY",
        "size": "100",
        "price": "0.42",
    }))


def test_last_trade_price_swallows_callback_exceptions():
    """A buggy consumer must not kill the WS pump."""

    def raising(_event):
        raise RuntimeError("consumer blew up")

    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, trade_callback=raising)

    feed._handle_message(json.dumps({
        "type": "last_trade_price",
        "asset_id": "yes-tok",
        "side": "BUY",
        "size": "100",
    }))


def test_set_trade_callback_replaces_existing_consumer():
    first_calls: list[dict] = []
    second_calls: list[dict] = []
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror, trade_callback=first_calls.append)

    feed.set_trade_callback(second_calls.append)
    feed._handle_message(json.dumps({
        "type": "last_trade_price",
        "asset_id": "yes-tok",
        "side": "SELL",
        "size": "12",
    }))

    assert first_calls == []
    assert len(second_calls) == 1


def test_apply_delta_buffers_until_snapshot_arrives():
    """Pre-snapshot deltas must be replayed once the snapshot lands.

    Previously they were silently dropped, which caused the first few price
    movements after a reconnect to be invisible to downstream tiers.
    """
    mirror = OrderBookMirror()

    # delta arrives first — must NOT be applied to a non-existent book
    mirror.apply_delta("token-late", "buy", 0.41, 100.0)
    assert mirror.get("token-late") is None

    # snapshot arrives — buffered delta should be replayed on top
    mirror.apply_snapshot(
        "token-late",
        bids=[{"price": "0.40", "size": "50"}],
        asks=[{"price": "0.45", "size": "50"}],
    )

    snap = mirror.get("token-late")
    assert snap is not None
    bid_prices = sorted(level.price for level in snap.bids)
    # Replayed delta added a 0.41 level to the snapshot's [0.40] bids.
    assert 0.41 in bid_prices
    assert 0.40 in bid_prices


def test_callback_queue_drops_oldest_snapshot_when_backlogged():
    mirror = OrderBookMirror()
    with mirror._callbacks_lock:
        mirror._on_change_callbacks.append(lambda token_id, snap: None)
        mirror._callback_worker_running = True

    for idx in range(1024):
        mirror._fire_callbacks(f"token-{idx}", object())

    assert mirror._callback_queue.full() is True

    mirror._fire_callbacks("latest-token", object())

    items = []
    while not mirror._callback_queue.empty():
        item = mirror._callback_queue.get_nowait()
        items.append(item)
        mirror._callback_queue.task_done()

    tokens = [token_id for token_id, _ in items if token_id is not None]
    assert "token-0" not in tokens
    assert "latest-token" in tokens


def test_stop_can_progress_even_when_callback_queue_is_full(monkeypatch):
    mirror = OrderBookMirror()
    with mirror._callbacks_lock:
        mirror._on_change_callbacks.append(lambda token_id, snap: None)
        mirror._callback_worker_running = True
    mirror._callback_worker = type(
        "_Worker",
        (),
        {"join": lambda self, timeout=None: None},
    )()

    for idx in range(1024):
        mirror._callback_queue.put_nowait((f"token-{idx}", object()))

    join_called = {"value": False}

    def _fake_join():
        join_called["value"] = True

    monkeypatch.setattr(mirror._callback_queue, "join", _fake_join)

    mirror.stop()

    assert mirror._callback_worker_running is False
    assert join_called["value"] is True
