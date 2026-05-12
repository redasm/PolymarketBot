"""Dirty-market tracker for event-driven T0 prioritization.

When a WebSocket delta changes a token's best bid/ask, the natural
next step is to re-run T0 detection on the market that owns that
token — not 3 seconds later when the next polling cycle starts.

This module implements the thread-safe glue layer between the
``orderbook-callbacks`` thread (producer) and the main loop
(consumer). It deliberately does *not* run detection itself: the
detector / risk / execution stack is not designed for concurrent
access, so callbacks only publish "this market deserves re-scan
soon" and the main loop drains the set at the top of the next
cycle (and is woken early via :pyattr:`wake_event` if enough
markets accumulate).

Design:

- ``mark_dirty(token_id)`` is cheap and lock-protected; it's the
  only thing the WS callback thread does.
- ``register_token_map`` is called by the main loop after every
  universe / hot-pool refresh so the producer knows which
  ``condition_id`` a token belongs to.
- ``drain`` returns the accumulated condition_id set and resets
  state. The main loop reorders ``scanned_markets`` so dirty
  markets are scanned first inside the cycle.
- ``wake_event`` is a :class:`threading.Event` the main loop can
  wait on; when ``wake_threshold`` markets accumulate before the
  cycle's sleep window expires, the producer sets the event so
  the loop wakes up immediately and starts the next cycle.
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional


class DirtyMarketTracker:
    """Thread-safe staging area for ``condition_id``s whose books moved."""

    def __init__(self, *, wake_threshold: int = 1) -> None:
        # ``wake_threshold = 1`` means "wake the main loop as soon as
        # any market becomes dirty". Operators with very high WS
        # traffic can raise this to batch dirty events into bigger
        # cycle waves and avoid hammering the scan path.
        self._wake_threshold = max(1, int(wake_threshold))
        self._lock = threading.Lock()
        self._token_to_condition: dict[str, str] = {}
        self._dirty_conditions: set[str] = set()
        self.wake_event = threading.Event()

    @property
    def wake_threshold(self) -> int:
        return self._wake_threshold

    def set_wake_threshold(self, threshold: int) -> None:
        with self._lock:
            self._wake_threshold = max(1, int(threshold))

    def register_token_map(self, token_to_condition: dict[str, str]) -> None:
        """Replace the ``token_id → condition_id`` lookup atomically.

        Called by the main loop after every universe refresh so the
        producer always has the current hot-pool mapping. Unknown
        tokens (e.g. mid-flight subscriptions) silently no-op in
        :meth:`mark_dirty` rather than blow up — callbacks are best
        effort, not correctness-critical.
        """
        with self._lock:
            self._token_to_condition = dict(token_to_condition)

    def mark_dirty(self, token_id: str) -> None:
        """Note that ``token_id``'s book changed.

        Producer-side hot path: must stay cheap because the WS
        callback thread executes this on every best bid/ask delta.
        Setting ``wake_event`` is idempotent so contention is bounded
        even under burst traffic.
        """
        wake = False
        with self._lock:
            condition_id = self._token_to_condition.get(token_id)
            if condition_id is None:
                return
            self._dirty_conditions.add(condition_id)
            if len(self._dirty_conditions) >= self._wake_threshold:
                wake = True
        if wake:
            self.wake_event.set()

    def drain(self) -> set[str]:
        """Return + clear the dirty-condition set in one lock pass."""
        with self._lock:
            drained = self._dirty_conditions
            self._dirty_conditions = set()
        self.wake_event.clear()
        return drained

    def peek_size(self) -> int:
        with self._lock:
            return len(self._dirty_conditions)

    def reset(self, *, token_map: Optional[Iterable[tuple[str, str]]] = None) -> None:
        """Reset state; used by tests and on universe re-init."""
        with self._lock:
            self._dirty_conditions.clear()
            if token_map is not None:
                self._token_to_condition = dict(token_map)
        self.wake_event.clear()
