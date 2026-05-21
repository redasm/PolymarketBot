"""Hot-reloadable local inputs for opt-in quant strategies."""

from __future__ import annotations

import json
import logging
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuantInputSnapshot:
    logical_constraints_json: str = ""
    event_baselines_json: str = ""
    wallet_alpha_profiles_json: str = ""
    wallet_alpha_observations_json: str = ""
    metadata: dict[str, dict[str, Any]] = None

    def input_metadata(self, name: str) -> dict[str, Any]:
        return dict((self.metadata or {}).get(name, {}))


@dataclass
class _FileValue:
    path: str = ""
    fallback: str = ""
    cached_text: str = ""
    cached_mtime_ns: int | None = None
    cached_size: int | None = None
    logged_bad_mtime_ns: int | None = None

    def read_with_metadata(self) -> tuple[str, dict[str, Any]]:
        if not self.path:
            return self.fallback, _metadata_for_text(self.fallback, source="inline", path="")
        file_path = Path(self.path)
        try:
            stat = file_path.stat()
        except OSError:
            text = self.cached_text or self.fallback
            source = "cached_file" if self.cached_text else "fallback"
            return text, _metadata_for_text(text, source=source, path=self.path)
        try:
            text = file_path.read_text(encoding="utf-8-sig").strip()
            json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            if self.logged_bad_mtime_ns != stat.st_mtime_ns:
                LOG.error("量化输入文件读取失败，保留上一次有效值: path=%s error=%s", self.path, exc)
                self.logged_bad_mtime_ns = stat.st_mtime_ns
            text = self.cached_text or self.fallback
            source = "cached_file" if self.cached_text else "fallback"
            return text, _metadata_for_text(text, source=source, path=self.path)
        self.cached_text = text
        self.cached_mtime_ns = stat.st_mtime_ns
        self.cached_size = stat.st_size
        self.logged_bad_mtime_ns = None
        return text, _metadata_for_text(text, source="file", path=self.path, mtime_ns=stat.st_mtime_ns)


class QuantInputStore:
    """Read dynamic JSON inputs from files with inline env fallback."""

    def __init__(
        self,
        *,
        logical_constraints_json: str = "",
        event_baselines_json: str = "",
        wallet_alpha_profiles_json: str = "",
        wallet_alpha_observations_json: str = "",
        logical_constraints_file: str = "",
        event_baselines_file: str = "",
        wallet_alpha_profiles_file: str = "",
        wallet_alpha_observations_file: str = "",
    ) -> None:
        self._logical_constraints = _FileValue(logical_constraints_file, logical_constraints_json)
        self._event_baselines = _FileValue(event_baselines_file, event_baselines_json)
        self._wallet_profiles = _FileValue(wallet_alpha_profiles_file, wallet_alpha_profiles_json)
        self._wallet_observations = _FileValue(wallet_alpha_observations_file, wallet_alpha_observations_json)

    @classmethod
    def from_config(cls, config) -> "QuantInputStore":
        return cls(
            logical_constraints_json=config.logical_constraints_json,
            event_baselines_json=config.event_baselines_json,
            wallet_alpha_profiles_json=config.wallet_alpha_profiles_json,
            wallet_alpha_observations_json=config.wallet_alpha_observations_json,
            logical_constraints_file=getattr(config, "logical_constraints_file", ""),
            event_baselines_file=getattr(config, "event_baselines_file", ""),
            wallet_alpha_profiles_file=getattr(config, "wallet_alpha_profiles_file", ""),
            wallet_alpha_observations_file=getattr(config, "wallet_alpha_observations_file", ""),
        )

    def snapshot(self) -> QuantInputSnapshot:
        logical_constraints, logical_meta = self._logical_constraints.read_with_metadata()
        event_baselines, event_meta = self._event_baselines.read_with_metadata()
        wallet_profiles, wallet_profiles_meta = self._wallet_profiles.read_with_metadata()
        wallet_observations, wallet_observations_meta = self._wallet_observations.read_with_metadata()
        return QuantInputSnapshot(
            logical_constraints_json=logical_constraints,
            event_baselines_json=event_baselines,
            wallet_alpha_profiles_json=wallet_profiles,
            wallet_alpha_observations_json=wallet_observations,
            metadata={
                "logical_constraints": logical_meta,
                "event_baselines": event_meta,
                "wallet_alpha_profiles": wallet_profiles_meta,
                "wallet_alpha_observations": wallet_observations_meta,
            },
        )


def _metadata_for_text(
    text: str,
    *,
    source: str,
    path: str,
    mtime_ns: int | None = None,
) -> dict[str, Any]:
    encoded = (text or "").encode("utf-8")
    out: dict[str, Any] = {
        "source": source,
        "path": path,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }
    if mtime_ns is not None:
        out["mtime_ns"] = int(mtime_ns)
    return out
