"""OrderBookAnalyzer cache tests."""

import time

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
