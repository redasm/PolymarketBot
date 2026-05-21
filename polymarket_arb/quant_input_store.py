"""Hot-reloadable local inputs for opt-in quant strategies."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuantInputSnapshot:
    logical_constraints_json: str = ""
    event_baselines_json: str = ""
    wallet_alpha_profiles_json: str = ""
    wallet_alpha_observations_json: str = ""


@dataclass
class _FileValue:
    path: str = ""
    fallback: str = ""
    cached_text: str = ""
    cached_mtime_ns: int | None = None

    def read(self) -> str:
        if not self.path:
            return self.fallback
        file_path = Path(self.path)
        try:
            stat = file_path.stat()
        except OSError:
            return self.cached_text or self.fallback
        if self.cached_mtime_ns == stat.st_mtime_ns and self.cached_text:
            return self.cached_text
        try:
            text = file_path.read_text(encoding="utf-8-sig").strip()
            json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("量化输入文件读取失败，保留上一次有效值: path=%s error=%s", self.path, exc)
            return self.cached_text or self.fallback
        self.cached_text = text
        self.cached_mtime_ns = stat.st_mtime_ns
        return text


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
        return QuantInputSnapshot(
            logical_constraints_json=self._logical_constraints.read(),
            event_baselines_json=self._event_baselines.read(),
            wallet_alpha_profiles_json=self._wallet_profiles.read(),
            wallet_alpha_observations_json=self._wallet_observations.read(),
        )
