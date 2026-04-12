"""EnhancedBookStore unit tests."""

from __future__ import annotations

import pytest

from polymarket_arb.book_store import EnhancedBookStore, OrderbookSide


def test_orderbook_side_snapshot_computes_microprice_and_spread():
    side = OrderbookSide(
        bids=[(0.40, 100), (0.39, 50)],
        asks=[(0.42, 120), (0.43, 40)],
        ts_ms=1_000,
    )

    snap = side.snapshot()

    assert snap["mid"] == pytest.approx(0.41, abs=1e-9)
    assert snap["spread"] == pytest.approx(0.02, abs=1e-9)
    assert snap["microprice"] == pytest.approx((0.42 * 100 + 0.40 * 120) / 220, abs=1e-9)
    assert snap["bid_depth_top5"] == 150
    assert snap["ask_depth_top5"] == 160


@pytest.mark.parametrize(
    ("token_id", "expected_yes_mid", "expected_no_mid"),
    [
        ("yes-token", 0.41, None),
        ("no-token", None, 0.41),
    ],
)
def test_update_by_token_id_routes_to_expected_side(token_id, expected_yes_mid, expected_no_mid):
    store = EnhancedBookStore()
    store.set_market("m1", "yes-token", "no-token")

    matched = store.update_by_token_id(token_id, bids=[(0.40, 10)], asks=[(0.42, 10)], ts_ms=1_000)

    assert matched is True
    if expected_yes_mid is None:
        assert store.get_yes_mid() is None
    else:
        assert store.get_yes_mid() == pytest.approx(expected_yes_mid, abs=1e-9)
    if expected_no_mid is None:
        assert store.get_no_mid() is None
    else:
        assert store.get_no_mid() == pytest.approx(expected_no_mid, abs=1e-9)


def test_is_ready_requires_connected_and_fresh_both_sides(monkeypatch):
    store = EnhancedBookStore()
    store.set_market("m1", "yes-token", "no-token")
    store.update_yes([(0.40, 10)], [(0.42, 10)], 1_000)
    store.update_no([(0.57, 10)], [(0.61, 10)], 1_000)

    monkeypatch.setattr("polymarket_arb.book_store.now_ms", lambda: 5_000)
    assert store.is_ready() is False

    store.set_connected(True)
    assert store.is_ready() is True

    monkeypatch.setattr("polymarket_arb.book_store.now_ms", lambda: 20_500)
    assert store.is_ready() is False
