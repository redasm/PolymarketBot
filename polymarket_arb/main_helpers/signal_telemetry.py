"""Telemetry compression for high-frequency strategy signals."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _SignalBucket:
    first_seen_ts: float
    last_seen_ts: float
    last_emit_ts: float
    suppressed_count: int = 0


class StrategySignalTelemetryCompressor:
    """Throttle repeated unsubmitted signal telemetry.

    Strategy generation can produce the same T2 signal every scan cycle while
    risk/rate caps prevent execution. Recording every duplicate makes the event
    log look busy without adding new evidence. Submitted signals are always
    emitted; unsubmitted repeats are summarized once per cooldown window.
    """

    def __init__(self, *, cooldown_sec: float = 60.0, bucket_ttl_sec: float = 6 * 3600.0) -> None:
        self._cooldown_sec = max(0.0, float(cooldown_sec))
        self._bucket_ttl_sec = max(self._cooldown_sec, float(bucket_ttl_sec))
        self._buckets: dict[tuple[str, str, str, str], _SignalBucket] = {}

    def consume(self, payload: dict[str, Any], *, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        self._prune(now)
        if bool(payload.get("submitted", False)):
            return [dict(payload)]

        signature = _signal_signature(payload)
        bucket = self._buckets.get(signature)
        if bucket is None:
            self._buckets[signature] = _SignalBucket(
                first_seen_ts=now,
                last_seen_ts=now,
                last_emit_ts=now,
            )
            return [_with_compression_fields(payload, duplicate_count=1, window_sec=0.0)]

        bucket.last_seen_ts = now
        if now - bucket.last_emit_ts < self._cooldown_sec:
            bucket.suppressed_count += 1
            return []

        duplicate_count = bucket.suppressed_count + 1
        window_sec = max(0.0, now - bucket.last_emit_ts)
        bucket.first_seen_ts = now
        bucket.last_emit_ts = now
        bucket.suppressed_count = 0
        return [
            _with_compression_fields(
                payload,
                duplicate_count=duplicate_count,
                window_sec=window_sec,
            )
        ]

    def _prune(self, now: float) -> None:
        if self._bucket_ttl_sec <= 0:
            return
        expired = [
            signature for signature, bucket in self._buckets.items()
            if now - bucket.last_seen_ts >= self._bucket_ttl_sec
        ]
        for signature in expired:
            self._buckets.pop(signature, None)


def _signal_signature(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    nested_payload = payload.get("payload")
    if not isinstance(nested_payload, dict):
        nested_payload = {}
    return (
        str(payload.get("tier") or ""),
        str(payload.get("signal_type") or ""),
        str(payload.get("market_id") or ""),
        _resolve_action(nested_payload),
    )


def _resolve_action(payload: dict[str, Any]) -> str:
    direct = str(payload.get("action") or "").upper()
    if direct:
        return direct
    execution_check = payload.get("execution_check")
    if isinstance(execution_check, dict):
        action = str(execution_check.get("action") or "").upper()
        if action:
            return action
    research_overlay = payload.get("research_overlay")
    if isinstance(research_overlay, dict):
        action = str(research_overlay.get("action") or "").upper()
        if action:
            return action
    return ""


def _with_compression_fields(
    payload: dict[str, Any],
    *,
    duplicate_count: int,
    window_sec: float,
) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["compressed"] = duplicate_count > 1
    enriched["duplicate_count"] = int(duplicate_count)
    enriched["duplicate_window_sec"] = round(float(window_sec), 3)
    return enriched
