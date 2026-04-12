"""Build compact ResearchSignal objects from grouped rows."""

from __future__ import annotations

from polymarket_arb.models import ResearchSignal
from research_signal.scorers.scoring import compute_confidence, compute_freshness_sec
from research_signal.scorers.source_profile import resolve_source_profile

_BULLISH_TERMS = ("approve", "approval", "beat", "gain", "higher", "rally", "rise", "surge", "up", "win")
_BEARISH_TERMS = ("delay", "drop", "fall", "lower", "lose", "miss", "reject", "slump", "under", "down")


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _infer_stance(rows: list[dict]) -> str:
    text = " ".join((row.get("summary", "") or "").lower() for row in rows)
    bullish_score = sum(term in text for term in _BULLISH_TERMS)
    bearish_score = sum(term in text for term in _BEARISH_TERMS)
    if bullish_score > bearish_score:
        return "bullish"
    if bearish_score > bullish_score:
        return "bearish"
    return "uncertain"


def build_signal(topic_id: str, rows: list[dict]) -> ResearchSignal:
    rows_sorted = sorted(
        rows,
        key=lambda row: float(row.get("published_ts") or row.get("ts") or 0.0),
        reverse=True,
    )
    first = rows_sorted[0]
    summary_parts = _dedupe_preserve_order(
        [row.get("summary", "") for row in rows_sorted if row.get("summary")]
    )[:3]
    sources = sorted({row.get("source", "unknown") for row in rows})
    event_candidates = sorted({row.get("event_id", "") for row in rows if row.get("event_id")})
    top_links = _dedupe_preserve_order([row.get("link", "") for row in rows_sorted if row.get("link")])[:3]
    freshest_ts = max(float(row.get("published_ts") or row.get("ts") or 0.0) for row in rows)
    source_counts = {
        source: sum(1 for row in rows if row.get("source") == source)
        for source in sources
    }
    source_profiles = {
        source: resolve_source_profile(source, next((row.get("link", "") for row in rows if row.get("source") == source), ""))
        for source in sources
    }
    top_domains = _dedupe_preserve_order(
        [profile.get("key", "") for profile in source_profiles.values() if profile.get("type") != "market_seed"]
    )[:5]

    return ResearchSignal(
        topic_id=topic_id,
        event_candidates=event_candidates,
        summary=" | ".join(summary_parts)[:320],
        sources=sources,
        confidence=compute_confidence(rows),
        freshness_sec=compute_freshness_sec(freshest_ts),
        stance=_infer_stance(rows_sorted),
        metadata={
            "sample_size": len(rows),
            "canonical_topic": first.get("topic", ""),
            "top_links": top_links,
            "source_counts": source_counts,
            "source_profiles": source_profiles,
            "source_types": sorted({str(profile.get("type", "unknown")) for profile in source_profiles.values()}),
            "top_domains": top_domains,
            "evidence": summary_parts,
        },
    )
