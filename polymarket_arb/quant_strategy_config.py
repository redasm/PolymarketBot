"""Templates for opt-in quant strategy configuration."""

from __future__ import annotations

import json
from typing import Any


def build_quant_strategy_env_template() -> dict[str, str]:
    """Return parseable example env values for new quant strategies."""
    logical_constraints = [
        {
            "subject_market_id": "candidate-condition-id",
            "bound_market_id": "party-condition-id",
            "relation_type": "subject_lte_bound",
            "min_violation_bps": 250,
            "tags": ["example", "replace-before-live"],
        }
    ]
    event_baselines = {
        "event-condition-id": {
            "baseline_probability": 0.55,
            "confidence": 0.80,
            "time_to_event_sec": 3600,
        }
    }
    wallet_profiles = {
        "0xvalidated-wallet": {
            "trade_count": 40,
            "realized_roi": 0.12,
            "lagged_follow_roi": 0.06,
            "max_drawdown": 0.18,
            "concentration_score": 0.30,
            "category_edges": {"macro": 0.05},
        }
    }
    wallet_observations = [
        {
            "wallet_address": "0xvalidated-wallet",
            "market_id": "event-condition-id",
            "category": "macro",
            "action": "BUY_YES",
            "observed_size_usdc": 50,
        }
    ]
    return {
        "SNIPER_GATE_ENABLED": "true",
        "SNIPER_MIN_NET_EDGE_BPS": "250",
        "SNIPER_MIN_CONFIDENCE": "0.75",
        "SNIPER_MIN_LIQUIDITY": "1000",
        "SNIPER_MIN_VOLUME_24H": "500",
        "SNIPER_MAX_CORRELATION_SCORE": "0.80",
        "LOGICAL_CONSTRAINTS_JSON": json.dumps(logical_constraints, separators=(",", ":")),
        "EVENT_BASELINES_JSON": json.dumps(event_baselines, separators=(",", ":")),
        "WALLET_ALPHA_PROFILES_JSON": json.dumps(wallet_profiles, separators=(",", ":")),
        "WALLET_ALPHA_OBSERVATIONS_JSON": json.dumps(wallet_observations, separators=(",", ":")),
        "LOGICAL_CONSTRAINTS_FILE": "data/quant_inputs/logical_constraints.json",
        "EVENT_BASELINES_FILE": "data/quant_inputs/event_baselines.json",
        "WALLET_ALPHA_PROFILES_FILE": "data/quant_inputs/wallet_profiles.json",
        "WALLET_ALPHA_OBSERVATIONS_FILE": "data/quant_inputs/wallet_observations.json",
    }


def format_env_template(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def build_event_baselines_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        condition_id = str(row.get("condition_id") or row.get("market_id") or "").strip()
        baseline = _to_float(row.get("baseline_probability"))
        confidence = _to_float(row.get("confidence"))
        time_to_event = _to_float(row.get("time_to_event_sec") or row.get("seconds_to_event"))
        if not condition_id or baseline is None or confidence is None or time_to_event is None:
            continue
        out[condition_id] = {
            "baseline_probability": baseline,
            "confidence": confidence,
            "time_to_event_sec": time_to_event,
        }
    return out


def build_logical_constraints_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        subject = str(row.get("subject_market_id") or "").strip()
        bound = str(row.get("bound_market_id") or "").strip()
        if not subject or not bound:
            continue
        rule = {
            "subject_market_id": subject,
            "bound_market_id": bound,
            "relation_type": str(row.get("relation_type") or "subject_lte_bound").strip() or "subject_lte_bound",
            "min_violation_bps": _to_float(row.get("min_violation_bps")) or 200.0,
        }
        tags = _parse_tags(row.get("tags"))
        if tags:
            rule["tags"] = tags
        max_size = _to_float(row.get("max_size_usdc"))
        if max_size is not None:
            rule["max_size_usdc"] = max_size
        out.append(rule)
    return out


def build_wallet_observations_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        wallet = str(row.get("wallet_address") or "").strip()
        market_id = str(row.get("market_id") or row.get("condition_id") or "").strip()
        action = str(row.get("action") or "BUY_YES").strip().upper()
        if not wallet or not market_id or action not in {"BUY_YES", "BUY_NO"}:
            continue
        out.append(
            {
                "wallet_address": wallet,
                "market_id": market_id,
                "category": str(row.get("category") or "").strip(),
                "action": action,
                "observed_size_usdc": _to_float(row.get("observed_size_usdc")) or 0.0,
            }
        )
    return out


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]
