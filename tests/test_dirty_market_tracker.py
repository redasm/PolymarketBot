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


def test_drain_clear_is_atomic_with_set_swap():
    """Regression for the lost-wakeup race fixed in P1.

    Before the fix, drain() released the lock between emptying the
    set and clearing wake_event, leaving a window where a concurrent
    mark_dirty()+set() would be wiped by drain's lagging clear().

    With clear() inside the lock, the post-drain state is well-defined:
    - dirty_set is empty
    - wake_event is cleared
    - the lock has been released

    A mark_dirty that runs AFTER drain releases the lock will then:
    - acquire lock, add cond, release
    - call wake_event.set() — guaranteed to land AFTER drain's clear,
      so the signal survives.

    We assert this property directly: after drain, mark_dirty must
    leave wake_event in the set state.
    """
    tracker = DirtyMarketTracker(wake_threshold=1)
    tracker.register_token_map({"yA": "cA", "yB": "cB"})

    tracker.mark_dirty("yA")
    drained = tracker.drain()
    assert drained == {"cA"}
    assert not tracker.wake_event.is_set()

    # A WS callback that lands immediately after drain returns must
    # re-arm the wake event so the main loop's next wait() doesn't
    # sleep through the dirty marker.
    tracker.mark_dirty("yB")
    assert tracker.wake_event.is_set(), (
        "wake signal for cond cB was lost — main loop would sleep "
        "through dirty markets"
    )
    assert tracker.drain() == {"cB"}


def test_reset_clears_wake_event_under_lock():
    """``reset()`` is a stronger version of drain — it must leave the
    structure in the same well-defined post-state (empty set, cleared
    event) without the same lost-wakeup window.
    """
    tracker = DirtyMarketTracker(wake_threshold=1)
    tracker.register_token_map({"yA": "cA"})
    tracker.mark_dirty("yA")
    assert tracker.wake_event.is_set()

    tracker.reset()
    assert tracker.peek_size() == 0
    assert not tracker.wake_event.is_set()

    # Post-reset mark_dirty must still arm the event.
    tracker.register_token_map({"yA": "cA"})
    tracker.mark_dirty("yA")
    assert tracker.wake_event.is_set()
