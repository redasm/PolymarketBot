"""Research signal collectors."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

import requests

from polymarket_arb.models import MarketInfo
from research_signal.normalizers.topic import extract_topic_keywords, topic_overlap_score

_DEFAULT_HEADERS = {
    "User-Agent": "PolymarketBot/1.0 (+research-signal)",
    "Accept": "application/rss+xml, application/xml, text/xml",
}


def _parse_published_ts(raw_value: str) -> float | None:
    if not raw_value.strip():
        return None
    try:
        return parsedate_to_datetime(raw_value).timestamp()
    except Exception:
        return None


class PolymarketEventCollector:
    def collect(self, markets: Iterable[MarketInfo]) -> list[dict]:
        now = time.time()
        collected: list[dict] = []
        for market in markets:
            collected.append(
                {
                    "topic": market.question,
                    "event_id": market.event_id,
                    "condition_id": market.condition_id,
                    "summary": market.question,
                    "source": "polymarket_market",
                    "ts": now,
                    "slug": market.slug,
                    "event_slug": market.event_slug,
                }
            )
        return collected


class WebSearchCollector:
    """Collect lightweight external context from public RSS feeds."""

    def __init__(self, timeout_sec: float = 5.0, max_items_per_topic: int = 3):
        self._timeout_sec = timeout_sec
        self._max_items_per_topic = max_items_per_topic

    def collect(self, topics: list[str]) -> list[dict]:
        rows: list[dict] = []
        for topic in topics:
            rows.extend(self._collect_google_news_rss(topic))
        return rows

    def _collect_google_news_rss(self, topic: str) -> list[dict]:
        if not topic.strip():
            return []

        query = urllib.parse.quote_plus(topic[:120])
        url = f"https://news.google.com/rss/search?q={query}"
        try:
            resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=self._timeout_sec)
            resp.raise_for_status()
        except Exception:
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return []

        rows: list[dict] = []
        now = time.time()
        for item in root.findall(".//item")[: self._max_items_per_topic]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            published_at = (item.findtext("pubDate") or "").strip()
            rows.append(
                {
                    "topic": topic,
                    "summary": title,
                    "source": "google_news_rss",
                    "ts": now,
                    "published_at": published_at,
                    "published_ts": _parse_published_ts(published_at),
                    "link": (item.findtext("link") or "").strip(),
                }
            )
        return rows


class GenericRSSCollector:
    """Collect context from configurable RSS endpoints.

    Feed templates should include `{query}` which will be replaced by a URL-encoded topic.
    """

    def __init__(
        self,
        feeds: list[tuple[str, str]] | None = None,
        timeout_sec: float = 5.0,
        max_items_per_feed: int = 2,
    ) -> None:
        self._feeds = feeds or []
        self._timeout_sec = timeout_sec
        self._max_items_per_feed = max_items_per_feed

    def collect(self, topics: list[str]) -> list[dict]:
        rows: list[dict] = []
        for topic in topics:
            if not topic.strip():
                continue
            for feed_name, feed_template in self._feeds:
                rows.extend(self._collect_feed(feed_name, feed_template, topic))
        return rows

    def _collect_feed(self, feed_name: str, feed_template: str, topic: str) -> list[dict]:
        query = urllib.parse.quote_plus(topic[:120])
        try:
            url = feed_template.format(query=query, topic=query)
        except Exception:
            return []

        try:
            resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=self._timeout_sec)
            resp.raise_for_status()
        except Exception:
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return []

        now = time.time()
        rows: list[dict] = []
        for item in root.findall(".//item")[: self._max_items_per_feed]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            published_at = (item.findtext("pubDate") or "").strip()
            rows.append(
                {
                    "topic": topic,
                    "summary": title,
                    "source": feed_name,
                    "ts": now,
                    "published_at": published_at,
                    "published_ts": _parse_published_ts(published_at),
                    "link": (item.findtext("link") or "").strip(),
                }
            )
        return rows


class LocalKnowledgeBaseCollector:
    """Collect context from local JSONL knowledge files.

    Each line may contain keys like topic, summary, event_id, tags, link, published_ts.
    """

    def __init__(self, knowledge_dir: str, max_matches_per_topic: int = 3) -> None:
        self._knowledge_dir = Path(knowledge_dir)
        self._max_matches_per_topic = max_matches_per_topic

    def collect(self, topics: list[str]) -> list[dict]:
        if not self._knowledge_dir.exists():
            return []

        docs = self._load_docs()
        rows: list[dict] = []
        for topic in topics:
            rows.extend(self._match_docs(topic, docs))
        return rows

    def _load_docs(self) -> list[dict]:
        docs: list[dict] = []
        for path in sorted(self._knowledge_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row["_knowledge_file"] = path.name
                docs.append(row)
        return docs

    def _match_docs(self, topic: str, docs: list[dict]) -> list[dict]:
        scored: list[tuple[float, dict]] = []
        topic_keywords = set(extract_topic_keywords(topic, limit=12))
        if not topic_keywords:
            return []

        for doc in docs:
            text = " ".join(
                [
                    str(doc.get("topic", "")),
                    str(doc.get("summary", "")),
                    " ".join(str(tag) for tag in doc.get("tags", [])),
                ]
            ).strip()
            if not text:
                continue
            overlap = topic_overlap_score(topic, text)
            tag_overlap = len(topic_keywords & set(extract_topic_keywords(text, limit=20)))
            score = overlap + (0.1 * tag_overlap)
            if score < 0.5:
                continue
            scored.append((score, doc))

        now = time.time()
        rows: list[dict] = []
        for _, doc in sorted(scored, key=lambda item: item[0], reverse=True)[: self._max_matches_per_topic]:
            published_ts = doc.get("published_ts")
            ts = float(doc.get("ts") or published_ts or now)
            rows.append(
                {
                    "topic": topic,
                    "summary": str(doc.get("summary") or doc.get("topic") or "")[:500],
                    "source": str(doc.get("source") or "local_knowledge_base"),
                    "event_id": str(doc.get("event_id") or "").strip(),
                    "condition_id": str(doc.get("condition_id") or "").strip(),
                    "ts": ts,
                    "published_ts": float(published_ts) if published_ts is not None else None,
                    "link": str(doc.get("link") or "").strip(),
                    "tags": list(doc.get("tags", []))[:8],
                    "knowledge_file": doc.get("_knowledge_file", ""),
                }
            )
        return rows
