"""Confidence and freshness heuristics for research signals."""

from __future__ import annotations

import math
import time

from research_signal.scorers.source_profile import resolve_source_profile


def compute_freshness_sec(ts: float) -> float:
    return max(0.0, time.time() - ts)


def compute_confidence(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    now = time.time()
    unique_profiles: dict[str, float] = {}
    freshness_scores: list[float] = []

    for row in rows:
        profile = resolve_source_profile(str(row.get("source", "unknown")), str(row.get("link", "")))
        unique_profiles[str(profile.get("key", row.get("source", "unknown")))] = float(profile.get("weight", 0.55))
        ts = float(row.get("published_ts") or row.get("ts") or now)
        freshness_scores.append(_freshness_score(max(0.0, now - ts)))

    source_score = sum(unique_profiles.values()) / max(1, len(unique_profiles))
    freshness_score = sum(sorted(freshness_scores, reverse=True)[:3]) / max(1, min(3, len(freshness_scores)))
    diversity_bonus = min(0.16, 0.06 * max(0, len(unique_profiles) - 1))
    sample_bonus = min(0.10, 0.03 * max(0, len(rows) - 1))

    score = 0.18 + (0.42 * source_score) + (0.14 * freshness_score) + diversity_bonus + sample_bonus
    return max(0.0, min(1.0, round(score, 4)))


def _freshness_score(age_sec: float, *, half_life_sec: float = 6 * 3600) -> float:
    if age_sec <= 0:
        return 1.0
    if half_life_sec <= 0:
        return 0.0
    return math.exp(-math.log(2) * (age_sec / half_life_sec))
