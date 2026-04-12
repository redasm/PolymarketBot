"""轻量事件录制器：将机会/交易/AI/风控事件写入 NDJSON，便于离线分析."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


class EventRecorder:
    """按类别写 NDJSON 事件流."""

    def __init__(
        self,
        output_dir: str = "data/telemetry",
        enabled: bool = False,
        max_file_size_mb: float = 100.0,
    ) -> None:
        self._enabled = enabled
        self._output_dir = Path(output_dir)
        self._max_bytes = int(max_file_size_mb * 1024 * 1024)
        self._lock = threading.Lock()
        self._files: dict[str, Any] = {}
        self._file_dates: dict[str, str] = {}
        self._bytes_written: dict[str, int] = {}
        self._event_count = 0

        if self._enabled:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            LOG.info("event_recorder enabled, output_dir=%s", self._output_dir)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def event_count(self) -> int:
        return self._event_count

    def write_event(self, category: str, payload: dict[str, Any]) -> None:
        if not self._enabled:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            handle = self._ensure_file(category)
            handle.write(encoded)
            handle.flush()
            self._bytes_written[category] += len(encoded)
            self._event_count += 1
            if self._bytes_written[category] >= self._max_bytes:
                self._rotate(category)

    def close(self) -> None:
        with self._lock:
            for handle in self._files.values():
                handle.flush()
                handle.close()
            self._files.clear()
            self._file_dates.clear()
            self._bytes_written.clear()
        if self._enabled:
            LOG.info("event_recorder closed, total events recorded: %d", self._event_count)

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
