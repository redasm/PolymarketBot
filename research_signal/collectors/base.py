"""Research signal collectors."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import json
import logging
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
LOG = logging.getLogger(__name__)


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


class GenericHTTPJSONCollector:
    """Collect topic-conditioned rows from generic JSON APIs.

    Each source config supports keys like:
    - name
    - url
    - items_path
    - summary_path
    - link_path
    - published_path
    - topic_param
    """

    def __init__(
        self,
        sources: list[dict] | None = None,
        timeout_sec: float = 5.0,
        max_items_per_source: int = 2,
    ) -> None:
        self._sources = sources or []
        self._timeout_sec = timeout_sec
        self._max_items_per_source = max_items_per_source

    def collect(self, topics: list[str]) -> list[dict]:
        rows: list[dict] = []
        for topic in topics:
            if not topic.strip():
                continue
            for source in self._sources:
                rows.extend(self._collect_source(source, topic))
        return rows

    def _collect_source(self, source: dict, topic: str) -> list[dict]:
        name = str(source.get("name") or "http_json").strip() or "http_json"
        topic_param = str(source.get("topic_param") or "q").strip() or "q"
        raw_url = str(source.get("url") or "").strip()
        if not raw_url:
            return []

        query = urllib.parse.quote_plus(topic[:120])
        try:
            url = raw_url.format(query=query, topic=query)
        except Exception:
            url = raw_url

        params = dict(source.get("params") or {})
        params.setdefault(topic_param, topic[:120])
        headers = {
            "Accept": "application/json",
            "User-Agent": _DEFAULT_HEADERS["User-Agent"],
            **dict(source.get("headers") or {}),
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self._timeout_sec)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []

        items = _lookup_path(payload, str(source.get("items_path") or "")) if source.get("items_path") else payload
        if not isinstance(items, list):
            return []

        rows: list[dict] = []
        now = time.time()
        for item in items[: self._max_items_per_source]:
            if not isinstance(item, dict):
                continue
            summary = _lookup_path(item, str(source.get("summary_path") or "title"))
            if not summary:
                continue
            published_value = _lookup_path(item, str(source.get("published_path") or "")) if source.get("published_path") else ""
            published_text = str(published_value or "").strip()
            rows.append(
                {
                    "topic": topic,
                    "summary": str(summary).strip()[:500],
                    "source": name,
                    "ts": now,
                    "published_at": published_text,
                    "published_ts": _parse_http_published_ts(published_value),
                    "link": str(_lookup_path(item, str(source.get("link_path") or "url")) or "").strip(),
                }
            )
        return rows


class SurfSignalCollector:
    """Collect concise crypto intelligence summaries from Surf AI.

    The collector is optional and intended for research augmentation only.
    It should not sit on the critical execution path.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        api_key: str = "",
        api_base: str = "https://api.asksurf.ai/surf-ai",
        model: str = "surf-1.5-instant",
        timeout_sec: float = 8.0,
        cache_ttl_sec: float = 1800.0,
        max_items_per_topic: int = 1,
    ) -> None:
        self._enabled = enabled
        self._api_key = api_key.strip()
        self._api_base = api_base.rstrip("/")
        self._model = model.strip() or "surf-1.5-instant"
        self._timeout_sec = timeout_sec
        self._cache_ttl_sec = cache_ttl_sec
        self._max_items_per_topic = max_items_per_topic
        self._cache: dict[str, tuple[float, dict]] = {}

    def collect(self, topics: list[str]) -> list[dict]:
        if not self._enabled or not self._api_key:
            return []
        rows: list[dict] = []
        for topic in topics[: self._max_items_per_topic * max(1, len(topics))]:
            if not topic.strip():
                continue
            cached = self._cache.get(topic)
            now = time.time()
            if cached and (now - cached[0]) < self._cache_ttl_sec:
                rows.append(dict(cached[1]))
                continue
            row = self._collect_topic(topic)
            if row is not None:
                self._cache[topic] = (now, dict(row))
                rows.append(row)
        return rows

    def _collect_topic(self, topic: str) -> dict | None:
        url = f"{self._api_base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a crypto market intelligence analyst. "
                        "Return one concise factual paragraph focused on trade-relevant developments."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Summarize the most relevant crypto intelligence for this market topic: {topic}\n"
                        "Focus on catalysts, sentiment, positioning, onchain context, and market structure. "
                        "Keep it under 80 words."
                    ),
                },
            ],
            "temperature": 0.1,
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self._timeout_sec)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            LOG.debug("surf_ai collector request failed: model=%s topic=%s error=%s", self._model, topic[:80], type(e).__name__)
            return None

        summary = (
            _lookup_path(data, "choices.0.message.content")
            or _lookup_path(data, "choices.0.text")
            or ""
        )
        summary_text = str(summary).strip()
        if not summary_text:
            LOG.debug("surf_ai collector returned empty summary: model=%s topic=%s", self._model, topic[:80])
            return None
        now = time.time()
        return {
            "topic": topic,
            "summary": summary_text[:500],
            "source": "surf_ai",
            "ts": now,
            "published_ts": now,
            "link": "",
            "model": self._model,
        }


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
            stance = str(doc.get("stance") or "").strip().lower()
            row = {
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
            if stance in {"bullish", "bearish", "uncertain"}:
                row["stance"] = stance
            rows.append(row)
        return rows


def _lookup_path(payload: dict | list | None, path: str) -> object:
    if payload is None or not path:
        return payload
    current = payload
    for part in path.split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return None
            if index < 0 or index >= len(current):
                return None
            current = current[index]
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _parse_http_published_ts(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return _parse_published_ts(raw)
