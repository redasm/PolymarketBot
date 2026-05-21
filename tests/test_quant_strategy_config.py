from __future__ import annotations

import json

from polymarket_arb.quant_strategy_config import build_quant_strategy_env_template
from polymarket_arb.quant_strategy_config import (
    build_event_baselines_from_rows,
    build_logical_constraints_from_rows,
    build_wallet_observations_from_rows,
)


def test_build_quant_strategy_env_template_contains_parseable_json_values() -> None:
    template = build_quant_strategy_env_template()

    logical = json.loads(template["LOGICAL_CONSTRAINTS_JSON"])
    event = json.loads(template["EVENT_BASELINES_JSON"])
    wallet_profiles = json.loads(template["WALLET_ALPHA_PROFILES_JSON"])
    wallet_observations = json.loads(template["WALLET_ALPHA_OBSERVATIONS_JSON"])

    assert template["SNIPER_GATE_ENABLED"] == "true"
    assert template["SNIPER_MIN_NET_EDGE_BPS"] == "500"
    assert logical[0]["relation_type"] == "subject_lte_bound"
    assert "baseline_probability" in next(iter(event.values()))
    assert next(iter(wallet_profiles.values()))["lagged_follow_roi"] > 0
    assert wallet_observations[0]["action"] == "BUY_YES"
    assert template["WALLET_ALPHA_OBSERVATIONS_FILE"].endswith("wallet_observations.json")


def test_build_event_baselines_from_rows_skips_incomplete_rows() -> None:
    out = build_event_baselines_from_rows(
        [
            {
                "condition_id": "c1",
                "baseline_probability": "0.58",
                "confidence": "0.82",
                "time_to_event_sec": "7200",
            },
            {"condition_id": "bad", "baseline_probability": ""},
        ]
    )

    assert out["c1"]["baseline_probability"] == 0.58
    assert out["c1"]["confidence"] == 0.82
    assert out["c1"]["time_to_event_sec"] == 7200.0
    assert out["c1"]["generated_at"] > 0


def test_build_logical_constraints_from_rows_emits_subject_bound_rules() -> None:
    out = build_logical_constraints_from_rows(
        [
            {
                "subject_market_id": "candidate",
                "bound_market_id": "party",
                "relation_type": "",
                "min_violation_bps": "300",
                "tags": "election,manual",
            }
        ]
    )

    assert out == [
        {
            "subject_market_id": "candidate",
            "bound_market_id": "party",
            "relation_type": "subject_lte_bound",
            "min_violation_bps": 300.0,
            "tags": ["election", "manual"],
        }
    ]


def test_build_wallet_observations_from_rows_normalizes_action_and_size() -> None:
    out = build_wallet_observations_from_rows(
        [
            {
                "wallet_address": "0xabc",
                "market_id": "cond",
                "category": "weather",
                "action": "buy_yes",
                "observed_size_usdc": "25.5",
            }
        ]
    )

    assert out == [
        {
            "wallet_address": "0xabc",
            "market_id": "cond",
            "category": "weather",
            "action": "BUY_YES",
            "observed_size_usdc": 25.5,
        }
    ]
