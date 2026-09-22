"""轻量事件录制器：将机会/交易/风控事件写入 NDJSON，便于离线分析."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


DEFAULT_SCHEMA_VERSION = 2
_OVERFLOW_LOG_INTERVAL_SEC = 10.0


# Poison pill: a tuple whose first element is None signals the writer
# thread to flush and exit. Using a sentinel keeps `Queue.get()` typed
# without a separate "stop" event.
_POISON: tuple[None, None] = (None, None)


class EventRecorder:
    """按类别写 NDJSON 事件流.

    Two execution modes:

    - **Synchronous** (``async_write=False``, default): every
      :meth:`write_event` call serializes, writes, and ``flush()``-es
      to disk inline under ``self._lock``. This is the historical
      behaviour and is the safest choice for tests and one-off
      scripts that read files immediately after writing.

    - **Asynchronous** (``async_write=True``): producers serialize
      and enqueue bytes; a single daemon thread drains the queue,
      writes, and only flushes when the queue empties. Under bursty
      cycles (T0 sweep emitting cycle_metrics, signals, executions,
      and risk_events back-to-back) this drops main-thread blocking
      time from ~milliseconds per event to ~microseconds. If the
      queue is full the *oldest* event is dropped — telemetry is
      best-effort, and newer events are more relevant for live
      debugging.

    The async path is opt-in through ``RECORDER_ASYNC_WRITE`` so
    operators who want strict ordering (e.g. forensic replay) can
    keep the default. ``close()`` always drains the queue before
    returning so end-of-run summaries don't lose data.
    """

    def __init__(
        self,
        output_dir: str = "data/telemetry",
        enabled: bool = False,
        max_file_size_mb: float = 100.0,
        async_write: bool = False,
        queue_size: int = 10000,
    ) -> None:
        self._enabled = enabled
        self._output_dir = Path(output_dir)
        self._max_bytes = int(max_file_size_mb * 1024 * 1024)
        self._lock = threading.Lock()
        self._files: dict[str, Any] = {}
        self._file_dates: dict[str, str] = {}
        self._bytes_written: dict[str, int] = {}
        self._event_count = 0
        self._dropped_events = 0
        self._last_overflow_log_ts = 0.0
        self._last_overflow_log_dropped = 0

        self._async_write = bool(async_write and enabled)
        self._queue: queue.Queue[tuple[str | None, bytes | None]] | None = None
        self._writer_thread: threading.Thread | None = None

        if self._enabled:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            LOG.info(
                "event_recorder enabled, output_dir=%s async=%s",
                self._output_dir,
                self._async_write,
            )

        if self._async_write:
            self._queue = queue.Queue(maxsize=max(100, int(queue_size)))
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name="event-recorder-writer",
                daemon=True,
            )
            self._writer_thread.start()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    def write_event(self, category: str, payload: dict[str, Any]) -> None:
        if not self._enabled:
            return
        safe_payload = dict(payload)
        if "ts" in safe_payload:
            safe_payload.setdefault("payload_ts", safe_payload.pop("ts"))
        if "category" in safe_payload:
            safe_payload.setdefault("payload_category", safe_payload.pop("category"))
        try:
            schema_version = int(safe_payload.pop("schema_version", DEFAULT_SCHEMA_VERSION))
        except (TypeError, ValueError):
            schema_version = DEFAULT_SCHEMA_VERSION
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "schema_version": schema_version,
            **safe_payload,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")

        if self._async_write and self._queue is not None:
            try:
                self._queue.put_nowait((category, encoded))
                return
            except queue.Full:
                # Drop oldest to make room — keeps the most recent
                # events for live forensic value. Atomic in queue.Queue
                # because each individual op holds the queue's lock.
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    pass
                self._dropped_events += 1
                try:
                    self._queue.put_nowait((category, encoded))
                except queue.Full:
                    # Should be impossible but stay safe.
                    self._dropped_events += 1
                self._log_overflow_if_due(category)
                return

        self._write_sync(category, encoded, flush=True)

    def _write_sync(self, category: str, encoded: bytes, *, flush: bool) -> None:
        with self._lock:
            handle = self._ensure_file(category)
            handle.write(encoded)
            if flush:
                handle.flush()
            self._bytes_written[category] += len(encoded)
            self._event_count += 1
            if self._bytes_written[category] >= self._max_bytes:
                self._rotate(category)

    def _writer_loop(self) -> None:
        assert self._queue is not None
        while True:
            item = self._queue.get()
            try:
                if item[0] is None:
                    self._flush_all()
                    return
                category, encoded = item
                self._write_sync(category, encoded, flush=False)
                if self._queue.empty():
                    self._flush_all()
            except Exception as exc:
                LOG.warning("event_recorder writer error: %s", exc)
            finally:
                self._queue.task_done()

    def _log_overflow_if_due(self, category: str) -> None:
        now = time.monotonic()
        if (
            self._last_overflow_log_ts
            and now - self._last_overflow_log_ts < _OVERFLOW_LOG_INTERVAL_SEC
        ):
            return
        dropped_since_last = self._dropped_events - self._last_overflow_log_dropped
        self._last_overflow_log_ts = now
        self._last_overflow_log_dropped = self._dropped_events
        LOG.warning(
            "event_recorder queue overflow: category=%s dropped_events=%d dropped_since_last=%d",
            category,
            self._dropped_events,
            dropped_since_last,
        )

    def _flush_all(self) -> None:
        with self._lock:
            for handle in self._files.values():
                try:
                    handle.flush()
                except Exception:
                    pass

    def close(self) -> None:
        if self._async_write and self._writer_thread is not None and self._queue is not None:
            try:
                self._queue.put(_POISON, timeout=2.0)
            except queue.Full:
                LOG.warning("event_recorder queue full at shutdown; some events may be lost")
            self._writer_thread.join(timeout=10.0)
        with self._lock:
            for handle in self._files.values():
                handle.flush()
                handle.close()
            self._files.clear()
            self._file_dates.clear()
            self._bytes_written.clear()
        if self._enabled:
            LOG.info(
                "event_recorder closed, total events recorded: %d, dropped: %d",
                self._event_count,
                self._dropped_events,
            )

    def _ensure_file(self, category: str):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if category not in self._files or self._file_dates.get(category) != today:
            self._close_category(category)
            path = self._output_dir / f"{today}.{category}.ndjson"
            handle = open(path, "ab")
            self._files[category] = handle
            self._file_dates[category] = today
            self._bytes_written[category] = path.stat().st_size if path.exists() else 0
            LOG.info("event_recorder opened %s", path)
        return self._files[category]

    def _rotate(self, category: str) -> None:
        self._close_category(category)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        seq = 1
        while True:
            path = self._output_dir / f"{today}.{category}.{seq}.ndjson"
            if not path.exists():
                break
            seq += 1
        handle = open(path, "ab")
        self._files[category] = handle
        self._file_dates[category] = today
        self._bytes_written[category] = 0
        LOG.info("event_recorder rotated to %s", path)

    def _close_category(self, category: str) -> None:
        handle = self._files.pop(category, None)
        if handle is not None:
            handle.flush()
            handle.close()
        self._file_dates.pop(category, None)
        self._bytes_written.pop(category, None)
