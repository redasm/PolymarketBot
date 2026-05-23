#!/usr/bin/env python3
"""入口：机器人主循环 + LLM 量化输入旁路.

主循环热加载 JSON；两个旁路 worker 负责调用 LLM 生成/刷新这些 JSON。
这样操作者只需运行根目录脚本，盘口扫描仍不会被 LLM 延迟阻塞。
"""

from __future__ import annotations

import argparse
import sys

from scripts.run_automated_quant_pipeline import (
    ProcessSpec,
    _bot_env,
    _bot_spec,
    _resolve_path,
    run_supervisor,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one bot plus logical-rule, event-baseline and research-feeds LLM workers",
    )
    parser.add_argument("--dotenv-path", default=".env")
    parser.add_argument("--quant-input-dir", default="data/quant_inputs")
    parser.add_argument("--telemetry-dir", default="data/telemetry")
    parser.add_argument("--logical-interval-sec", type=float, default=1800.0)
    parser.add_argument("--event-baseline-interval-sec", type=float, default=1800.0)
    parser.add_argument("--research-feeds-interval-sec", type=float, default=3600.0)
    parser.add_argument("--logical-event-limit", type=int, default=100)
    parser.add_argument("--logical-max-candidates", type=int, default=40)
    parser.add_argument("--event-baseline-event-limit", type=int, default=100)
    parser.add_argument("--event-baseline-max-candidates", type=int, default=40)
    parser.add_argument("--research-feeds-max", type=int, default=12)
    parser.add_argument(
        "--research-feeds-http-timeout-sec",
        type=float,
        default=8.0,
        help="Per-template HTTP probe timeout when validating LLM-proposed RSS feeds",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without starting processes")
    args = parser.parse_args()

    specs = build_process_specs(args)
    if args.dry_run:
        for spec in specs:
            print(f"{spec.name}: {' '.join(spec.args)}")
        return 0
    return run_supervisor(specs)


def build_process_specs(args: argparse.Namespace) -> list[ProcessSpec]:
    quant_dir = _resolve_path(args.quant_input_dir)
    telemetry_dir = _resolve_path(args.telemetry_dir)
    quant_dir.mkdir(parents=True, exist_ok=True)

    logical_candidates_file = quant_dir / "logical_candidates.json"
    logical_file = quant_dir / "logical_constraints.json"
    logical_status_file = quant_dir / "logical_rules_status.json"
    event_baseline_candidates_file = quant_dir / "event_baseline_candidates.json"
    baselines_file = quant_dir / "event_baselines.json"
    baselines_status_file = quant_dir / "event_baselines_status.json"
    research_feeds_file = quant_dir / "research_feeds.json"
    research_feeds_status_file = quant_dir / "research_feeds_status.json"
    logical_rules_expires_sec = max(
        float(args.logical_interval_sec) * 2.0,
        float(args.logical_interval_sec) + 300.0,
    )

    bot_env = _bot_env(
        dotenv_path=_resolve_path(args.dotenv_path),
        overrides={
            "LOGICAL_CONSTRAINTS_FILE": str(logical_file),
            "EVENT_BASELINES_FILE": str(baselines_file),
            "RESEARCH_SIGNAL_FEEDS_FILE": str(research_feeds_file),
            "TELEMETRY_RECORD_DIR": str(telemetry_dir),
        },
    )

    return [
        _bot_spec("bot", env=bot_env),
        ProcessSpec(
            "logical-rules-llm",
            [
                sys.executable,
                "scripts/scan_quant_strategy_inputs.py",
                "logical-rules-auto",
                "--dotenv-path",
                str(_resolve_path(args.dotenv_path)),
                "--fetch-gamma",
                "--event-limit",
                str(args.logical_event_limit),
                "--max-candidates",
                str(args.logical_max_candidates),
                "--repeat-interval-sec",
                str(args.logical_interval_sec),
                "--rules-expires-sec",
                str(logical_rules_expires_sec),
                "--repeat-count",
                "0",
                "--candidates-output",
                str(logical_candidates_file),
                "--status-output",
                str(logical_status_file),
                "--output",
                str(logical_file),
            ],
            critical=False,
        ),
        ProcessSpec(
            "event-baselines-llm",
            [
                sys.executable,
                "scripts/scan_quant_strategy_inputs.py",
                "event-baselines-auto",
                "--dotenv-path",
                str(_resolve_path(args.dotenv_path)),
                "--fetch-gamma",
                "--event-limit",
                str(args.event_baseline_event_limit),
                "--max-candidates",
                str(args.event_baseline_max_candidates),
                "--repeat-interval-sec",
                str(args.event_baseline_interval_sec),
                "--repeat-count",
                "0",
                "--candidates-output",
                str(event_baseline_candidates_file),
                "--status-output",
                str(baselines_status_file),
                "--output",
                str(baselines_file),
            ],
            critical=False,
        ),
        ProcessSpec(
            "research-feeds-llm",
            [
                sys.executable,
                "scripts/scan_quant_strategy_inputs.py",
                "research-feeds-auto",
                "--dotenv-path",
                str(_resolve_path(args.dotenv_path)),
                "--max-feeds",
                str(args.research_feeds_max),
                "--http-timeout-sec",
                str(args.research_feeds_http_timeout_sec),
                "--repeat-interval-sec",
                str(args.research_feeds_interval_sec),
                "--repeat-count",
                "0",
                "--status-output",
                str(research_feeds_status_file),
                "--output",
                str(research_feeds_file),
            ],
            critical=False,
        ),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
