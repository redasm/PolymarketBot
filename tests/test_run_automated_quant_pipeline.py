from __future__ import annotations

import argparse
from pathlib import Path

from scripts.run_automated_quant_pipeline import ENV_ONLY_SENTINEL, build_process_specs


def test_automated_pipeline_builds_shadow_live_scanner_and_promoter(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "PRIVATE_KEY=test-key",
                "POLYMARKET_FUNDER=0xfunder",
                "LIVE_TRADING_ACK=true",
            ]
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        dotenv_path=str(env_path),
        quant_input_dir=str(tmp_path / "quant_inputs"),
        telemetry_dir=str(tmp_path / "telemetry"),
        scanner_interval_sec=120.0,
        logical_interval_sec=1800.0,
        event_baseline_interval_sec=1800.0,
        promoter_interval_sec=300.0,
        markout_interval_sec=300.0,
        logical_event_limit=100,
        logical_max_candidates=40,
        event_baseline_event_limit=100,
        event_baseline_max_candidates=40,
        wallet_recent_limit=500,
        wallet_trade_limit=100,
        wallet_markout_lag_sec=300.0,
        min_wallet_trades=3,
        min_wallet_notional=100.0,
        max_wallets=25,
        promotion_min_trades=30,
        promotion_min_lagged_roi=0.04,
        promotion_max_concentration=0.35,
        promotion_max_drawdown=0.35,
        lookback_days=7,
    )

    specs = build_process_specs(args)

    assert [spec.name for spec in specs] == [
        "bot",
        "logical-rules-auto",
        "event-baselines-auto",
        "wallet-scanner",
        "wallet-markout-scanner",
        "wallet-promoter",
    ]
    assert ENV_ONLY_SENTINEL in specs[0].args[-1]
    assert specs[0].critical is True
    assert specs[0].env is not None
    assert specs[0].env["PRIVATE_KEY"] == "test-key"
    assert specs[0].env["WALLET_ALPHA_OBSERVATIONS_FILE"].endswith("wallet_observations.json")
    assert specs[0].env["WALLET_ALPHA_PROFILES_FILE"].endswith("wallet_profiles.json")
    assert specs[0].env["TELEMETRY_RECORD_ENABLED"] == "true"
    assert "logical-rules-auto" in specs[1].args
    assert "--fetch-gamma" in specs[1].args
    assert specs[1].args[specs[1].args.index("--candidates-output") + 1].endswith("logical_candidates.json")
    assert specs[1].args[specs[1].args.index("--output") + 1].endswith("logical_constraints.json")
    rules_expires = float(specs[1].args[specs[1].args.index("--rules-expires-sec") + 1])
    assert rules_expires > args.logical_interval_sec
    assert specs[1].args[specs[1].args.index("--status-output") + 1].endswith("logical_rules_status.json")
    assert "event-baselines-auto" in specs[2].args
    assert "--fetch-gamma" in specs[2].args
    assert specs[2].args[specs[2].args.index("--candidates-output") + 1].endswith("event_baseline_candidates.json")
    assert specs[2].args[specs[2].args.index("--output") + 1].endswith("event_baselines.json")
    assert specs[2].args[specs[2].args.index("--status-output") + 1].endswith("event_baselines_status.json")
    assert "auto-wallet-observations" in specs[3].args
    assert "wallet-markouts-from-telemetry" in specs[4].args
    assert "promote-wallet-profiles" in specs[5].args
    assert specs[1].critical is False
    assert specs[2].critical is False
    assert specs[3].critical is False
    assert specs[4].critical is False
    assert specs[5].critical is False
    assert not (tmp_path / "runtime" / ".env.shadow").exists()
    assert not (tmp_path / "runtime" / ".env.live").exists()
