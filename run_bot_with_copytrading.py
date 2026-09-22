#!/usr/bin/env python3
"""入口：机器人主循环 + 跟单(wallet_alpha)数据 worker，不含任何 LLM worker.

为什么单独一个入口：
跟单策略(wallet_alpha)的三个数据 worker —— wallet-scanner / wallet-markout /
wallet-promoter —— 全是纯链上数据处理(走 Polymarket Data API 抓成交、算
markout、按阈值晋级钱包画像)，**不调用任何 LLM**。但它们原本混在
`run_automated_quant_pipeline.py` 这个 supervisor 里，和三个烧钱的 LLM worker
(logical-rules / event-baselines / research-feeds)一起被拉起。

LLM worker 已确认对交易零产出而全停。本入口把跟单数据链路从 LLM supervisor 中
解耦出来，让跟单能独立复活：只拉起 bot + 三个 wallet worker，绝不启动 LLM。

复用 `run_automated_quant_pipeline` 的 supervisor / ProcessSpec / 环境装配，
不重复造轮子。跟单仍受 `WALLET_ALPHA_SHADOW_VALIDATION_ENABLED=true` 约束
(只影子验证、不真实下单)，先验证再谈实盘。

用法：
    python run_bot_with_copytrading.py --dotenv-path .env
    python run_bot_with_copytrading.py --dry-run   # 只打印进程清单，不启动
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
        description="Run one bot plus wallet copy-trading data workers (NO LLM workers)",
    )
    parser.add_argument("--dotenv-path", default=".env")
    parser.add_argument("--quant-input-dir", default="data/quant_inputs")
    parser.add_argument("--telemetry-dir", default="data/telemetry")
    parser.add_argument("--scanner-interval-sec", type=float, default=120.0)
    parser.add_argument("--markout-interval-sec", type=float, default=300.0)
    parser.add_argument("--promoter-interval-sec", type=float, default=300.0)
    parser.add_argument("--wallet-recent-limit", type=int, default=500)
    parser.add_argument("--wallet-trade-limit", type=int, default=100)
    parser.add_argument("--min-wallet-trades", type=int, default=3)
    parser.add_argument("--min-wallet-notional", type=float, default=100.0)
    parser.add_argument("--max-wallets", type=int, default=25)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--promotion-min-trades", type=int, default=30)
    parser.add_argument("--promotion-min-lagged-roi", type=float, default=0.04)
    parser.add_argument("--promotion-max-concentration", type=float, default=0.35)
    parser.add_argument("--promotion-max-drawdown", type=float, default=0.35)
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

    bot_env = _bot_env(
        dotenv_path=_resolve_path(args.dotenv_path),
        overrides={
            "WALLET_ALPHA_PROFILES_FILE": str(profiles_file),
            "WALLET_ALPHA_OBSERVATIONS_FILE": str(observations_file),
            "TELEMETRY_RECORD_DIR": str(telemetry_dir),
        },
    )

    return [
        _bot_spec("bot", env=bot_env),
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


if __name__ == "__main__":
    raise SystemExit(main())
