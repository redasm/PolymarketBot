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


def test_apply_delta_skips_snapshot_allocation_on_no_op_remove():
    """Removing a price that isn't on book is a no-op — no new
    snapshot should be allocated and no callbacks fired.
    """
    mirror = OrderBookMirror()
    mirror.apply_snapshot(
        "tk",
        bids=[{"price": "0.40", "size": "50"}],
        asks=[{"price": "0.45", "size": "50"}],
    )
    snap_before = mirror.get("tk")
    mirror.apply_delta("tk", "buy", 0.33, 0.0)  # price not on book, size 0 → no-op
    snap_after = mirror.get("tk")
    assert snap_after is snap_before


def test_apply_delta_inserts_in_sorted_position_without_full_sort():
    """The post-rewrite path must still keep bids DESC / asks ASC
    after a single-level insert (no full sort).
    """
    mirror = OrderBookMirror()
    mirror.apply_snapshot(
        "tk",
        bids=[
            {"price": "0.40", "size": "5"},
            {"price": "0.38", "size": "5"},
        ],
        asks=[
            {"price": "0.45", "size": "5"},
            {"price": "0.47", "size": "5"},
        ],
    )

    mirror.apply_delta("tk", "buy", 0.39, 7.0)  # mid-insert
    mirror.apply_delta("tk", "sell", 0.46, 9.0)  # mid-insert

    snap = mirror.get("tk")
    assert [lv.price for lv in snap.bids] == [0.40, 0.39, 0.38]
    assert [lv.price for lv in snap.asks] == [0.45, 0.46, 0.47]


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


def test_handle_tick_size_change_updates_tick_size_map():
    """tick_size_change pushes update self._tick_sizes so callers can
    round prices correctly for live orders. See client-side
    `polymarket_arb/websocket_feed.py::_handle_tick_size_change`.
    """
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)

    feed._handle_message(json.dumps({
        "event_type": "tick_size_change",
        "asset_id": "tok-a",
        "new_tick_size": "0.001",
    }))

    assert feed.get_tick_size("tok-a") == pytest.approx(0.001, abs=1e-9)


def test_handle_tick_size_change_ignores_bad_payload():
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)

    feed._handle_message(json.dumps({
        "event_type": "tick_size_change",
        "asset_id": "tok-a",
        "new_tick_size": "not-a-number",
    }))
    feed._handle_message(json.dumps({
        "event_type": "tick_size_change",
        "asset_id": "",
        "new_tick_size": "0.01",
    }))
    feed._handle_message(json.dumps({
        "event_type": "tick_size_change",
        "asset_id": "tok-a",
        "new_tick_size": "-0.1",
    }))

    assert feed.get_tick_size("tok-a") is None


def test_market_resolved_auto_unsubscribes_resolved_tokens():
    """market_resolved must mark tokens resolved AND attempt to remove
    them from the live subscription, so a settled market doesn't keep
    burning a subscription slot.
    """
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed.subscribe(["tok-yes", "tok-no", "tok-other"])

    feed._handle_message(json.dumps({
        "event_type": "market_resolved",
        "id": "0xresolved",
        "winning_asset_id": "tok-yes",
        "clob_token_ids": ["tok-yes", "tok-no"],
    }))

    assert feed.get_resolved_tokens() == {"tok-yes", "tok-no"}
    # remove_tokens called with ws_handle=None returns False but still
    # updates the local subscribed set so reconnect picks up the new set.
    assert "tok-yes" not in feed.subscribed_tokens()
    assert "tok-no" not in feed.subscribed_tokens()
    assert "tok-other" in feed.subscribed_tokens()


def test_market_resolved_supports_assets_ids_alias():
    """Server has historically used both `clob_token_ids` and
    `assets_ids` — both must be accepted.
    """
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed.subscribe(["tok-a"])

    feed._handle_message(json.dumps({
        "event_type": "market_resolved",
        "id": "0xresolved",
        "winning_asset_id": "tok-a",
        "assets_ids": ["tok-a"],
    }))

    assert feed.get_resolved_tokens() == {"tok-a"}
    assert feed.subscribed_tokens() == set()


def test_new_market_handler_logs_without_modifying_subscription(caplog):
    """new_market is informational only — REST market discovery is the
    source of truth. We must NOT auto-subscribe new tokens from WS.
    """
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed.subscribe(["tok-existing"])

    with caplog.at_level("INFO"):
        feed._handle_message(json.dumps({
            "event_type": "new_market",
            "id": "0xnew",
            "question": "Will Z?",
            "clob_token_ids": ["new-yes", "new-no"],
        }))

    assert feed.subscribed_tokens() == {"tok-existing"}


def test_best_bid_ask_event_does_not_raise():
    """`best_bid_ask` is silent-absorbed because price_change already
    maintains best levels — confirm we don't crash on it.
    """
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)

    feed._handle_message(json.dumps({
        "event_type": "best_bid_ask",
        "asset_id": "tok-a",
        "best_bid": "0.40",
        "best_ask": "0.42",
    }))


