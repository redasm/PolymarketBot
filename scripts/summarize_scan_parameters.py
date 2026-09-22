"""Summarize parameter-scan robustness across datasets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.backtest.reports.stability import (
    build_parameter_stability_summary,
    collect_parameter_scan_rows,
    save_stability_summary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize parameter scans across datasets")
    parser.add_argument("--output-dir", default="research/backtest/output")
    parser.add_argument("--strategy-name", default=None)
    parser.add_argument("--filename", default="parameter_stability_summary.json")
    args = parser.parse_args()

    rows = collect_parameter_scan_rows(args.output_dir, strategy_name=args.strategy_name)
    summary = build_parameter_stability_summary(rows)
    path = save_stability_summary(summary, args.output_dir, filename=args.filename)
    print(json.dumps({"path": str(path), "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
