"""Signal-source attribution helpers for telemetry and PnL analysis."""

from __future__ import annotations

from typing import Any


def infer_signal_attribution(signal_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})
    explicit_source = str(payload.get("signal_source") or "").strip()
    explicit_components = _component_list(payload.get("signal_components"))
    if explicit_source:
        return {
            "signal_source": explicit_source,
            "signal_components": explicit_components or [explicit_source],
        }

    lowered = str(signal_type or "").lower()
    if lowered.startswith("wallet_alpha_"):
        return {"signal_source": "wallet_alpha", "signal_components": ["wallet_alpha"]}
    if lowered.startswith("event_calendar_"):
        return {"signal_source": "event_calendar", "signal_components": ["event_calendar"]}
    if lowered.startswith("logical_constraint_"):
        return {"signal_source": "logical_constraint", "signal_components": ["logical_constraint"]}
    if lowered.startswith("maker_"):
        return {"signal_source": "maker", "signal_components": _maker_components(payload)}
    if lowered.startswith("cross_platform_"):
        return {"signal_source": "cross_platform", "signal_components": ["cross_platform"]}
    if lowered.startswith("statistical_") or lowered.startswith("stat_"):
        return {"signal_source": "statistical_model", "signal_components": _stat_components(payload)}
    return {"signal_source": "unknown", "signal_components": ["unknown"]}


def _component_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _stat_components(payload: dict[str, Any]) -> list[str]:
    signals = payload.get("signals")
    if not isinstance(signals, dict):
        signals = {}
    components = [
        name for name in ("obi", "momentum", "cross_market")
        if abs(_to_float(signals.get(name))) > 1e-12
    ]
    if payload.get("research_overlay"):
        components.append("research_overlay")
    if payload.get("tail_risk"):
        components.append("tail_risk")
    return components or ["statistical_model"]


def _maker_components(payload: dict[str, Any]) -> list[str]:
    components = ["maker"]
    if payload.get("flow_bias") or payload.get("flow_snapshot"):
        components.append("maker_flow_bias")
    return components


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
