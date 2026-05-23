"""Service for collecting and summarizing research signals."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from polymarket_arb.models import MarketInfo, ResearchSignal, ResearchSignalReport
from research_signal.collectors.base import (
    GenericHTTPJSONCollector,
    GenericRSSCollector,
    PolymarketEventCollector,
    WebSearchCollector,
)
from research_signal.collectors.coingecko import CoinGeckoCollector
from research_signal.collectors.crypto_macro import CryptoMacroCollector
from research_signal.collectors.defillama import DeFiLlamaCollector
from research_signal.collectors.econ_calendar import EconCalendarCollector
from research_signal.collectors.funding_rate import FundingRateCollector
from research_signal.collectors.polymarket_activity import PolymarketActivityCollector
from research_signal.normalizers.topic import group_by_topic, normalize_topic, topic_overlap_score
from research_signal.scorers.source_profile import resolve_source_profile
from research_signal.summaries.builder import build_signal

LOG = logging.getLogger(__name__)


class ResearchSignalService:
    def __init__(
        self,
        max_items: int = 5,
        cache_ttl_sec: int = 300,
        cache_dir: str = "data/research_signal",
        feeds_file: str | None = None,
        http_json_sources: list[dict] | None = None,
        crypto_macro_enabled: bool = False,
        coingecko_enabled: bool = False,
        funding_rate_enabled: bool = False,
        econ_calendar_enabled: bool = False,
        defillama_enabled: bool = False,
        polymarket_activity_enabled: bool = False,
    ):
        self._max_items = max_items
        self._cache_ttl_sec = cache_ttl_sec
        self._cache_dir = Path(cache_dir)
        self._event_collector = PolymarketEventCollector()
        self._web_collector = WebSearchCollector()
        self._feeds_file = Path(feeds_file).resolve() if feeds_file else None
        self._feeds_mtime: float = 0.0
        self._generic_rss_collector = GenericRSSCollector(self._load_feeds_from_file())
        self._http_json_collector = GenericHTTPJSONCollector(http_json_sources or [])
        self._crypto_macro_collector = CryptoMacroCollector(enabled=crypto_macro_enabled)
        self._coingecko_collector = CoinGeckoCollector(enabled=coingecko_enabled)
        self._funding_rate_collector = FundingRateCollector(enabled=funding_rate_enabled)
        self._econ_calendar_collector = EconCalendarCollector(enabled=econ_calendar_enabled)
        self._defillama_collector = DeFiLlamaCollector(enabled=defillama_enabled)
        self._polymarket_activity_collector = PolymarketActivityCollector(enabled=polymarket_activity_enabled)
        enabled_sources = [
            name for name, flag in [
                ("crypto_macro", crypto_macro_enabled),
                ("coingecko", coingecko_enabled),
                ("funding_rate", funding_rate_enabled),
                ("econ_calendar", econ_calendar_enabled),
                ("defillama", defillama_enabled),
                ("polymarket_activity", polymarket_activity_enabled),
            ] if flag
        ]
        if enabled_sources:
            LOG.info("Research signal collectors enabled: %s", ", ".join(enabled_sources))
        self._cache: dict[tuple[tuple[str, ...], int], tuple[float, ResearchSignalReport]] = {}

    def get_signals(self, markets: list[MarketInfo], window_sec: int) -> list[ResearchSignal]:
        return self.collect_report(markets, window_sec).signals

    def collect_report(self, markets: list[MarketInfo], window_sec: int) -> ResearchSignalReport:
        topic_key = tuple(
            sorted(
                f"{market.event_id}:{market.condition_id}:{normalize_topic(market.question)[:80]}"
                for market in markets[: self._max_items]
            )
        )
        cache_key = (topic_key, window_sec)
        now = time.time()

        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self._cache_ttl_sec:
            return self._clone_report(cached[1], cache_hit=True)

        disk_cached = self._load_disk_cache(cache_key, now)
        if disk_cached is not None:
            self._cache[cache_key] = (now, disk_cached)
            return self._clone_report(disk_cached, cache_hit=True)

        topics = [market.question for market in markets[: self._max_items]]
        self._maybe_reload_feeds()
        collected_rows = self._event_collector.collect(markets)
        collected_rows.extend(self._web_collector.collect(topics))
        collected_rows.extend(self._generic_rss_collector.collect(topics))
        collected_rows.extend(self._http_json_collector.collect(topics))
        collected_rows.extend(self._crypto_macro_collector.collect(topics))
        collected_rows.extend(self._coingecko_collector.collect(topics))
        collected_rows.extend(self._funding_rate_collector.collect(topics))
        collected_rows.extend(self._econ_calendar_collector.collect(topics))
        collected_rows.extend(self._defillama_collector.collect(topics))
        collected_rows.extend(self._polymarket_activity_collector.collect(markets))

        prepared_rows, dropped_rows = self._prepare_rows(collected_rows, window_sec=window_sec, now=now)
        grouped = group_by_topic(prepared_rows)
        signals = [build_signal(topic_id, rows) for topic_id, rows in grouped.items()]
        signals.sort(key=lambda signal: (-signal.confidence, signal.freshness_sec, signal.topic_id))
        signals = signals[: self._max_items]
        report = ResearchSignalReport(
            generated_at=now,
            window_sec=window_sec,
            market_count=len(markets),
            row_count=len(prepared_rows),
            topic_count=len(grouped),
            source_counts=dict(Counter(row.get("source", "unknown") for row in prepared_rows)),
            signals=signals,
            dropped_rows=dropped_rows,
        )
        self._cache[cache_key] = (now, report)
        self._save_disk_cache(cache_key, report)
        return self._clone_report(report, cache_hit=False)

    def attach_to_markets(self, markets: list[MarketInfo], signals: list[ResearchSignal]) -> list[MarketInfo]:
        signal_dicts = [signal.to_dict() for signal in signals]
        for market in markets:
            matched = [
                signal for signal in signal_dicts
                if self._signal_matches_market(signal, market)
            ]
            market.raw["research_signals"] = matched[:3]
        return markets

    def _load_feeds_from_file(self) -> list[tuple[str, str]]:
        if self._feeds_file is None or not self._feeds_file.exists():
            self._feeds_mtime = 0.0
            return []
        try:
            mtime = self._feeds_file.stat().st_mtime
            payload = json.loads(self._feeds_file.read_text(encoding="utf-8"))
        except Exception as e:
            LOG.warning("research feeds file 解析失败 path=%s err=%s", self._feeds_file, e)
            return []
        self._feeds_mtime = mtime
        rows = payload.get("feeds", []) if isinstance(payload, dict) else []
        feeds: list[tuple[str, str]] = []
        for idx, row in enumerate(rows if isinstance(rows, list) else [], start=1):
            if not isinstance(row, dict):
                continue
            template = str(row.get("url_template") or row.get("url") or "").strip()
            if not template or "{query}" not in template:
                continue
            name = str(row.get("name") or "").strip() or f"llm_feed_{idx}"
            feeds.append((name, template))
        return feeds

    def _maybe_reload_feeds(self) -> None:
        if self._feeds_file is None:
            return
        try:
            mtime = self._feeds_file.stat().st_mtime if self._feeds_file.exists() else 0.0
        except OSError:
            return
        if mtime == self._feeds_mtime:
            return
        feeds = self._load_feeds_from_file()
        self._generic_rss_collector = GenericRSSCollector(feeds)
        LOG.info("research feeds reloaded: count=%d path=%s", len(feeds), self._feeds_file)

    def _cache_path(self, cache_key: tuple[tuple[str, ...], int]) -> Path:
        topics, window_sec = cache_key
        safe_parts = []
        for topic in topics[:3]:
            cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", topic).strip("_")
            safe_parts.append(cleaned[:30] or "topic")
        slug = "_".join(safe_parts) or "empty"
        return self._cache_dir / f"{slug}_{window_sec}.json"

    def _load_disk_cache(
        self,
        cache_key: tuple[tuple[str, ...], int],
        now: float,
    ) -> ResearchSignalReport | None:
        path = self._cache_path(cache_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        created_at = float(payload.get("created_at", 0.0))
        if now - created_at >= self._cache_ttl_sec:
            return None
        rows = payload.get("signals", [])
        return ResearchSignalReport(
            generated_at=created_at,
            window_sec=int(payload.get("window_sec", cache_key[1])),
            market_count=int(payload.get("market_count", 0)),
            row_count=int(payload.get("row_count", len(rows))),
            topic_count=int(payload.get("topic_count", len(rows))),
            source_counts=dict(payload.get("source_counts", {})),
            cache_hit=False,
            dropped_rows=int(payload.get("dropped_rows", 0)),
            signals=[
                ResearchSignal(
                    topic_id=row.get("topic_id", ""),
                    event_candidates=list(row.get("event_candidates", [])),
                    summary=row.get("summary", ""),
                    sources=list(row.get("sources", [])),
                    confidence=float(row.get("confidence", 0.0)),
                    freshness_sec=float(row.get("freshness_sec", 0.0)),
                    stance=row.get("stance", "uncertain"),
                    metadata=dict(row.get("metadata", {})),
                )
                for row in rows
            ],
        )

    def _save_disk_cache(
        self,
        cache_key: tuple[tuple[str, ...], int],
        report: ResearchSignalReport,
    ) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(cache_key)
        payload = {
            "created_at": report.generated_at,
            "window_sec": report.window_sec,
            "market_count": report.market_count,
            "row_count": report.row_count,
            "topic_count": report.topic_count,
            "source_counts": report.source_counts,
            "dropped_rows": report.dropped_rows,
            "signals": [signal.to_dict() for signal in report.signals],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _prepare_rows(
        self,
        rows: list[dict],
        *,
        window_sec: int,
        now: float,
    ) -> tuple[list[dict], int]:
        dropped = 0
        candidates: list[dict] = []
        for row in rows:
            normalized = self._normalize_row(row)
            if normalized is None:
                dropped += 1
                continue

            signal_ts = float(normalized.get("published_ts") or normalized.get("ts") or now)
            if signal_ts and (now - signal_ts) > window_sec:
                dropped += 1
                continue
            candidates.append(normalized)

        prepared: list[dict] = []
        seen_summary_keys: set[str] = set()
        seen_link_keys: set[str] = set()
        ordered = sorted(candidates, key=lambda row: self._row_quality(row, now=now), reverse=True)

        for normalized in ordered:
            summary_key = normalize_topic(normalized.get("summary", "") or normalized.get("topic", ""))[:160]
            link_key = self._canonical_link(
                normalized.get("link") or normalized.get("event_id") or normalized.get("condition_id") or ""
            )
            source_type = str(normalized.get("source_profile", {}).get("type", "unknown"))

            duplicate_by_link = bool(link_key) and link_key in seen_link_keys
            duplicate_by_summary = (
                bool(summary_key)
                and summary_key in seen_summary_keys
                and source_type != "market_seed"
            )
            if duplicate_by_link or duplicate_by_summary:
                dropped += 1
                continue

            if link_key:
                seen_link_keys.add(link_key)
            if summary_key:
                seen_summary_keys.add(summary_key)
            prepared.append(normalized)
        return prepared, dropped

    def _normalize_row(self, row: dict) -> dict | None:
        topic = (row.get("topic") or row.get("summary") or "").strip()
        summary = (row.get("summary") or topic).strip()
        if not summary:
            return None
        normalized = dict(row)
        normalized["topic"] = topic
        normalized["summary"] = summary
        normalized["source"] = str(row.get("source") or "unknown").strip() or "unknown"
        normalized["event_id"] = str(row.get("event_id") or "").strip()
        normalized["condition_id"] = str(row.get("condition_id") or "").strip()
        normalized["link"] = str(row.get("link") or "").strip()
        normalized["ts"] = float(row.get("ts") or time.time())
        published_ts = row.get("published_ts")
        normalized["published_ts"] = float(published_ts) if published_ts else None
        normalized["source_profile"] = resolve_source_profile(normalized["source"], normalized["link"])
        return normalized

    def _row_quality(self, row: dict, *, now: float) -> float:
        profile = row.get("source_profile", {}) if isinstance(row.get("source_profile"), dict) else {}
        weight = float(profile.get("weight", 0.55))
        ts = float(row.get("published_ts") or row.get("ts") or now)
        age_hours = max(0.0, now - ts) / 3600.0
        freshness_boost = max(0.0, 1.0 - min(1.0, age_hours / 24.0))
        return weight + (0.25 * freshness_boost)

    def _canonical_link(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("evt:") or value.startswith("cond:"):
            return value
        if re.fullmatch(r"[a-zA-Z0-9_-]{4,}", value):
            return value
        try:
            parsed = urlparse(value)
        except Exception:
            return value[:160]
        host = (parsed.netloc or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        path = (parsed.path or "").rstrip("/")
        return f"{host}{path}"[:160]

    def _signal_matches_market(self, signal: dict, market: MarketInfo) -> bool:
        if market.event_id and market.event_id in signal.get("event_candidates", []):
            return True
        canonical_topic = signal.get("metadata", {}).get("canonical_topic", "")
        summary = signal.get("summary", "")
        best_score = max(
            topic_overlap_score(market.question, canonical_topic),
            topic_overlap_score(market.question, summary),
        )
        return best_score >= 0.5

    def _clone_report(self, report: ResearchSignalReport, *, cache_hit: bool) -> ResearchSignalReport:
        return ResearchSignalReport(
            generated_at=report.generated_at,
            window_sec=report.window_sec,
            market_count=report.market_count,
            row_count=report.row_count,
            topic_count=report.topic_count,
            source_counts=dict(report.source_counts),
            signals=[
                ResearchSignal(
                    topic_id=signal.topic_id,
                    event_candidates=list(signal.event_candidates),
                    summary=signal.summary,
                    sources=list(signal.sources),
                    confidence=signal.confidence,
                    freshness_sec=signal.freshness_sec,
                    stance=signal.stance,
                    metadata=dict(signal.metadata),
                )
                for signal in report.signals
            ],
            cache_hit=cache_hit,
            dropped_rows=report.dropped_rows,
        )
