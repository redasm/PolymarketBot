"""Economic calendar collector (free APIs: FRED, BLS, Trading Economics RSS).

Provides macro event context for markets sensitive to economic data releases.
Uses free endpoints that don't require API keys.

Outputs per detected macro topic:
  - source     : "econ_calendar"
  - summary    : upcoming/recent economic event description
  - stance     : "bullish" | "bearish" | "neutral" based on surprise direction
  - extras     : indicator, actual, forecast, previous
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

_CACHE_TTL_SEC = 1800.0

_MACRO_KEYWORDS = frozenset({
    "fed", "fomc", "rate cut", "rate hike", "interest rate",
    "cpi", "inflation", "pce", "deflation",
    "gdp", "recession", "growth",
    "unemployment", "jobless", "payroll", "nonfarm", "jobs",
    "treasury", "yield", "bond",
    "dollar", "dxy", "usd",
})

_INDICATOR_RSS_FEEDS = [
    ("tradingeconomics_us", "https://tradingeconomics.com/united-states/rss"),
    ("tradingeconomics_calendar", "https://tradingeconomics.com/calendar/rss"),
]

_DEFAULT_HEADERS = {
    "User-Agent": "PolymarketBot/1.0 (+research-signal)",
    "Accept": "application/rss+xml, application/xml, text/xml",
}


class EconCalendarCollector:
    """Collect economic calendar events from free RSS feeds."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_sec: float = 5.0,
        cache_ttl_sec: float = _CACHE_TTL_SEC,
        max_items_per_feed: int = 5,
    ) -> None:
        self._enabled = bool(enabled)
        self._timeout_sec = float(timeout_sec)
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._max_items_per_feed = max_items_per_feed
        self._cache: tuple[float, list[dict]] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def collect(self, topics: Iterable[str]) -> list[dict]:
        if not self._enabled:
            return []
        topics_list = [t for t in topics if t and t.strip()]
        if not topics_list:
            return []

        macro_topics = [t for t in topics_list if _is_macro_topic(t)]
        if not macro_topics:
            return []

        events = self._get_cached_events()
        if not events:
            return []

        now = time.time()
        rows: list[dict] = []
        for topic in macro_topics:
            matched = _match_events_to_topic(topic, events)
            for event in matched[:3]:
                rows.append({
                    "topic": topic,
                    "source": "econ_calendar",
                    "summary": event["title"],
                    "stance": event.get("stance", "neutral"),
                    "ts": now,
                    "published_ts": event.get("pub_ts", now),
                    "link": event.get("link", ""),
                    "extras": {
                        "indicator": event.get("indicator", ""),
                        "feed_source": event.get("feed_source", ""),
                    },
                })
        return rows

    def _get_cached_events(self) -> list[dict]:
        now = time.time()
        if self._cache and (now - self._cache[0]) < self._cache_ttl_sec:
            return self._cache[1]

        events = self._fetch_all_feeds()
        self._cache = (now, events)
        return events

    def _fetch_all_feeds(self) -> list[dict]:
        all_events: list[dict] = []
        for feed_name, feed_url in _INDICATOR_RSS_FEEDS:
            events = self._fetch_rss_feed(feed_name, feed_url)
            all_events.extend(events)
        return all_events

    def _fetch_rss_feed(self, feed_name: str, url: str) -> list[dict]:
        try:
            resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=self._timeout_sec)
            resp.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("EconCalendarCollector: %s fetch failed: %s", feed_name, exc)
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return []

        events: list[dict] = []
        for item in root.findall(".//item")[: self._max_items_per_feed]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            pub_ts = _parse_pub_ts(pub_date)

            events.append({
                "title": title,
                "link": link,
                "pub_ts": pub_ts,
                "feed_source": feed_name,
                "indicator": _extract_indicator(title),
                "stance": _infer_stance_from_title(title),
            })
        return events


def _is_macro_topic(topic: str) -> bool:
    lower = topic.lower()
    return any(kw in lower for kw in _MACRO_KEYWORDS)


def _match_events_to_topic(topic: str, events: list[dict]) -> list[dict]:
    topic_lower = topic.lower()
    topic_tokens = set(re.split(r"[^a-z0-9]+", topic_lower))

    scored: list[tuple[float, dict]] = []
    for event in events:
        title_lower = event["title"].lower()
        title_tokens = set(re.split(r"[^a-z0-9]+", title_lower))
        overlap = len(topic_tokens & title_tokens)
        if overlap >= 1 or any(kw in title_lower for kw in _MACRO_KEYWORDS if kw in topic_lower):
            scored.append((overlap, event))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ev for _, ev in scored]


def _extract_indicator(title: str) -> str:
    indicators = ["CPI", "GDP", "PCE", "NFP", "PMI", "PPI", "FOMC", "Fed"]
    for ind in indicators:
        if ind.lower() in title.lower():
            return ind
    return ""


def _infer_stance_from_title(title: str) -> str:
    lower = title.lower()
    bullish_signals = ["beat", "above", "surge", "rise", "strong", "rate cut", "dovish"]
    bearish_signals = ["miss", "below", "fall", "weak", "rate hike", "hawkish", "decline"]
    bull_score = sum(1 for s in bullish_signals if s in lower)
    bear_score = sum(1 for s in bearish_signals if s in lower)
    if bull_score > bear_score:
        return "bullish"
    if bear_score > bull_score:
        return "bearish"
    return "neutral"


def _parse_pub_ts(raw: str) -> float | None:
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        return None
