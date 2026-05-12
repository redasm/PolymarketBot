"""Regression tests for the DirtyMarketTracker (P0-dirty)."""

from __future__ import annotations

from polymarket_arb.main_helpers.dirty_market_tracker import DirtyMarketTracker


def test_mark_dirty_resolves_token_to_condition_and_drains():
    tracker = DirtyMarketTracker()
    tracker.register_token_map({"yes-1": "cond-a", "no-1": "cond-a", "yes-2": "cond-b"})

    tracker.mark_dirty("yes-1")
    tracker.mark_dirty("no-1")  # same market, dedup via set
    tracker.mark_dirty("yes-2")

    drained = tracker.drain()
    assert drained == {"cond-a", "cond-b"}
    assert tracker.drain() == set()  # cleared after first drain


def test_mark_dirty_ignores_unknown_token():
    tracker = DirtyMarketTracker()
    tracker.register_token_map({"yes-1": "cond-a"})
    tracker.mark_dirty("unmapped-token")
    assert tracker.drain() == set()


def test_wake_event_fires_only_when_threshold_reached():
    tracker = DirtyMarketTracker(wake_threshold=2)
    tracker.register_token_map({"y1": "c1", "y2": "c2"})

    tracker.mark_dirty("y1")
    assert not tracker.wake_event.is_set()

    tracker.mark_dirty("y2")
    assert tracker.wake_event.is_set()


def test_drain_clears_wake_event():
    tracker = DirtyMarketTracker(wake_threshold=1)
    tracker.register_token_map({"y1": "c1"})
    tracker.mark_dirty("y1")
    assert tracker.wake_event.is_set()
    tracker.drain()
    assert not tracker.wake_event.is_set()
