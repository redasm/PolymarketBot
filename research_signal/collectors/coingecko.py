"""CoinGecko price/market-cap collector (free tier, no API key).

Provides real-time crypto price context for crypto-keyword topics.
Uses the free /simple/price endpoint (rate-limited to ~10-30 req/min).

Outputs per detected crypto topic:
  - source     : "coingecko"
  - summary    : human-readable price + 24h change
  - stance     : "bullish" | "bearish" | "neutral" based on 24h change
  - extras     : price_usd, market_cap, change_24h, volume_24h
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

_BASE_URL = "https://api.coingecko.com/api/v3"
_CACHE_TTL_SEC = 300.0

_COIN_MAP: dict[str, str] = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "xrp": "ripple",
    "ripple": "ripple",
    "doge": "dogecoin",
    "dogecoin": "dogecoin",
    "ada": "cardano",
    "cardano": "cardano",
    "avax": "avalanche-2",
    "avalanche": "avalanche-2",
    "matic": "matic-network",
    "polygon": "matic-network",
    "dot": "polkadot",
    "polkadot": "polkadot",
    "link": "chainlink",
    "chainlink": "chainlink",
    "shib": "shiba-inu",
    "ltc": "litecoin",
    "litecoin": "litecoin",
}

_CRYPTO_TOKENS = frozenset(_COIN_MAP.keys())


class CoinGeckoCollector:
    """Fetch crypto price snapshots from CoinGecko free API."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_sec: float = 5.0,
        cache_ttl_sec: float = _CACHE_TTL_SEC,
    ) -> None:
        self._enabled = bool(enabled)
        self._timeout_sec = float(timeout_sec)
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def collect(self, topics: Iterable[str]) -> list[dict]:
        if not self._enabled:
            return []
        coin_ids: set[str] = set()
        topic_coins: dict[str, list[str]] = {}
        for topic in topics:
            if not topic or not topic.strip():
                continue
            coins = _extract_coins(topic)
            if coins:
                topic_coins[topic] = coins
                coin_ids.update(coins)
        if not coin_ids:
            return []

        prices = self._fetch_prices(list(coin_ids))
        if not prices:
            return []

        now = time.time()
        rows: list[dict] = []
        for topic, coins in topic_coins.items():
            for coin_id in coins:
                data = prices.get(coin_id)
                if not data:
                    continue
                rows.append(self._build_row(topic, coin_id, data, now))
        return rows

    def _fetch_prices(self, coin_ids: list[str]) -> dict[str, dict[str, Any]]:
        now = time.time()
        to_fetch: list[str] = []
        cached_results: dict[str, dict[str, Any]] = {}

        for cid in coin_ids:
            cached = self._cache.get(cid)
            if cached and (now - cached[0]) < self._cache_ttl_sec:
                cached_results[cid] = cached[1]
            else:
                to_fetch.append(cid)

        if to_fetch:
            fetched = self._api_fetch(to_fetch)
            for cid, data in fetched.items():
                self._cache[cid] = (now, data)
                cached_results[cid] = data

        return cached_results

    def _api_fetch(self, coin_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids_str = ",".join(coin_ids[:10])
        url = f"{_BASE_URL}/simple/price"
        params = {
            "ids": ids_str,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
        }
        try:
            resp = requests.get(url, params=params, timeout=self._timeout_sec)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("CoinGeckoCollector: fetch failed: %s", exc)
            return {}

    def _build_row(self, topic: str, coin_id: str, data: dict, now: float) -> dict:
        price = data.get("usd", 0)
        change_24h = data.get("usd_24h_change", 0)
        market_cap = data.get("usd_market_cap", 0)
        volume_24h = data.get("usd_24h_vol", 0)

        return {
            "topic": topic,
            "source": "coingecko",
            "summary": f"{coin_id}: ${price:,.2f} ({change_24h:+.1f}% 24h)",
            "stance": _stance_from_change(change_24h),
            "ts": now,
            "published_ts": now,
            "link": f"https://www.coingecko.com/en/coins/{coin_id}",
            "extras": {
                "coin_id": coin_id,
                "price_usd": price,
                "change_24h_pct": change_24h,
                "market_cap_usd": market_cap,
                "volume_24h_usd": volume_24h,
            },
        }


def _extract_coins(topic: str) -> list[str]:
    tokens = {t for t in re.split(r"[^a-z0-9]+", topic.lower()) if t}
    matched = tokens & _CRYPTO_TOKENS
    coin_ids = list({_COIN_MAP[t] for t in matched})
    return sorted(coin_ids)


def _stance_from_change(change_24h: Any) -> str:
    try:
        v = float(change_24h)
    except (TypeError, ValueError):
        return "neutral"
    if v >= 5.0:
        return "bullish"
    if v <= -5.0:
        return "bearish"
    return "neutral"
