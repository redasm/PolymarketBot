"""定期清理 data 目录中的旧录制文件和缓存，避免长期运行占满磁盘."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOG = logging.getLogger(__name__)
_GIGABYTE = 1024 * 1024 * 1024


@dataclass
class CleanupStats:
    scope: str
    deleted_files: int = 0
    deleted_bytes: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "scope": self.scope,
            "deleted_files": self.deleted_files,
            "deleted_bytes": self.deleted_bytes,
        }


class DataJanitor:
    def __init__(
        self,
        *,
        enabled: bool,
        interval_sec: float,
        tick_dir: str,
        tick_retention_days: int,
        tick_max_gb: float,
        telemetry_dir: str,
        telemetry_retention_days: int,
        telemetry_max_gb: float,
        research_cache_dir: str,
        research_cache_retention_days: int,
        research_cache_max_gb: float,
        backtest_data_dir: str,
        backtest_retention_days: int,
        backtest_max_gb: float,
    ) -> None:
        self._enabled = enabled
        self._interval_sec = max(1.0, float(interval_sec))
        self._last_run_ts = 0.0
        self._jobs = [
            ("ticks", Path(tick_dir), tick_retention_days, tick_max_gb, None),
            ("telemetry", Path(telemetry_dir), telemetry_retention_days, telemetry_max_gb, None),
            (
                "research_cache",
                Path(research_cache_dir),
                research_cache_retention_days,
                research_cache_max_gb,
                None,
            ),
            ("backtest", Path(backtest_data_dir), backtest_retention_days, backtest_max_gb, None),
        ]

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def should_run(self) -> bool:
        return self._enabled and (time.time() - self._last_run_ts) >= self._interval_sec

    def run_once(self) -> list[CleanupStats]:
        if not self._enabled:
            return []
        self._last_run_ts = time.time()
        results: list[CleanupStats] = []
        for scope, root, retention_days, max_gb, exclude in self._jobs:
            stats = self._cleanup_directory(
                scope=scope,
                root=root,
                retention_days=retention_days,
                max_bytes=int(max_gb * _GIGABYTE),
                exclude=exclude,
            )
            results.append(stats)
        return results

    def _cleanup_directory(
        self,
        *,
        scope: str,
        root: Path,
        retention_days: int,
        max_bytes: int,
        exclude: Callable[[Path], bool] | None,
    ) -> CleanupStats:
        stats = CleanupStats(scope=scope)
        if not root.exists():
            return stats

        files = [
            path for path in root.rglob("*")
            if path.is_file() and not (exclude(path) if exclude is not None else False)
        ]
        now = time.time()
        if retention_days > 0:
            cutoff_ts = now - (retention_days * 86400)
            for path in list(files):
                try:
                    if path.stat().st_mtime < cutoff_ts:
                        size = path.stat().st_size
                        path.unlink(missing_ok=True)
                        stats.deleted_files += 1
                        stats.deleted_bytes += size
                        files.remove(path)
                except FileNotFoundError:
                    continue

        if max_bytes > 0:
            tracked = []
            total_bytes = 0
            for path in files:
                try:
                    file_stat = path.stat()
                except FileNotFoundError:
                    continue
                tracked.append((path, file_stat.st_mtime, file_stat.st_size))
                total_bytes += file_stat.st_size
            tracked.sort(key=lambda item: item[1])
            while total_bytes > max_bytes and tracked:
                path, _, size = tracked.pop(0)
                try:
                    path.unlink(missing_ok=True)
                    stats.deleted_files += 1
                    stats.deleted_bytes += size
                    total_bytes -= size
                except FileNotFoundError:
                    continue

        self._remove_empty_dirs(root, exclude=exclude)
        if stats.deleted_files > 0:
            LOG.info(
                "data cleanup complete: scope=%s deleted_files=%d deleted_bytes=%d",
                scope,
                stats.deleted_files,
                stats.deleted_bytes,
            )
        return stats

    def _remove_empty_dirs(self, root: Path, *, exclude: Callable[[Path], bool] | None) -> None:
        for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            if exclude is not None and exclude(path):
                continue
            try:
                next(path.iterdir())
            except StopIteration:
                path.rmdir()
            except (FileNotFoundError, OSError):
                continue
