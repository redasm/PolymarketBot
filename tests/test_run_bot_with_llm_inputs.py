from __future__ import annotations

import argparse
from pathlib import Path

from run_bot_with_llm_inputs import build_process_specs
from scripts.run_automated_quant_pipeline import ENV_ONLY_SENTINEL


def test_bot_with_llm_inputs_builds_bot_and_three_llm_workers(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "PRIVATE_KEY=test-key",
                "POLYMARKET_FUNDER=0xfunder",
                "AI_API_KEY=test-ai-key",
            ]
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        dotenv_path=str(env_path),
        quant_input_dir=str(tmp_path / "quant_inputs"),
        telemetry_dir=str(tmp_path / "telemetry"),
        logical_interval_sec=1800.0,
        event_baseline_interval_sec=900.0,
        research_feeds_interval_sec=3600.0,
        logical_event_limit=100,
        logical_max_candidates=40,
        event_baseline_event_limit=80,
        event_baseline_max_candidates=25,
        research_feeds_max=12,
        research_feeds_http_timeout_sec=8.0,
        research_feeds_seed_file=None,
    )

    specs = build_process_specs(args)

    assert [spec.name for spec in specs] == [
        "bot",
        "logical-rules-llm",
        "event-baselines-llm",
        "research-feeds-llm",
    ]
    assert ENV_ONLY_SENTINEL in specs[0].args[-1]
    assert specs[0].critical is True
    assert specs[0].env is not None
    assert specs[0].env["PRIVATE_KEY"] == "test-key"
    assert specs[0].env["LOGICAL_CONSTRAINTS_FILE"].endswith("logical_constraints.json")
    assert specs[0].env["EVENT_BASELINES_FILE"].endswith("event_baselines.json")
    assert specs[0].env["RESEARCH_SIGNAL_FEEDS_FILE"].endswith("research_feeds.json")

    assert "logical-rules-auto" in specs[1].args
    assert "--fetch-gamma" in specs[1].args
    assert specs[1].args[specs[1].args.index("--candidates-output") + 1].endswith("logical_candidates.json")
    assert specs[1].args[specs[1].args.index("--output") + 1].endswith("logical_constraints.json")
    assert specs[1].args[specs[1].args.index("--status-output") + 1].endswith("logical_rules_status.json")

    assert "event-baselines-auto" in specs[2].args
    assert "--fetch-gamma" in specs[2].args
    assert specs[2].args[specs[2].args.index("--candidates-output") + 1].endswith(
        "event_baseline_candidates.json"
    )
    assert specs[2].args[specs[2].args.index("--output") + 1].endswith("event_baselines.json")
    assert specs[2].args[specs[2].args.index("--status-output") + 1].endswith("event_baselines_status.json")

    assert "research-feeds-auto" in specs[3].args
    assert specs[3].args[specs[3].args.index("--max-feeds") + 1] == "12"
    assert specs[3].args[specs[3].args.index("--http-timeout-sec") + 1] == "8.0"
    assert specs[3].args[specs[3].args.index("--output") + 1].endswith("research_feeds.json")
    assert specs[3].args[specs[3].args.index("--status-output") + 1].endswith("research_feeds_status.json")
    assert "--seed-feeds-file" not in specs[3].args

    assert specs[1].critical is False
    assert specs[2].critical is False
    assert specs[3].critical is False


def test_research_feeds_seed_file_passed_through(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "PRIVATE_KEY=test-key",
                "POLYMARKET_FUNDER=0xfunder",
                "AI_API_KEY=test-ai-key",
            ]
        ),
        encoding="utf-8",
    )
    seed_path = tmp_path / "seed.json"
    seed_path.write_text("{\"feeds\": []}", encoding="utf-8")
    args = argparse.Namespace(
        dotenv_path=str(env_path),
        quant_input_dir=str(tmp_path / "quant_inputs"),
        telemetry_dir=str(tmp_path / "telemetry"),
        logical_interval_sec=1800.0,
        event_baseline_interval_sec=900.0,
        research_feeds_interval_sec=3600.0,
        logical_event_limit=100,
        logical_max_candidates=40,
        event_baseline_event_limit=80,
        event_baseline_max_candidates=25,
        research_feeds_max=12,
        research_feeds_http_timeout_sec=8.0,
        research_feeds_seed_file=str(seed_path),
    )

    specs = build_process_specs(args)
    research_args = specs[3].args
    assert "--seed-feeds-file" in research_args
    assert research_args[research_args.index("--seed-feeds-file") + 1].endswith("seed.json")
