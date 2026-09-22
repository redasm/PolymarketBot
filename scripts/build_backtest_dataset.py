"""Build a backtest dataset from recorded tick NDJSON files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.backtest.datasets import build_binary_snapshots_from_ticks


def main() -> int:
    parser = argparse.ArgumentParser(description="Build backtest dataset from tick recorder NDJSON")
    parser.add_argument("--ticks", required=True, help="Path to tick NDJSON file")
    parser.add_argument("--output-dir", required=True, help="Dataset directory to write into")
    parser.add_argument("--condition-id", default=None)
    parser.add_argument("--event-id", default=None)
    parser.add_argument("--question", default=None)
    parser.add_argument("--slug", default=None)
    parser.add_argument("--condition-id-filter", default=None, help="Only build snapshots for a single condition_id")
    args = parser.parse_args()

    path = build_binary_snapshots_from_ticks(
        args.ticks,
        args.output_dir,
        condition_id=args.condition_id,
        event_id=args.event_id,
        question=args.question,
        slug=args.slug,
        condition_id_filter=args.condition_id_filter,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
