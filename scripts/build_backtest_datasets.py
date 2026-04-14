"""Batch-build backtest datasets from all tick recorder files in a directory."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.backtest.datasets import build_binary_snapshots_from_ticks


def _discover_condition_ids(path: Path) -> list[str]:
    ids = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            import json

            row = json.loads(line)
        except Exception:
            continue
        condition_id = str(row.get("condition_id") or "").strip()
        if condition_id and condition_id not in seen:
            seen.add(condition_id)
            ids.append(condition_id)
    return ids


def _extract_condition_metadata(path: Path, condition_id: str) -> dict[str, str]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("condition_id") or "").strip() != condition_id:
            continue
        return {
            "question": str(row.get("question") or "").strip(),
            "slug": str(row.get("slug") or "").strip(),
        }
    return {}


def _slugify(value: str, *, max_len: int = 36) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return (cleaned[:max_len] or "market").rstrip("-")


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-build backtest datasets from tick NDJSON files")
    parser.add_argument("--ticks-dir", required=True, help="Directory containing tick NDJSON files")
    parser.add_argument("--output-root", required=True, help="Directory where datasets will be created")
    parser.add_argument("--prefix", default="from_ticks")
    parser.add_argument("--allow-heuristic-role-map", action="store_true", help="Allow unlabeled legacy tick files")
    args = parser.parse_args()

    ticks_dir = Path(args.ticks_dir)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    built = []
    for path in sorted(ticks_dir.glob("*.ndjson")):
        condition_ids = _discover_condition_ids(path)
        if not condition_ids:
            dataset_name = f"{args.prefix}_{path.stem}"
            dataset_dir = output_root / dataset_name
            try:
                out_path = build_binary_snapshots_from_ticks(
                    path,
                    dataset_dir,
                    condition_id=f"{dataset_name}-cond",
                    event_id=f"{dataset_name}-event",
                    question=f"Recovered binary market {path.stem}",
                    slug=f"recovered-binary-market-{path.stem}",
                    strict=not args.allow_heuristic_role_map,
                )
            except ValueError as exc:
                raise SystemExit(
                    f"{path.name}: {exc}. Use --allow-heuristic-role-map only for legacy unlabeled files."
                ) from exc
            built.append(str(out_path))
            continue
        for condition_id in condition_ids:
            metadata = _extract_condition_metadata(path, condition_id)
            label = _slugify(metadata.get("slug") or metadata.get("question") or condition_id[:12])
            dataset_name = f"{args.prefix}_{path.stem}_{label}"
            dataset_dir = output_root / dataset_name
            try:
                out_path = build_binary_snapshots_from_ticks(
                    path,
                    dataset_dir,
                    condition_id=f"{dataset_name}-cond",
                    event_id=f"{dataset_name}-event",
                    question=metadata.get("question") or f"Recovered binary market {path.stem}",
                    slug=metadata.get("slug") or f"recovered-binary-market-{path.stem}",
                    condition_id_filter=condition_id,
                    strict=not args.allow_heuristic_role_map,
                )
            except ValueError as exc:
                raise SystemExit(
                    f"{path.name} [{condition_id[:12]}]: {exc}. Use --allow-heuristic-role-map only for legacy unlabeled files."
                ) from exc
            built.append(str(out_path))

    for item in built:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
