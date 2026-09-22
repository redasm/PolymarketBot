"""Summarize runtime artifacts from logs, telemetry, and ticks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.runtime_analysis import summarize_runtime_artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize runtime logs, telemetry, and tick artifacts")
    parser.add_argument("--log-path", default="arb_bot.log")
    parser.add_argument("--telemetry-dir", default="data/telemetry")
    parser.add_argument("--ticks-dir", default="data/ticks")
    args = parser.parse_args()

    summary = summarize_runtime_artifacts(
        log_path=args.log_path,
        telemetry_dir=args.telemetry_dir,
        ticks_dir=args.ticks_dir,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
