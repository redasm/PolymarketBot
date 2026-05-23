"""Perpetual funding rate collector (Binance + Bybit free APIs).

Funding rates are a strong signal for crypto market sentiment:
- Positive funding = longs pay shorts = market is overleveraged long
- Negative funding = shorts pay longs = market is overleveraged short

Outputs per detected crypto topic:
  - source     : "funding_rate"
  - summary    : human-readable funding rate + annualized
  - stance     : "bullish" | "bearish" | "neutral"
  - extras     : rate, annualized_pct, exchange, symbol
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

_CACHE_TTL_SEC = 300.0

_SYMBOL_MAP: dict[str, str] = {
    "btc": "BTCUSDT",
    "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT",
    "ethereum": "ETHUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",
    "xrp": "XRPUSDT",
    "ripple": "XRPUSDT",
    "doge": "DOGEUSDT",
    "dogecoin": "DOGEUSDT",
    "ada": "ADAUSDT",
    "cardano": "ADAUSDT",
    "avax": "AVAXUSDT",
    "avalanche": "AVAXUSDT",
    "matic": "MATICUSDT",
    "polygon": "MATICUSDT",
    "link": "LINKUSDT",
    "chainlink": "LINKUSDT",
}

_CRYPTO_TOKENS = frozenset(_SYMBOL_MAP.keys())


class FundingRateCollector:
    """Fetch perpetual funding rates from Binance and Bybit."""

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
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def collect(self, topics: Iterable[str]) -> list[dict]:
        if not self._enabled:
            return []
        rows: list[dict] = []
        seen_symbols: set[str] = set()
        for topic in topics:
            if not topic or not topic.strip():
                continue
            symbols = _extract_symbols(topic)
            for symbol in symbols:
                if symbol in seen_symbols:
                    continue
                seen_symbols.add(symbol)
                data = self._get_funding_rate(symbol)
                if data:
                    rows.append(self._build_row(topic, symbol, data))
        return rows

    def _get_funding_rate(self, symbol: str) -> dict[str, Any] | None:
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and (now - cached[0]) < self._cache_ttl_sec:
            return cached[1]

        data = self._fetch_binance(symbol)
        if data is None:
            data = self._fetch_bybit(symbol)

        self._cache[symbol] = (now, data)
        return data

    def _fetch_binance(self, symbol: str) -> dict[str, Any] | None:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        try:
            resp = requests.get(
                url, params={"symbol": symbol}, timeout=self._timeout_sec
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("FundingRateCollector: Binance fetch failed for %s: %s", symbol, exc)
            return None

        if not isinstance(payload, dict):
            return None
        try:
            rate = float(payload.get("lastFundingRate", 0))
        except (TypeError, ValueError):
            return None
        return {
            "rate": rate,
            "symbol": symbol,
            "exchange": "binance",
            "mark_price": float(payload.get("markPrice", 0)),
        }

    def _fetch_bybit(self, symbol: str) -> dict[str, Any] | None:
        url = "https://api.bybit.com/v5/market/tickers"
        try:
            resp = requests.get(
                url,
                params={"category": "linear", "symbol": symbol},
                timeout=self._timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("FundingRateCollector: Bybit fetch failed for %s: %s", symbol, exc)
            return None

        result = payload.get("result", {})
        items = result.get("list", [])
        if not items:
            return None
        item = items[0]
        try:
            rate = float(item.get("fundingRate", 0))
        except (TypeError, ValueError):
            return None
        return {
            "rate": rate,
            "symbol": symbol,
            "exchange": "bybit",
            "mark_price": float(item.get("markPrice", 0)),
        }

    def _build_row(self, topic: str, symbol: str, data: dict) -> dict:
        rate = data["rate"]
        annualized = rate * 3 * 365 * 100
        now = time.time()

        return {
            "topic": topic,
            "source": "funding_rate",
            "summary": f"{symbol} funding: {rate*100:.4f}% (≈{annualized:.1f}% ann.)",
            "stance": _stance_from_funding(rate),
            "ts": now,
            "published_ts": now,
            "link": f"https://www.binance.com/en/futures/{symbol}",
            "extras": {
                "symbol": symbol,
                "rate": rate,
                "annualized_pct": annualized,
                "exchange": data["exchange"],
                "mark_price": data.get("mark_price", 0),
            },
        }


def _extract_symbols(topic: str) -> list[str]:
    tokens = {t for t in re.split(r"[^a-z0-9]+", topic.lower()) if t}
    matched = tokens & _CRYPTO_TOKENS
    symbols = list({_SYMBOL_MAP[t] for t in matched})
    return sorted(symbols)


def _stance_from_funding(rate: float) -> str:
    """Contrarian read: high positive funding = overleveraged longs = bearish.
    High negative funding = overleveraged shorts = bullish."""
    if rate >= 0.001:
        return "bearish"
    if rate <= -0.001:
        return "bullish"
    return "neutral"
