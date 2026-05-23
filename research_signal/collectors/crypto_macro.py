"""Crypto macro-sentiment collector (shadow-mode contribution).

Article 3 of the original research bundle proposed a four-dim resonance
(MVRV / SOPR / ETF / macro). Of those, only **Fear & Greed** + **ETF
netflow** are accessible on free, no-auth tiers. MVRV and SOPR require
paid Glassnode / CryptoQuant; until paid data is wired in, this
collector contributes a single sentiment row per crypto-keyword topic
so the bot at least has a *consistent* macro context next to the
existing news-based signals.

Outputs a row per detected crypto topic with:
  - source        : "fear_greed"
  - summary       : human-readable sentiment string
  - stance        : "bullish" | "bearish" | "neutral"
                    (heuristic from F&G; see _stance_from_fg below)
  - extras.value  : raw F&G numeric (0-100)
  - extras.label  : F&G classification ("Extreme Fear" ... "Extreme Greed")

The collector is intentionally NOT wired to modify trading on its own
— it feeds research_overlay via the existing group_by_topic / build_signal
pipeline. The orchestrator's `_apply_research_overlay` already handles
stance aggregation and conviction-based size adjustment.

Caching: F&G updates daily. Cache for 1h to avoid hammering the API on
each scan cycle.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

_FG_URL = "https://api.alternative.me/fng/?limit=1"
_CACHE_TTL_SEC = 3600.0

# Keywords that mark a topic as crypto-related. Conservative — false
# negatives (missed crypto topics) are cheaper than false positives
# (applying crypto sentiment to politics topics).
_CRYPTO_TOKENS = frozenset({
    "btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp", "ripple",
    "doge", "dogecoin", "ada", "cardano", "ltc", "litecoin", "shib", "shiba",
    "bch", "trx", "tron", "matic", "polygon", "avax", "avalanche",
    "crypto", "cryptocurrency", "blockchain", "defi", "nft", "stablecoin",
    "usdc", "usdt", "binance", "coinbase", "kraken", "saylor", "microstrategy",
})


class CryptoMacroCollector:
    """Surface Fear & Greed snapshot as a research_signal row for crypto topics."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_sec: float = 5.0,
        cache_ttl_sec: float = _CACHE_TTL_SEC,
        api_url: str = _FG_URL,
    ) -> None:
        self._enabled = bool(enabled)
        self._timeout_sec = float(timeout_sec)
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._api_url = api_url
        self._cache: tuple[float, dict[str, Any] | None] = (0.0, None)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def collect(self, topics: Iterable[str]) -> list[dict]:
        if not self._enabled:
            return []
        topics_list = [t for t in topics if t and t.strip()]
        if not topics_list:
            return []
        snapshot = self._snapshot_cached()
        if snapshot is None:
            return []
        now = time.time()
        rows: list[dict] = []
        for topic in topics_list:
            if not _is_crypto_topic(topic):
                continue
            rows.append(
                {
                    "topic": topic,
                    "source": "fear_greed",
                    "summary": _summary_text(snapshot),
                    "stance": _stance_from_fg(snapshot.get("value")),
                    "ts": now,
                    "published_ts": float(snapshot.get("ts") or now),
                    "extras": {
                        "fear_greed_value": snapshot.get("value"),
                        "fear_greed_label": snapshot.get("label"),
                    },
                    "link": "https://alternative.me/crypto/fear-and-greed-index/",
                }
            )
        return rows

    def _snapshot_cached(self) -> dict[str, Any] | None:
        now = time.time()
        ts, cached = self._cache
        if cached is not None and (now - ts) < self._cache_ttl_sec:
            return cached
        snap = self._fetch_snapshot()
        self._cache = (now, snap)
        return snap

    def _fetch_snapshot(self) -> dict[str, Any] | None:
        try:
            resp = requests.get(self._api_url, timeout=self._timeout_sec)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("CryptoMacroCollector: F&G fetch failed: %s", exc)
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            return None
        head = data[0] if isinstance(data[0], dict) else {}
        try:
            value = int(head.get("value", 0))
        except (TypeError, ValueError):
            return None
        label = str(head.get("value_classification") or "").strip() or _label_from_value(value)
        try:
            ts_unix = float(head.get("timestamp", time.time()))
        except (TypeError, ValueError):
            ts_unix = time.time()
        return {"value": value, "label": label, "ts": ts_unix}


def _is_crypto_topic(topic: str) -> bool:
    tokens = {t for t in re.split(r"[^a-z0-9]+", topic.lower()) if t}
    return bool(tokens & _CRYPTO_TOKENS)


def _summary_text(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("value")
    label = snapshot.get("label") or _label_from_value(value)
    return f"Fear & Greed = {value} ({label})"


def _stance_from_fg(value: Any) -> str:
    """Map F&G numeric to a stance.

    The classical contrarian read: extreme fear (≤25) ⇒ bottoms ⇒
    bullish on "BTC up"-style binaries. Extreme greed (≥75) ⇒ tops ⇒
    bearish. Middle band is neutral so the signal doesn't push the
    aggregator one way or the other on routine days.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return "neutral"
    if v <= 25:
        return "bullish"
    if v >= 75:
        return "bearish"
    return "neutral"


def _label_from_value(value: Any) -> str:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if v <= 25:
        return "Extreme Fear"
    if v <= 45:
        return "Fear"
    if v <= 55:
        return "Neutral"
    if v <= 75:
        return "Greed"
    return "Extreme Greed"
