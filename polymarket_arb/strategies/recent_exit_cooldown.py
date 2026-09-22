"""Cross-restart cooldown for markets we just exited.

Without this store the bot will reliably re-enter the *same* directional
position immediately after a restart — the in-memory T2 state is wiped,
the statistical detector recomputes the same deviation it saw before,
and the orchestrator (also restarted, with empty rate-cap state) lets
the first signal through. The user's manual close-out gets undone on
the next launch.

This module persists `{condition_id: last_exit_ts}` to a JSON file under
`data/telemetry/`. ``RecentExitCooldownStore.in_cooldown()`` returns
true while the gap is shorter than ``cooldown_sec``.

Recorded triggers (callers must invoke ``record_exit``):
  - T2 successful exit (`status == "exited"`).
  - T2 abandoned position (escalation path released exposure).

Not recorded:
  - Failed exit attempts (we still *want* to exit, retry path handles it).
  - Partial exits (not yet fully out).
  - T0/T1/T3 trades (different lifecycle).

Failure modes are non-fatal: if the state file is missing or corrupted
we start with an empty map; if disk write fails we LOG.warning and keep
running with in-memory state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


class RecentExitCooldownStore:
    """Persistent map of condition_id -> last successful-exit timestamp."""

    def __init__(self, *, state_file: str, cooldown_sec: float):
        self._state_file = Path(state_file) if state_file else None
        self._cooldown_sec = max(0.0, float(cooldown_sec))
        # Serialise reads + writes. The bot's main loop is single-threaded
        # for T2 work today, but t2_exit_manager.evaluate() and the entry
        # gate could end up on different threads in the future.
        self._lock = threading.RLock()
        self._entries: dict[str, float] = self._load()

    @property
    def cooldown_sec(self) -> float:
        return self._cooldown_sec

    def record_exit(self, condition_id: str, now_ts: float | None = None) -> None:
        if not condition_id or self._cooldown_sec <= 0:
            return
        ts = float(now_ts) if now_ts is not None else time.time()
        with self._lock:
            self._entries[str(condition_id)] = ts
            self._prune_locked(ts)
            self._save_locked()

    def in_cooldown(
        self, condition_id: str, now_ts: float | None = None
    ) -> tuple[bool, float]:
        """Return ``(blocked, remaining_sec)``.

        ``remaining_sec`` is 0 when ``blocked`` is False, otherwise the
        seconds left before the cooldown expires. Callers log this so
        operators can see *why* a signal was rejected.
        """
        if not condition_id or self._cooldown_sec <= 0:
            return False, 0.0
        ts = float(now_ts) if now_ts is not None else time.time()
        with self._lock:
            last_exit = self._entries.get(str(condition_id))
            if last_exit is None:
                return False, 0.0
            elapsed = ts - last_exit
            if elapsed >= self._cooldown_sec:
                return False, 0.0
            return True, max(0.0, self._cooldown_sec - elapsed)

    def snapshot(self) -> dict[str, float]:
        """Return a shallow copy. Tests use this to assert state."""
        with self._lock:
            return dict(self._entries)

    def _load(self) -> dict[str, float]:
        if self._state_file is None or not self._state_file.exists():
            return {}
        try:
            with self._state_file.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning(
                "RecentExitCooldownStore: 状态文件读取失败 (%s)，以空状态启动: %s",
                self._state_file,
                exc,
            )
            return {}
        entries_raw = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries_raw, dict):
            return {}
        clean: dict[str, float] = {}
        for k, v in entries_raw.items():
            try:
                clean[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return clean

    def _prune_locked(self, now_ts: float) -> None:
        """Drop entries older than 2× cooldown so the file doesn't grow forever."""
        if self._cooldown_sec <= 0:
            return
        cutoff = now_ts - (self._cooldown_sec * 2)
        self._entries = {k: v for k, v in self._entries.items() if v >= cutoff}

    def _save_locked(self) -> None:
        if self._state_file is None:
            return
        payload = {"entries": dict(self._entries)}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to tmp in the same dir, then rename.
            # Same-dir rename is atomic on every supported FS (Win + POSIX).
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".recent_exits.",
                suffix=".tmp",
                dir=str(self._state_file.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, sort_keys=True)
                os.replace(tmp_path, self._state_file)
            except Exception:
                # Best-effort cleanup of the temp file on failure.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            LOG.warning(
                "RecentExitCooldownStore: 状态文件写入失败 (%s): %s",
                self._state_file,
                exc,
            )


def make_recent_exit_cooldown_store(config: Any) -> RecentExitCooldownStore:
    """Convenience factory wired from `ArbConfig`."""
    return RecentExitCooldownStore(
        state_file=str(getattr(config, "t2_recent_exits_state_file", "") or ""),
        cooldown_sec=float(getattr(config, "t2_post_exit_cooldown_sec", 0.0) or 0.0),
    )
