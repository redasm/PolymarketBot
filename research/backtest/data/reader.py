"""Minimal backtest dataset reader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.backtest.data.schemas import DatasetPaths, build_dataset_paths


class BacktestDatasetReader:
    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)

    def get_paths(self, dataset_name: str) -> DatasetPaths:
        return build_dataset_paths(self._data_dir, dataset_name)

    def load_jsonl(self, path: str | Path) -> list[dict[str, Any]]:
        file_path = Path(path)
        if not file_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in file_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        return rows

    def load_t0_market_states(self, dataset_name: str) -> list[dict[str, Any]]:
        """Load time-ordered market states for T0 binary backtests."""
        paths = self.get_paths(dataset_name)
        snapshot_rows = self.load_jsonl(paths.market_snapshots)
        event_rows = self.load_jsonl(paths.orderbook_events)

        if snapshot_rows:
            return sorted(snapshot_rows, key=lambda row: int(row.get("ts_ms", 0)))
        if not event_rows:
            return []

        state_by_condition: dict[str, dict[str, Any]] = {}
        emitted: list[dict[str, Any]] = []

        for event in sorted(event_rows, key=lambda row: int(row.get("ts_ms", 0))):
            condition_id = event.get("condition_id") or ""
            token_role = event.get("token_role") or ""
            side = event.get("side") or ""
            if not condition_id or token_role not in {"yes", "no"}:
                continue

            state = state_by_condition.setdefault(
                condition_id,
                {
                    "ts_ms": int(event.get("ts_ms", 0)),
                    "condition_id": condition_id,
                    "event_id": event.get("event_id", condition_id),
                    "question": event.get("question", condition_id),
                    "yes_token_id": event.get("yes_token_id", "yes"),
                    "no_token_id": event.get("no_token_id", "no"),
                },
            )
            state["ts_ms"] = int(event.get("ts_ms", 0))
            field_prefix = f"{token_role}_best"
            size_prefix = f"{token_role}_{'bid' if side == 'buy' else 'ask'}_size"
            if side == "buy":
                state[f"{field_prefix}_bid"] = float(event.get("price", 0))
            elif side == "sell":
                state[f"{field_prefix}_ask"] = float(event.get("price", 0))
            state[size_prefix] = float(event.get("size", 0))

            required = [
                "yes_best_bid",
                "yes_best_ask",
                "no_best_bid",
                "no_best_ask",
            ]
            if all(key in state for key in required):
                emitted.append(dict(state))

        return emitted
