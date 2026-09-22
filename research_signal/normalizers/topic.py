"""Topic normalization and simple entity grouping."""

from __future__ import annotations

import re
from collections import defaultdict

_STOPWORDS = {
    "a", "an", "and", "are", "be", "for", "from", "how", "in", "into", "is",
    "of", "on", "or", "the", "this", "to", "what", "when", "where", "who",
    "will", "with", "would", "yes", "no",
}


def normalize_topic(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_topic_keywords(text: str, *, limit: int = 6) -> list[str]:
    tokens = []
    for token in normalize_topic(text).split():
        if token in _STOPWORDS:
            continue
        if len(token) <= 2 and not token.isdigit():
            continue
        tokens.append(token)
    return tokens[:limit]


def topic_fingerprint(row: dict) -> str:
    event_id = str(row.get("event_id") or "").strip()
    if event_id:
        return f"event:{event_id}"

    seed = row.get("topic") or row.get("summary") or ""
    keywords = extract_topic_keywords(seed, limit=6)
    if keywords:
        return "kw:" + " ".join(keywords)
    return normalize_topic(seed)[:120]


def topic_overlap_score(text_a: str, text_b: str) -> float:
    a = set(extract_topic_keywords(text_a, limit=12))
    b = set(extract_topic_keywords(text_b, limit=12))
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def group_by_topic(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        topic_id = topic_fingerprint(row)[:120]
        if topic_id:
            grouped[topic_id].append(row)
    return dict(grouped)
