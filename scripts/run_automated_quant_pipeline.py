"""Run one bot plus non-blocking quant input data workers."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_ONLY_SENTINEL = "__ENV_ONLY__"


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    args: list[str]
    env: dict[str, str] | None = None
    critical: bool = False
    restart_delay_sec: float = 5.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one bot, wallet scanner, and wallet promoter continuously",
    )
    parser.add_argument("--dotenv-path", default=".env")
    parser.add_argument("--quant-input-dir", default="data/quant_inputs")
    parser.add_argument("--telemetry-dir", default="data/telemetry")
    parser.add_argument("--scanner-interval-sec", type=float, default=120.0)
    parser.add_argument("--logical-interval-sec", type=float, default=1800.0)
    parser.add_argument("--event-baseline-interval-sec", type=float, default=1800.0)
    parser.add_argument("--promoter-interval-sec", type=float, default=300.0)
    parser.add_argument("--markout-interval-sec", type=float, default=300.0)
    parser.add_argument("--logical-event-limit", type=int, default=100)
    parser.add_argument("--logical-max-candidates", type=int, default=40)
    parser.add_argument("--event-baseline-event-limit", type=int, default=100)
    parser.add_argument("--event-baseline-max-candidates", type=int, default=40)
    parser.add_argument("--wallet-recent-limit", type=int, default=500)
    parser.add_argument("--wallet-trade-limit", type=int, default=100)
    parser.add_argument("--wallet-markout-lag-sec", type=float, default=300.0)
    parser.add_argument("--min-wallet-trades", type=int, default=3)
    parser.add_argument("--min-wallet-notional", type=float, default=100.0)
    parser.add_argument("--max-wallets", type=int, default=25)
    parser.add_argument("--promotion-min-trades", type=int, default=30)
    parser.add_argument("--promotion-min-lagged-roi", type=float, default=0.04)
    parser.add_argument("--promotion-max-concentration", type=float, default=0.35)
    parser.add_argument("--promotion-max-drawdown", type=float, default=0.35)
    parser.add_argument("--lookback-days", type=int, default=7)
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

    observations_file = quant_dir / "wallet_observations.json"
    markouts_file = quant_dir / "wallet_markouts.json"
    profiles_file = quant_dir / "wallet_profiles.json"
    logical_candidates_file = quant_dir / "logical_candidates.json"
    logical_file = quant_dir / "logical_constraints.json"
    logical_status_file = quant_dir / "logical_rules_status.json"
    event_baseline_candidates_file = quant_dir / "event_baseline_candidates.json"
    baselines_file = quant_dir / "event_baselines.json"
    baselines_status_file = quant_dir / "event_baselines_status.json"
    logical_rules_expires_sec = max(
        float(args.logical_interval_sec) * 2.0,
        float(args.logical_interval_sec) + 300.0,
    )
    bot_env = _bot_env(
        dotenv_path=_resolve_path(args.dotenv_path),
        overrides={
            "LOGICAL_CONSTRAINTS_FILE": str(logical_file),
            "EVENT_BASELINES_FILE": str(baselines_file),
            "WALLET_ALPHA_PROFILES_FILE": str(profiles_file),
            "WALLET_ALPHA_OBSERVATIONS_FILE": str(observations_file),
            "TELEMETRY_RECORD_DIR": str(telemetry_dir),
        },
    )

    return [
        _bot_spec("bot", env=bot_env),
        ProcessSpec(
            "logical-rules-auto",
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
            "event-baselines-auto",
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
            "wallet-scanner",
            [
                sys.executable,
                "scripts/scan_quant_strategy_inputs.py",
                "auto-wallet-observations",
                "--recent-limit",
                str(args.wallet_recent_limit),
                "--wallet-trade-limit",
                str(args.wallet_trade_limit),
                "--min-trades",
                str(args.min_wallet_trades),
                "--min-notional",
                str(args.min_wallet_notional),
                "--max-wallets",
                str(args.max_wallets),
                "--repeat-interval-sec",
                str(args.scanner_interval_sec),
                "--repeat-count",
                "0",
                "--output",
                str(observations_file),
            ],
            critical=False,
        ),
        ProcessSpec(
            "wallet-markout-scanner",
            [
                sys.executable,
                "scripts/scan_quant_strategy_inputs.py",
                "wallet-markouts-from-telemetry",
                "--telemetry-dir",
                str(telemetry_dir),
                "--lookback-days",
                str(args.lookback_days),
                "--repeat-interval-sec",
                str(args.markout_interval_sec),
                "--repeat-count",
                "0",
                "--output",
                str(markouts_file),
            ],
            critical=False,
        ),
        ProcessSpec(
            "wallet-promoter",
            [
                sys.executable,
                "scripts/scan_quant_strategy_inputs.py",
                "promote-wallet-profiles",
                "--input",
                str(markouts_file),
                "--min-trades",
                str(args.promotion_min_trades),
                "--min-lagged-roi",
                str(args.promotion_min_lagged_roi),
                "--max-concentration",
                str(args.promotion_max_concentration),
                "--max-drawdown",
                str(args.promotion_max_drawdown),
                "--repeat-interval-sec",
                str(args.promoter_interval_sec),
                "--repeat-count",
                "0",
                "--output",
                str(profiles_file),
            ],
            critical=False,
        ),
    ]


def run_supervisor(specs: list[ProcessSpec]) -> int:
    processes: list[tuple[ProcessSpec, subprocess.Popen]] = []
    stopping = False

    def _stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        for _, proc in processes:
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    for spec in specs:
        print(f"starting {spec.name}: {' '.join(spec.args)}", flush=True)
        processes.append((spec, _start_process(spec)))

    while not stopping:
        for idx, (spec, proc) in enumerate(list(processes)):
            code = proc.poll()
            if code is None:
                continue
            if spec.critical:
                print(f"{spec.name} exited with code {code}; stopping pipeline", flush=True)
                stopping = True
                break
            print(
                f"{spec.name} exited with code {code}; restarting in {spec.restart_delay_sec:.1f}s",
                flush=True,
            )
            time.sleep(max(0.0, spec.restart_delay_sec))
            if not stopping:
                processes[idx] = (spec, _start_process(spec))
        time.sleep(1.0)

    exit_code = 0
    for _, proc in processes:
        if proc.poll() is None:
            proc.terminate()
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait(timeout=30)
        if code and exit_code == 0:
            exit_code = code
    return exit_code


def _start_process(spec: ProcessSpec) -> subprocess.Popen:
    return subprocess.Popen(spec.args, cwd=PROJECT_ROOT, env=spec.env)


def _bot_spec(name: str, *, env: dict[str, str]) -> ProcessSpec:
    code = f"from polymarket_arb.main_loop import main; main({ENV_ONLY_SENTINEL!r})"
    return ProcessSpec(
        name,
        [
            sys.executable,
            "-c",
            code,
        ],
        env=env,
        critical=True,
    )


def _bot_env(*, dotenv_path: Path, overrides: Mapping[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({key: str(value) for key, value in dotenv_values(dotenv_path).items() if value is not None})
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
