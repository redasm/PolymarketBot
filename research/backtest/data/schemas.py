"""Canonical schemas for backtest datasets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatasetPaths:
    dataset_name: str
    root_dir: Path
    market_snapshots: Path
    orderbook_events: Path
    trade_events: Path
    event_metadata: Path


def build_dataset_paths(root_dir: str | Path, dataset_name: str) -> DatasetPaths:
    root = Path(root_dir) / dataset_name
    return DatasetPaths(
        dataset_name=dataset_name,
        root_dir=root,
        market_snapshots=root / "market_snapshots.jsonl",
        orderbook_events=root / "orderbook_events.jsonl",
        trade_events=root / "trade_events.jsonl",
        event_metadata=root / "event_metadata.jsonl",
    )
