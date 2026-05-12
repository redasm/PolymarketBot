"""OrderBookAnalyzer cache tests."""

import time

from polymarket_arb.models import OrderBookLevel, OrderBookSnapshot
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer


class _Level:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class _Book:
    def __init__(self):
        self.bids = [_Level(0.4, 10)]
        self.asks = [_Level(0.42, 12)]
        self.tick_size = "0.01"


class _Client:
    def __init__(self):
        self.calls = 0

    def get_order_book(self, token_id):
        self.calls += 1
        return _Book()


def test_get_snapshot_uses_short_ttl_cache():
    client = _Client()
    analyzer = OrderBookAnalyzer(client, snapshot_ttl_sec=1.0)

    first = analyzer.get_snapshot("token-1")
    second = analyzer.get_snapshot("token-1")

    assert first is second
    assert client.calls == 1


def test_get_snapshot_retries_on_transient_failure():
    class _RetryClient:
        def __init__(self):
            self.calls = 0

        def get_order_book(self, token_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary failure")
            return _Book()

    client = _RetryClient()
    analyzer = OrderBookAnalyzer(client, snapshot_ttl_sec=0.0, retry_count=1, retry_delay_sec=0.0)

    snapshot = analyzer.get_snapshot("token-1")

    assert snapshot is not None
    assert client.calls == 2


def test_get_snapshot_merges_duplicate_price_levels():
    class _DuplicateBook:
        def __init__(self):
            self.bids = [_Level(0.4, 10), _Level(0.4, 15), _Level(0.39, 5)]
            self.asks = [_Level(0.42, 12), _Level(0.42, 8), _Level(0.43, 4)]
            self.tick_size = "0.01"

    class _DuplicateClient:
        def get_order_book(self, token_id):
            return _DuplicateBook()

    analyzer = OrderBookAnalyzer(_DuplicateClient(), snapshot_ttl_sec=0.0)

    snapshot = analyzer.get_snapshot("token-dup")

    assert snapshot is not None
    assert [(level.price, level.size) for level in snapshot.bids] == [(0.4, 25), (0.39, 5)]
    assert [(level.price, level.size) for level in snapshot.asks] == [(0.42, 20), (0.43, 4)]


def test_get_snapshot_caches_missing_orderbook_failures_for_cooldown():
    class _MissingBookClient:
        def __init__(self):
            self.calls = 0

        def get_order_book(self, token_id):
            self.calls += 1
            raise RuntimeError("No orderbook exists for the requested token id")

    client = _MissingBookClient()
    analyzer = OrderBookAnalyzer(
        client,
        snapshot_ttl_sec=0.0,
        retry_count=0,
        retry_delay_sec=0.0,
    )

    assert analyzer.get_snapshot("missing-token") is None
    assert analyzer.get_snapshot("missing-token") is None
    assert client.calls == 1

    time.sleep(0.01)
    assert analyzer.get_snapshot("another-token") is None
    assert client.calls == 2


def test_get_snapshot_prefers_ws_mirror_before_rest():
    class _Mirror:
        def __init__(self, snap):
            self._snap = snap

        def get(self, token_id):
            if token_id == self._snap.token_id:
                return self._snap
            return None

    mirror_snap = OrderBookSnapshot(
        token_id="ws-token",
        best_bid=0.48,
        best_ask=0.50,
        bids=[OrderBookLevel(0.48, 20)],
        asks=[OrderBookLevel(0.50, 25)],
    )
    client = _Client()
    analyzer = OrderBookAnalyzer(client, snapshot_ttl_sec=0.0, live_mirror=_Mirror(mirror_snap))

    snapshot = analyzer.get_snapshot("ws-token")

    assert snapshot is mirror_snap
    assert client.calls == 0


def test_get_snapshot_does_not_apply_rest_ttl_to_recent_ws_snapshot():
    class _Mirror:
        def __init__(self, snap):
            self._snap = snap

        def get(self, token_id):
            if token_id == self._snap.token_id:
                return self._snap
            return None

    mirror_snap = OrderBookSnapshot(
        token_id="ws-token",
        best_bid=0.48,
        best_ask=0.50,
        bids=[OrderBookLevel(0.48, 20)],
        asks=[OrderBookLevel(0.50, 25)],
        timestamp=time.time() - 1.0,
    )
    client = _Client()
    analyzer = OrderBookAnalyzer(
        client,
        snapshot_ttl_sec=0.1,
        live_mirror=_Mirror(mirror_snap),
        ws_snapshot_max_age_sec=5.0,
    )

    snapshot = analyzer.get_snapshot("ws-token")

    assert snapshot is mirror_snap
    assert client.calls == 0


def test_stale_ws_snapshot_is_not_returned_via_cache_path():
    class _Mirror:
        def __init__(self, snap):
            self._snap = snap

        def get(self, token_id):
            if token_id == self._snap.token_id:
                return self._snap
            return None

    mirror_snap = OrderBookSnapshot(
        token_id="ws-token",
        best_bid=0.48,
        best_ask=0.50,
        bids=[OrderBookLevel(0.48, 20)],
        asks=[OrderBookLevel(0.50, 25)],
        timestamp=time.time() - 20.0,
    )
    client = _Client()
    analyzer = OrderBookAnalyzer(
        client,
        snapshot_ttl_sec=60.0,
        live_mirror=_Mirror(mirror_snap),
        ws_snapshot_max_age_sec=5.0,
    )

    first = analyzer.get_snapshot("ws-token", allow_rest_fallback=False)
    second = analyzer.get_snapshot("ws-token", allow_rest_fallback=False)

    assert first is None
    assert second is None
    assert client.calls == 0


def test_ws_snapshot_that_becomes_stale_is_not_returned_from_cache():
    class _Mirror:
        def __init__(self, snap):
            self._snap = snap

        def get(self, token_id):
            if token_id == self._snap.token_id:
                return self._snap
            return None

    mirror_snap = OrderBookSnapshot(
        token_id="ws-token",
        best_bid=0.48,
        best_ask=0.50,
        bids=[OrderBookLevel(0.48, 20)],
        asks=[OrderBookLevel(0.50, 25)],
        timestamp=time.time(),
    )
    client = _Client()
    analyzer = OrderBookAnalyzer(
        client,
        snapshot_ttl_sec=60.0,
        live_mirror=_Mirror(mirror_snap),
        ws_snapshot_max_age_sec=5.0,
    )

    assert analyzer.get_snapshot("ws-token", allow_rest_fallback=False) is mirror_snap
    mirror_snap.timestamp = time.time() - 20.0

    snapshot = analyzer.get_snapshot("ws-token", allow_rest_fallback=False)

    assert snapshot is None
    assert client.calls == 0


def test_batch_get_snapshots_only_hits_rest_for_tokens_missing_from_ws_and_cache():
    class _Mirror:
        def __init__(self, snapshots):
            self._snapshots = snapshots

        def get(self, token_id):
            return self._snapshots.get(token_id)

    mirror_snap = OrderBookSnapshot(
        token_id="ws-token",
        best_bid=0.40,
        best_ask=0.42,
        bids=[OrderBookLevel(0.40, 10)],
        asks=[OrderBookLevel(0.42, 12)],
    )
    client = _Client()
    analyzer = OrderBookAnalyzer(
        client,
        snapshot_ttl_sec=60.0,
        live_mirror=_Mirror({"ws-token": mirror_snap}),
    )
    cached_snap = OrderBookSnapshot(
        token_id="cached-token",
        best_bid=0.51,
        best_ask=0.53,
        bids=[OrderBookLevel(0.51, 8)],
        asks=[OrderBookLevel(0.53, 9)],
    )
    analyzer._set_cached_snapshot("cached-token", cached_snap, source="rest")

    snapshots = analyzer.batch_get_snapshots(
        ["ws-token", "cached-token", "rest-token", "rest-token"],
        delay=0.0,
    )

    assert set(snapshots) == {"ws-token", "cached-token", "rest-token"}
    assert snapshots["ws-token"] is mirror_snap
    assert snapshots["cached-token"] is cached_snap
    assert client.calls == 1


def test_snapshot_stats_report_hit_mix_and_can_reset():
    class _Mirror:
        def __init__(self, snapshots):
            self._snapshots = snapshots

        def get(self, token_id):
            return self._snapshots.get(token_id)

    mirror_snap = OrderBookSnapshot(
        token_id="ws-token",
        best_bid=0.44,
        best_ask=0.46,
        bids=[OrderBookLevel(0.44, 10)],
        asks=[OrderBookLevel(0.46, 10)],
    )
    client = _Client()
    analyzer = OrderBookAnalyzer(
        client,
        snapshot_ttl_sec=60.0,
        live_mirror=_Mirror({"ws-token": mirror_snap}),
    )
    cached_snap = OrderBookSnapshot(
        token_id="cached-token",
        best_bid=0.51,
        best_ask=0.52,
        bids=[OrderBookLevel(0.51, 5)],
        asks=[OrderBookLevel(0.52, 6)],
    )
    analyzer._set_cached_snapshot("cached-token", cached_snap, source="rest")

    assert analyzer.get_snapshot("ws-token") is mirror_snap
    assert analyzer.get_snapshot("cached-token") is cached_snap
    assert analyzer.get_snapshot("rest-token") is not None

    stats = analyzer.snapshot_stats()

    assert stats["ws_hit"] == 1
    assert stats["cache_hit"] == 1
    assert stats["rest_fallback"] == 1
    assert stats["rest_success"] == 1
    assert stats["rest_error"] == 0
    assert stats["requests"] == 3

    delta = analyzer.snapshot_stats(reset=True)
    assert delta == stats
    assert analyzer.snapshot_stats()["requests"] == 0


def test_batch_get_snapshots_counts_logical_requests_once_per_token():
    class _Mirror:
        def __init__(self, snapshots):
            self._snapshots = snapshots

        def get(self, token_id):
            return self._snapshots.get(token_id)

    mirror_snap = OrderBookSnapshot(
        token_id="ws-token",
        best_bid=0.44,
        best_ask=0.46,
        bids=[OrderBookLevel(0.44, 10)],
        asks=[OrderBookLevel(0.46, 10)],
    )
    client = _Client()
    analyzer = OrderBookAnalyzer(
        client,
        snapshot_ttl_sec=60.0,
        live_mirror=_Mirror({"ws-token": mirror_snap}),
    )
    analyzer._set_cached_snapshot("cached-token", OrderBookSnapshot(
        token_id="cached-token",
        best_bid=0.51,
        best_ask=0.52,
        bids=[OrderBookLevel(0.51, 5)],
        asks=[OrderBookLevel(0.52, 6)],
    ), source="rest")

    snapshots = analyzer.batch_get_snapshots(
        ["ws-token", "cached-token", "rest-token", "rest-token"],
        delay=0.0,
    )

    assert set(snapshots) == {"ws-token", "cached-token", "rest-token"}
    stats = analyzer.snapshot_stats()
    assert stats["requests"] == 3
    assert stats["rest_fallback"] == 1


def test_feed_health_token_scope_ignores_unrelated_stale_mirror():
    """P0-2: a stale mirror token outside the scope must not fail health."""
    now = time.time()
    fresh = OrderBookSnapshot(
        token_id="fresh-token",
        best_bid=0.40,
        best_ask=0.42,
        bids=[OrderBookLevel(0.40, 10)],
        asks=[OrderBookLevel(0.42, 12)],
        timestamp=now - 0.5,
    )
    stale = OrderBookSnapshot(
        token_id="stale-token",
        best_bid=0.60,
        best_ask=0.62,
        bids=[OrderBookLevel(0.60, 5)],
        asks=[OrderBookLevel(0.62, 5)],
        timestamp=now - 60.0,
    )

    class _Mirror:
        def __init__(self, snapshots):
            self._snapshots = snapshots

        def get_all(self):
            return self._snapshots

    analyzer = OrderBookAnalyzer(
        _Client(),
        snapshot_ttl_sec=60.0,
        live_mirror=_Mirror({"fresh-token": fresh, "stale-token": stale}),
    )

    scoped = analyzer.feed_health(
        max_snapshot_age_sec=5.0,
        min_ws_hit_ratio=0.0,
        token_ids=["fresh-token"],
    )
    assert scoped["healthy"] is True

    global_health = analyzer.feed_health(
        max_snapshot_age_sec=5.0,
        min_ws_hit_ratio=0.0,
    )
    assert global_health["healthy"] is False
    assert global_health["reason"] == "stale_ws_snapshot"


def test_feed_health_token_scope_catches_target_stale():
    """The opposite: scoping to a stale token still fails health."""
    now = time.time()
    stale = OrderBookSnapshot(
        token_id="target-token",
        best_bid=0.60,
        best_ask=0.62,
        bids=[OrderBookLevel(0.60, 5)],
        asks=[OrderBookLevel(0.62, 5)],
        timestamp=now - 60.0,
    )

    class _Mirror:
        def __init__(self, snapshots):
            self._snapshots = snapshots

        def get_all(self):
            return self._snapshots

    analyzer = OrderBookAnalyzer(
        _Client(),
        snapshot_ttl_sec=60.0,
        live_mirror=_Mirror({"target-token": stale}),
    )

    health = analyzer.feed_health(
        max_snapshot_age_sec=5.0,
        min_ws_hit_ratio=0.0,
        token_ids=["target-token"],
    )
    assert health["healthy"] is False
    assert health["reason"] == "stale_ws_snapshot"


def test_feed_health_global_caches_within_ttl():
    """Global-path feed_health() must reuse the cached verdict
    within ``_feed_health_cache_ttl_sec`` so the WS mirror's
    ``get_all`` lock isn't hit on every redundant call.
    """
    now = time.time()
    fresh = OrderBookSnapshot(
        token_id="t",
        best_bid=0.5,
        best_ask=0.51,
        bids=[OrderBookLevel(0.5, 5)],
        asks=[OrderBookLevel(0.51, 5)],
        timestamp=now,
    )

    class _CountingMirror:
        def __init__(self, snap):
            self._snap = {"t": snap}
            self.get_all_calls = 0

        def get_all(self):
            self.get_all_calls += 1
            return self._snap

    mirror = _CountingMirror(fresh)
    analyzer = OrderBookAnalyzer(
        _Client(),
        snapshot_ttl_sec=60.0,
        live_mirror=mirror,
        feed_health_cache_ttl_sec=0.5,
    )

    first = analyzer.feed_health(max_snapshot_age_sec=5.0, min_ws_hit_ratio=0.0)
    second = analyzer.feed_health(max_snapshot_age_sec=5.0, min_ws_hit_ratio=0.0)
    assert first["healthy"] is True
    assert second["healthy"] is True
    assert mirror.get_all_calls == 1  # second call served from cache

    # Token-scoped paths must bypass the cache because the scope
    # changes between call sites — a healthy global verdict isn't
    # a guarantee that the token-scoped check is healthy.
    scoped = analyzer.feed_health(
        max_snapshot_age_sec=5.0,
        min_ws_hit_ratio=0.0,
        token_ids=["t"],
    )
    assert scoped["healthy"] is True
    assert mirror.get_all_calls == 2


def test_feed_health_cache_expires_after_ttl():
    """Past the TTL the cache must be rebuilt — otherwise a stale
    book that lands after the cached verdict would be invisible.
    """
    now = time.time()
    fresh = OrderBookSnapshot(
        token_id="t",
        best_bid=0.5,
        best_ask=0.51,
        bids=[OrderBookLevel(0.5, 5)],
        asks=[OrderBookLevel(0.51, 5)],
        timestamp=now,
    )

    class _Mirror:
        def __init__(self, snap):
            self._snap = {"t": snap}
            self.get_all_calls = 0

        def get_all(self):
            self.get_all_calls += 1
            return self._snap

    mirror = _Mirror(fresh)
    analyzer = OrderBookAnalyzer(
        _Client(),
        snapshot_ttl_sec=60.0,
        live_mirror=mirror,
        feed_health_cache_ttl_sec=0.05,
    )
    analyzer.feed_health(max_snapshot_age_sec=5.0, min_ws_hit_ratio=0.0)
    time.sleep(0.08)
    analyzer.feed_health(max_snapshot_age_sec=5.0, min_ws_hit_ratio=0.0)
    assert mirror.get_all_calls == 2
