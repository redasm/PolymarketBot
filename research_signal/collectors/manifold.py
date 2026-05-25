"""Manifold Markets prediction-market collector (free, no API key).

Searches Manifold Markets' public API for binary markets matching each
Polymarket topic and converts the revealed crowd probability into a
directional research signal. Manifold's probability is treated as a
peer prediction-market consensus -- a high-quality input distinct from
news headlines, especially for politics / geopolitics / sports / awards
markets where the rest of the research pipeline is thin.

Outputs per matched market:
  - source     : "manifold"
  - summary    : "Manifold: <question> -- 67%"
  - stance     : "bullish" | "bearish" | "neutral" by probability
  - extras     : probability, volume_usd, unique_bettors, outcome_type
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

_BASE_URL = "https://api.manifold.markets/v0"
_CACHE_TTL_SEC = 300.0
_DEFAULT_HEADERS = {
    "User-Agent": "PolymarketBot/1.0 (+research-signal)",
    "Accept": "application/json",
}


class ManifoldCollector:
    """Fetch binary prediction-market probabilities from Manifold Markets."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_sec: float = 5.0,
        cache_ttl_sec: float = _CACHE_TTL_SEC,
        max_results_per_topic: int = 2,
        min_volume_usd: float = 50.0,
        search_limit: int = 8,
    ) -> None:
        self._enabled = bool(enabled)
        self._timeout_sec = float(timeout_sec)
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._max_results_per_topic = int(max_results_per_topic)
        self._min_volume_usd = float(min_volume_usd)
        self._search_limit = int(search_limit)
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def collect(self, topics: Iterable[str]) -> list[dict]:
        if not self._enabled:
            return []

        rows: list[dict] = []
        for topic in topics:
            if not topic or not topic.strip():
                continue
            rows.extend(self._collect_topic(topic.strip()))
        return rows

    def _collect_topic(self, topic: str) -> list[dict]:
        now = time.time()
        cached = self._cache.get(topic)
        if cached and (now - cached[0]) < self._cache_ttl_sec:
            return [dict(row) for row in cached[1]]

        markets = self._search_markets(topic)
        rows: list[dict] = []
        for market in markets:
            row = self._build_row(topic, market, now)
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= self._max_results_per_topic:
                break

        self._cache[topic] = (now, [dict(r) for r in rows])
        return rows

    def _search_markets(self, topic: str) -> list[dict[str, Any]]:
        url = f"{_BASE_URL}/search-markets"
        params = {"term": topic[:120], "limit": self._search_limit}
        try:
            resp = requests.get(
                url,
                params=params,
                headers=_DEFAULT_HEADERS,
                timeout=self._timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("ManifoldCollector: fetch failed topic=%s err=%s", topic, exc)
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _build_row(self, topic: str, market: dict[str, Any], now: float) -> dict | None:
        if market.get("outcomeType") != "BINARY":
            return None
        if market.get("isResolved"):
            return None
        prob = market.get("probability")
        if not isinstance(prob, (int, float)):
            return None
        try:
            prob_val = float(prob)
        except (TypeError, ValueError):
            return None
        if prob_val < 0.0 or prob_val > 1.0:
            return None

        volume = _as_float(market.get("volume"))
        if volume < self._min_volume_usd:
            return None

        close_time_ms = market.get("closeTime")
        close_time_sec = _ms_to_sec(close_time_ms)
        if close_time_sec is not None and close_time_sec < now:
            return None

        question = str(market.get("question") or "").strip()
        if not question:
            return None

        last_updated_sec = _ms_to_sec(market.get("lastUpdatedTime")) or now
        link = str(market.get("url") or "").strip()
        if not link:
            slug = str(market.get("slug") or "").strip()
            creator = str(market.get("creatorUsername") or "").strip()
            if slug and creator:
                link = f"https://manifold.markets/{creator}/{slug}"

        summary = f"Manifold: {question[:120]} -- {prob_val * 100:.0f}%"
        return {
            "topic": topic,
            "source": "manifold",
            "summary": summary,
            "stance": _stance_from_probability(prob_val),
            "ts": now,
            "published_ts": last_updated_sec,
            "link": link,
            "extras": {
                "probability": prob_val,
                "volume_usd": volume,
                "unique_bettors": int(_as_float(market.get("uniqueBettorCount"))),
                "outcome_type": "BINARY",
                "close_time": close_time_sec,
                "market_id": str(market.get("id") or "").strip(),
            },
        }


def _stance_from_probability(prob: float) -> str:
    if prob >= 0.60:
        return "bullish"
    if prob <= 0.40:
        return "bearish"
    return "neutral"


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ms_to_sec(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v / 1000.0