def test_add_tokens_returns_false_when_not_connected():
    """`add_tokens` must return False when ws_handle is None so the
    caller knows to fall back to stop+restart.
    """
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)

    ok = feed.add_tokens(["new-1", "new-2"])

    assert ok is False
    # Local subscription set must still be updated so the next reconnect
    # picks up the new tokens.
    assert feed.subscribed_tokens() == {"new-1", "new-2"}


def test_remove_tokens_returns_false_when_not_connected():
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed.subscribe(["existing-1", "existing-2"])

    ok = feed.remove_tokens(["existing-1"])

    assert ok is False
    assert feed.subscribed_tokens() == {"existing-2"}


def test_add_tokens_noop_when_already_subscribed():
    """No-op adds short-circuit to True without touching the WS handle
    so we don't pay a JSON-encode cost on idle refresh cycles.
    """
    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed.subscribe(["tok-a"])

    assert feed.add_tokens(["tok-a"]) is True


def test_add_tokens_sends_dynamic_subscription_payload_when_connected():
    """Lock the wire-format contract: subscribe payload uses sorted
    `assets_ids`, `operation=subscribe`, and `custom_feature_enabled=true`.
    """
    sent: list[str] = []

    class _SpyWs:
        def send(self, payload):
            sent.append(payload)

    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed._ws_handle = _SpyWs()

    ok = feed.add_tokens(["tok-b", "tok-a"])

    assert ok is True
    assert len(sent) == 1
    payload = json.loads(sent[0])
    assert payload["assets_ids"] == ["tok-a", "tok-b"]
    assert payload["operation"] == "subscribe"
    assert payload["custom_feature_enabled"] is True


def test_remove_tokens_sends_unsubscribe_without_custom_feature():
    """`unsubscribe` does not require custom_feature_enabled — keep the
    payload minimal so the server doesn't reject the diff.
    """
    sent: list[str] = []

    class _SpyWs:
        def send(self, payload):
            sent.append(payload)

    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed.subscribe(["tok-a", "tok-b"])
    feed._ws_handle = _SpyWs()

    ok = feed.remove_tokens(["tok-a"])

    assert ok is True
    payload = json.loads(sent[0])
    assert payload["operation"] == "unsubscribe"
    assert payload["assets_ids"] == ["tok-a"]
    assert "custom_feature_enabled" not in payload


def test_send_dynamic_subscription_falls_back_on_send_exception():
    """If ws.send raises (broken pipe, etc.), `add_tokens` must return
    False so the caller can fall back. The local subscription set
    should still reflect the desired state for the next reconnect.
    """
    class _BrokenWs:
        def send(self, payload):
            raise OSError("broken pipe")

    mirror = OrderBookMirror()
    feed = WebSocketFeed(mirror=mirror)
    feed._ws_handle = _BrokenWs()

    ok = feed.add_tokens(["tok-x"])

    assert ok is False
    assert feed.subscribed_tokens() == {"tok-x"}


def test_subscription_payload_sets_initial_dump_true():
    """Subscription payload must explicitly request initial_dump=true so
    the server behaviour stays stable if the default ever flips.
    See `_run_loop` subscription block in websocket_feed.py.
    """
    # This test does not start the connection thread; it asserts the
    # contract by inspecting the source code (we keep this defensive
    # because there's no public surface to test the inline payload).
    import inspect
    from polymarket_arb import websocket_feed

    src = inspect.getsource(websocket_feed.WebSocketFeed._run_loop)
    assert "\"initial_dump\": True" in src
    assert "\"custom_feature_enabled\": True" in src


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


def test_processor_applies_queued_messages_in_order_without_blocking_producer(monkeypatch):
    import threading as _threading
    import time as _time

    feed = WebSocketFeed(mirror=OrderBookMirror())
    seen: list[str] = []
    done = _threading.Event()

    def _slow_handle(raw):
        _time.sleep(0.02)
        seen.append(raw)
        if raw == "m9":
            done.set()

    monkeypatch.setattr(feed, "_handle_message", _slow_handle)
    feed._start_processor()
    try:
        started = _time.monotonic()
        for i in range(10):
            feed._inbox.put(f"m{i}")
        enqueue_elapsed = _time.monotonic() - started

        assert done.wait(timeout=5)
        assert seen == [f"m{i}" for i in range(10)]
        assert enqueue_elapsed < 0.02 * 10 / 2
    finally:
        feed._stop_processor()
    assert feed._processor is None


def test_processor_survives_handler_exception(monkeypatch):
    import threading as _threading

    feed = WebSocketFeed(mirror=OrderBookMirror())
    handled: list[str] = []
    done = _threading.Event()

    def _handle(raw):
        if raw == "bad":
            raise RuntimeError("boom")
        handled.append(raw)
        done.set()

    monkeypatch.setattr(feed, "_handle_message", _handle)
    feed._start_processor()
    try:
        feed._inbox.put("bad")
        feed._inbox.put("good")
        assert done.wait(timeout=5)
        assert handled == ["good"]
    finally:
        feed._stop_processor()


def test_run_loop_enqueues_instead_of_handling_inline():
    import inspect
    from polymarket_arb import websocket_feed

    src = inspect.getsource(websocket_feed.WebSocketFeed._run_loop)
    assert "self._inbox.put(raw)" in src
    assert "self._handle_message(raw)" not in src
