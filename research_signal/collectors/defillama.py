"""DeFiLlama TVL/flow collector (free, no API key).

Tracks Total Value Locked across DeFi protocols — a proxy for
capital flow sentiment in crypto markets.

Outputs per detected crypto/DeFi topic:
  - source     : "defillama"
  - summary    : TVL snapshot + 24h change
  - stance     : "bullish" | "bearish" | "neutral" based on TVL trend
  - extras     : total_tvl, change_1d, chain or protocol
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

_BASE_URL = "https://api.llama.fi"
_CACHE_TTL_SEC = 600.0

_CHAIN_MAP: dict[str, str] = {
    "eth": "Ethereum",
    "ethereum": "Ethereum",
    "sol": "Solana",
    "solana": "Solana",
    "avax": "Avalanche",
    "avalanche": "Avalanche",
    "matic": "Polygon",
    "polygon": "Polygon",
    "bsc": "BSC",
    "binance": "BSC",
    "arbitrum": "Arbitrum",
    "arb": "Arbitrum",
    "optimism": "Optimism",
    "op": "Optimism",
    "base": "Base",
}

_DEFI_KEYWORDS = frozenset({
    "defi", "tvl", "liquidity", "lending", "dex", "yield",
    "aave", "uniswap", "compound", "maker", "lido", "curve",
    *_CHAIN_MAP.keys(),
})


class DeFiLlamaCollector:
    """Fetch DeFi TVL data from DeFiLlama free API."""

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
        self._cache: tuple[float, dict[str, Any]] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def collect(self, topics: Iterable[str]) -> list[dict]:
        if not self._enabled:
            return []
        topics_list = [t for t in topics if t and t.strip()]
        if not topics_list:
            return []

        defi_topics = [t for t in topics_list if _is_defi_topic(t)]
        if not defi_topics:
            return []

        tvl_data = self._get_cached_tvl()
        if not tvl_data:
            return []

        now = time.time()
        rows: list[dict] = []
        for topic in defi_topics:
            chains = _extract_chains(topic)
            if chains:
                for chain_name in chains:
                    chain_data = tvl_data.get("chains", {}).get(chain_name)
                    if chain_data:
                        rows.append(self._build_chain_row(topic, chain_name, chain_data, now))
            else:
                total = tvl_data.get("total")
                if total:
                    rows.append(self._build_total_row(topic, total, now))
        return rows

    def _get_cached_tvl(self) -> dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache[0]) < self._cache_ttl_sec:
            return self._cache[1]

        data = self._fetch_tvl()
        self._cache = (now, data)
        return data

    def _fetch_tvl(self) -> dict[str, Any]:
        result: dict[str, Any] = {"total": None, "chains": {}}

        try:
            resp = requests.get(
                f"{_BASE_URL}/v2/historicalChainTvl",
                timeout=self._timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list) and len(payload) >= 2:
                latest = payload[-1]
                prev = payload[-2]
                tvl = float(latest.get("tvl", 0))
                prev_tvl = float(prev.get("tvl", 0))
                change_1d = ((tvl - prev_tvl) / prev_tvl * 100) if prev_tvl > 0 else 0
                result["total"] = {"tvl": tvl, "change_1d": change_1d}
        except (requests.RequestException, ValueError, IndexError) as exc:
            LOG.debug("DeFiLlamaCollector: total TVL fetch failed: %s", exc)

        try:
            resp = requests.get(f"{_BASE_URL}/v2/chains", timeout=self._timeout_sec)
            resp.raise_for_status()
            chains_data = resp.json()
            if isinstance(chains_data, list):
                for chain in chains_data:
                    if not isinstance(chain, dict):
                        continue
                    name = chain.get("name", "")
                    tvl = float(chain.get("tvl", 0))
                    change_1d = float(chain.get("change_1d", 0) or 0)
                    if name and tvl > 0:
                        result["chains"][name] = {"tvl": tvl, "change_1d": change_1d}
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("DeFiLlamaCollector: chains fetch failed: %s", exc)

        return result

    def _build_chain_row(self, topic: str, chain: str, data: dict, now: float) -> dict:
        tvl = data["tvl"]
        change = data["change_1d"]
        return {
            "topic": topic,
            "source": "defillama",
            "summary": f"{chain} TVL: ${tvl/1e9:.2f}B ({change:+.1f}% 24h)",
            "stance": _stance_from_tvl_change(change),
            "ts": now,
            "published_ts": now,
            "link": f"https://defillama.com/chain/{chain}",
            "extras": {
                "chain": chain,
                "tvl_usd": tvl,
                "change_1d_pct": change,
            },
        }

    def _build_total_row(self, topic: str, data: dict, now: float) -> dict:
        tvl = data["tvl"]
        change = data["change_1d"]
        return {
            "topic": topic,
            "source": "defillama",
            "summary": f"DeFi Total TVL: ${tvl/1e9:.2f}B ({change:+.1f}% 24h)",
            "stance": _stance_from_tvl_change(change),
            "ts": now,
            "published_ts": now,
            "link": "https://defillama.com/",
            "extras": {
                "chain": "all",
                "tvl_usd": tvl,
                "change_1d_pct": change,
            },
        }


def _is_defi_topic(topic: str) -> bool:
    tokens = {t for t in re.split(r"[^a-z0-9]+", topic.lower()) if t}
    return bool(tokens & _DEFI_KEYWORDS)


def _extract_chains(topic: str) -> list[str]:
    tokens = {t for t in re.split(r"[^a-z0-9]+", topic.lower()) if t}
    matched = tokens & frozenset(_CHAIN_MAP.keys())
    return list({_CHAIN_MAP[t] for t in matched})


def _stance_from_tvl_change(change: float) -> str:
    if change >= 3.0:
        return "bullish"
    if change <= -3.0:
        return "bearish"
    return "neutral"
