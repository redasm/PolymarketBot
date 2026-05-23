"""Source weighting helpers for research signals.

Inspired by multi-source news pipelines: distinguish market seed rows,
aggregator feeds, and primary media so scoring can reward corroborated
and timely evidence without over-trusting any one feed.
"""

from __future__ import annotations

from urllib.parse import urlparse

_DEFAULT_PROFILE = {
    "label": "Unknown",
    "type": "unknown",
    "tier": 4,
    "weight": 0.55,
}

_SOURCE_PROFILES: dict[str, dict[str, object]] = {
    "polymarket_market": {"label": "Polymarket Market", "type": "market_seed", "tier": 4, "weight": 0.18},
    "google_news_rss": {"label": "Google News RSS", "type": "aggregator", "tier": 3, "weight": 0.62},
    "coindesk": {"label": "CoinDesk", "type": "crypto_media", "tier": 2, "weight": 0.76},
    "cointelegraph": {"label": "Cointelegraph", "type": "crypto_media", "tier": 2, "weight": 0.72},
    "the_block": {"label": "The Block", "type": "crypto_media", "tier": 2, "weight": 0.79},
    "decrypt": {"label": "Decrypt", "type": "crypto_media", "tier": 2, "weight": 0.71},
    "reuters": {"label": "Reuters", "type": "wire", "tier": 1, "weight": 0.88},
    "associated_press": {"label": "Associated Press", "type": "wire", "tier": 1, "weight": 0.87},
    "ap": {"label": "Associated Press", "type": "wire", "tier": 1, "weight": 0.87},
    "bloomberg": {"label": "Bloomberg", "type": "financial_media", "tier": 1, "weight": 0.86},
    "cnbc": {"label": "CNBC", "type": "financial_media", "tier": 2, "weight": 0.77},
    "espn": {"label": "ESPN", "type": "sports_media", "tier": 2, "weight": 0.76},
    "weather": {"label": "Weather Feed", "type": "weather_data", "tier": 1, "weight": 0.84},
    "fear_greed": {"label": "Fear & Greed", "type": "market_sentiment", "tier": 3, "weight": 0.58},
    "coingecko_trending": {"label": "CoinGecko Trending", "type": "market_sentiment", "tier": 3, "weight": 0.57},
}


def resolve_source_profile(source: str, link: str = "") -> dict[str, object]:
    normalized = (source or "").strip().lower()
    if normalized in _SOURCE_PROFILES:
        return dict(_SOURCE_PROFILES[normalized], key=normalized)

    for key, profile in _SOURCE_PROFILES.items():
        if key in normalized:
            return dict(profile, key=normalized)

    domain = extract_domain(link)
    if domain:
        for key, profile in _SOURCE_PROFILES.items():
            if key.replace("_", "") in domain.replace(".", "") or key in domain:
                return dict(profile, key=normalized or domain)

    if normalized.endswith("_rss") or "rss" in normalized:
        return {
            "label": source or "RSS Feed",
            "type": "rss",
            "tier": 3,
            "weight": 0.64,
            "key": normalized or "rss",
        }

    return dict(_DEFAULT_PROFILE, key=normalized or "unknown")


def extract_domain(link: str) -> str:
    if not link:
        return ""
    try:
        parsed = urlparse(link)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host
