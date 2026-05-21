"""Print opt-in config snippets for quant strategy shadow runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.quant_strategy_config import build_quant_strategy_env_template, format_env_template


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate quant strategy config templates")
    parser.add_argument("--format", choices=["env", "json"], default="env")
    args = parser.parse_args()

    values = build_quant_strategy_env_template()
    if args.format == "json":
        print(json.dumps(values, ensure_ascii=False, indent=2))
    else:
        print(format_env_template(values), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
