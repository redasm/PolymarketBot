from __future__ import annotations

import argparse
from pathlib import Path

from run_bot_with_copytrading import build_process_specs
from scripts.run_automated_quant_pipeline import ENV_ONLY_SENTINEL

# LLM worker 子命令——本 launcher 绝不应包含它们(跟单已从 LLM supervisor 解耦)
_LLM_SUBCOMMANDS = {"logical-rules-auto", "event-baselines-auto", "research-feeds-auto"}


def _args(tmp_path: Path) -> argparse.Namespace:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(["PRIVATE_KEY=test-key", "POLYMARKET_FUNDER=0xfunder"]),
        encoding="utf-8",
    )
    return argparse.Namespace(
        dotenv_path=str(env_path),
        quant_input_dir=str(tmp_path / "quant_inputs"),
        telemetry_dir=str(tmp_path / "telemetry"),
        scanner_interval_sec=120.0,
        markout_interval_sec=300.0,
        promoter_interval_sec=300.0,
        wallet_recent_limit=500,
        wallet_trade_limit=100,
        min_wallet_trades=3,
        min_wallet_notional=100.0,
        max_wallets=25,
        lookback_days=7,
        promotion_min_trades=30,
        promotion_min_lagged_roi=0.04,
        promotion_max_concentration=0.35,
        promotion_max_drawdown=0.35,
    )


def test_copytrading_builds_bot_and_three_wallet_workers(tmp_path: Path) -> None:
    specs = build_process_specs(_args(tmp_path))

    assert [spec.name for spec in specs] == [
        "bot",
        "wallet-scanner",
        "wallet-markout-scanner",
        "wallet-promoter",
    ]
    # bot
    assert ENV_ONLY_SENTINEL in specs[0].args[-1]
    assert specs[0].critical is True
    assert specs[0].env is not None
    assert specs[0].env["PRIVATE_KEY"] == "test-key"
    assert specs[0].env["WALLET_ALPHA_PROFILES_FILE"].endswith("wallet_profiles.json")
    assert specs[0].env["WALLET_ALPHA_OBSERVATIONS_FILE"].endswith("wallet_observations.json")
    # wallet workers
    assert "auto-wallet-observations" in specs[1].args
    assert specs[1].args[specs[1].args.index("--output") + 1].endswith("wallet_observations.json")
    assert "wallet-markouts-from-telemetry" in specs[2].args
    assert specs[2].args[specs[2].args.index("--output") + 1].endswith("wallet_markouts.json")
    assert "promote-wallet-profiles" in specs[3].args
    assert specs[3].args[specs[3].args.index("--output") + 1].endswith("wallet_profiles.json")
    # wallet workers 非 critical(挂了不拖垮 bot)
    assert all(spec.critical is False for spec in specs[1:])


def test_copytrading_has_no_llm_workers(tmp_path: Path) -> None:
    """关键不变量: 本 launcher 绝不拉起任何 LLM worker, 也不设 LLM 输入文件 env。"""
    specs = build_process_specs(_args(tmp_path))
    for spec in specs:
        assert not (_LLM_SUBCOMMANDS & set(spec.args)), f"{spec.name} 含 LLM 子命令"
    bot_env = specs[0].env or {}
    assert "LOGICAL_CONSTRAINTS_FILE" not in bot_env
    assert "EVENT_BASELINES_FILE" not in bot_env
    assert "RESEARCH_SIGNAL_FEEDS_FILE" not in bot_env
