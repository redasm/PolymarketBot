"""Conservative timing adjustments from sidecar quant inputs.

The LLM workers produce structured inputs, not trade commands.  This module
lets those inputs influence timing only as a bounded calibration layer around
the existing statistical model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from polymarket_arb.models import MarketInfo


_MIN_BASELINE_CONFIDENCE = 0.75
_MIN_DIRECTIONAL_EDGE = 0.015
_STRONG_CONFLICT_CONFIDENCE = 0.85


@dataclass(frozen=True)
class EventBaselineTiming:
    model_prob: float
    confidence_delta: float
    size_multiplier: float
    veto: bool
    payload: dict[str, Any]


def parse_event_baselines(raw: dict[str, dict[str, Any]] | str | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def event_baseline_for_market(
    market: MarketInfo,
    baselines: dict[str, dict[str, Any]],
    *,
    now_ts: float | None = None,
) -> dict[str, float] | None:
    raw = baselines.get(market.condition_id) or baselines.get(market.slug)
    if raw is None:
        return None
    baseline = _first_float(raw, ("event_baseline_probability", "baseline_probability"))
    confidence = _first_float(raw, ("event_confidence", "confidence"))
    time_to_event = _time_to_event_sec(raw, now_ts=time.time() if now_ts is None else now_ts)
    if baseline is None or confidence is None or time_to_event is None:
        return None
    return {
        "baseline_probability": max(0.0, min(1.0, baseline)),
        "confidence": max(0.0, min(1.0, confidence)),
        "time_to_event_sec": max(0.0, time_to_event),
    }


def apply_event_baseline_timing(
    *,
    model_prob: float,
    market_prob: float,
    baseline_probability: float,
    confidence: float,
    time_to_event_sec: float | None = None,
) -> EventBaselineTiming:
    model_prob = max(0.0, min(1.0, float(model_prob)))
    market_prob = max(0.0, min(1.0, float(market_prob)))
    baseline_probability = max(0.0, min(1.0, float(baseline_probability)))
    confidence = max(0.0, min(1.0, float(confidence)))

    base_payload: dict[str, Any] = {
        "source": "event_baselines",
        "baseline_probability": round(baseline_probability, 4),
        "confidence": round(confidence, 4),
        "market_prob": round(market_prob, 4),
        "model_prob_before": round(model_prob, 4),
    }
    if time_to_event_sec is not None:
        base_payload["time_to_event_sec"] = round(max(0.0, float(time_to_event_sec)), 1)

    if confidence < _MIN_BASELINE_CONFIDENCE:
        payload = {**base_payload, "applied": False, "reason": "event_baseline_low_confidence"}
        return EventBaselineTiming(model_prob, 0.0, 1.0, False, payload)

    model_dev = model_prob - market_prob
    baseline_dev = baseline_probability - market_prob
    if abs(baseline_dev) < _MIN_DIRECTIONAL_EDGE:
        payload = {**base_payload, "applied": False, "reason": "event_baseline_neutral"}
        return EventBaselineTiming(model_prob, 0.0, 1.0, False, payload)

    same_direction = model_dev == 0 or (model_dev > 0) == (baseline_dev > 0)
    if not same_direction:
        # Conflict should make timing more conservative, not flip the model.
        adjusted = market_prob + (model_dev * 0.35)
        strong_conflict = confidence >= _STRONG_CONFLICT_CONFIDENCE and abs(baseline_dev) >= 0.03
        payload = {
            **base_payload,
            "applied": True,
            "reason": "event_baseline_conflict",
            "model_prob_after": round(adjusted, 4),
        }
        return EventBaselineTiming(
            max(0.0, min(1.0, adjusted)),
            -0.10,
            0.35,
            strong_conflict,
            payload,
        )

    # Alignment can improve timing, but the LLM baseline is capped so it cannot
    # dominate the statistical detector.
    max_step = 0.03
    weight = min(0.25, max(0.0, confidence - _MIN_BASELINE_CONFIDENCE))
    raw_step = (baseline_probability - model_prob) * weight
    step = max(-max_step, min(max_step, raw_step))
    adjusted = max(0.0, min(1.0, model_prob + step))
    payload = {
        **base_payload,
        "applied": True,
        "reason": "event_baseline_aligned",
        "model_prob_after": round(adjusted, 4),
    }
    return EventBaselineTiming(adjusted, 0.04, 1.10, False, payload)


def _first_float(raw: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in raw or raw[key] in (None, ""):
            continue
        try:
            return float(raw[key])
        except (TypeError, ValueError):
            continue
    return None


def _time_to_event_sec(raw: dict[str, Any], *, now_ts: float) -> float | None:
    direct = _first_float(raw, ("time_to_event_sec", "horizon_sec"))
    if direct is not None:
        return direct
    for key in ("resolution_at", "resolution_time", "event_time", "deadline"):
        value = raw.get(key)
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() - now_ts
    return None
