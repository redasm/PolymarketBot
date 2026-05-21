"""Build JSON inputs for opt-in quant strategies from CSV or JSON rows."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.quant_strategy_config import (
    build_event_baselines_from_rows,
    build_logical_constraints_from_rows,
    build_wallet_observations_from_rows,
)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("rows", [])
        return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Build quant strategy JSON config from CSV/JSON rows")
    parser.add_argument("kind", choices=["event-baselines", "logical-constraints", "wallet-observations"])
    parser.add_argument("--input", required=True, help="CSV or JSON file")
    args = parser.parse_args()

    rows = _load_rows(Path(args.input))
    if args.kind == "event-baselines":
        payload = build_event_baselines_from_rows(rows)
    elif args.kind == "logical-constraints":
        payload = build_logical_constraints_from_rows(rows)
    else:
        payload = build_wallet_observations_from_rows(rows)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
