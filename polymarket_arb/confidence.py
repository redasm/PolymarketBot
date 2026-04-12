"""Shared confidence helpers used across strategies."""

from __future__ import annotations

import math


def clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def confidence_from_edge_pct(edge_pct: float, *, full_confidence_pct: float = 10.0) -> float:
    if full_confidence_pct <= 0:
        return 0.0
    if edge_pct <= 0:
        return 0.0
    return clamp_confidence(math.tanh(edge_pct / full_confidence_pct))


def confidence_from_signal_strength(signal_strength: float, *, scale: float = 2.0) -> float:
    return clamp_confidence(signal_strength * scale)
