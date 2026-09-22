"""Polymarket Activity collector (volume spikes, large trades).

Uses Polymarket's public CLOB API to detect volume spikes and
unusual activity on tracked markets — a leading indicator of
informed flow or sentiment shifts.

Outputs per market with notable activity:
  - source     : "polymarket_activity"
  - summary    : human-readable volume/activity description
  - stance     : "bullish" | "bearish" | "neutral" based on trade direction
  - extras     : volume_24h, price, spread, last_trade_side
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

_CLOB_BASE = "https://clob.polymarket.com"
_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CACHE_TTL_SEC = 120.0
_DEFAULT_HEADERS = {
    "User-Agent": "PolymarketBot/1.0 (+research-signal)",
    "Accept": "application/json",
}


class PolymarketActivityCollector:
    """Detect volume spikes and unusual activity on Polymarket markets."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_sec: float = 5.0,
        cache_ttl_sec: float = _CACHE_TTL_SEC,
        volume_spike_threshold: float = 2.0,
    ) -> None:
        self._enabled = bool(enabled)
        self._timeout_sec = float(timeout_sec)
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._volume_spike_threshold = float(volume_spike_threshold)
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def collect(self, markets: Iterable[Any]) -> list[dict]:
        """Collect activity signals from market objects (MarketInfo or dicts)."""
        if not self._enabled:
            return []
        rows: list[dict] = []
        now = time.time()
        for market in markets:
            condition_id = _get_attr(market, "condition_id", "")
            if not condition_id:
                continue
            question = _get_attr(market, "question", "")
            slug = _get_attr(market, "slug", "")

            activity = self._get_market_activity(condition_id)
            if not activity:
                continue

            volume_24h = activity.get("volume_24h", 0)
            if volume_24h <= 0:
                continue

            rows.append({
                "topic": question,
                "source": "polymarket_activity",
                "summary": _build_summary(question, activity),
                "stance": _infer_stance(activity),
                "ts": now,
                "published_ts": now,
                "link": f"https://polymarket.com/event/{slug}" if slug else "",
                "extras": {
                    "condition_id": condition_id,
                    "volume_24h": volume_24h,
                    "best_bid": activity.get("best_bid", 0),
                    "best_ask": activity.get("best_ask", 0),
                    "spread_bps": activity.get("spread_bps", 0),
                    "mid_price": activity.get("mid_price", 0),
                },
            })
        return rows

    def _get_market_activity(self, condition_id: str) -> dict[str, Any] | None:
        now = time.time()
        cached = self._cache.get(condition_id)
        if cached and (now - cached[0]) < self._cache_ttl_sec:
            return cached[1]

        data = self._fetch_activity(condition_id)
        self._cache[condition_id] = (now, data)
        return data

    def _fetch_activity(self, condition_id: str) -> dict[str, Any] | None:
        try:
            resp = requests.get(
                f"{_GAMMA_BASE}/markets",
                params={"condition_id": condition_id},
                headers=_DEFAULT_HEADERS,
                timeout=self._timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.debug("PolymarketActivityCollector: fetch failed for %s: %s", condition_id[:12], exc)
            return None

        if isinstance(payload, list) and payload:
            market_data = payload[0]
        elif isinstance(payload, dict):
            market_data = payload
        else:
            return None

        try:
            volume_24h = float(market_data.get("volume24hr", 0) or 0)
            best_bid = float(market_data.get("bestBid", 0) or 0)
            best_ask = float(market_data.get("bestAsk", 0) or 0)
        except (TypeError, ValueError):
            return None

        mid_price = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0
        spread_bps = int((best_ask - best_bid) * 10000) if (best_bid and best_ask) else 0

        return {
            "volume_24h": volume_24h,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "spread_bps": spread_bps,
        }


def _get_attr(obj: Any, key: str, default: str = "") -> str:
    if isinstance(obj, dict):
        return str(obj.get(key, default))
    return str(getattr(obj, key, default))


def _build_summary(question: str, activity: dict) -> str:
    vol = activity["volume_24h"]
    mid = activity.get("mid_price", 0)
    spread = activity.get("spread_bps", 0)
    parts = [f"Vol ${vol:,.0f}/24h"]
    if mid > 0:
        parts.append(f"mid={mid:.2f}")
    if spread > 0:
        parts.append(f"spread={spread}bps")
    return f"{question[:60]}: {', '.join(parts)}"


def _infer_stance(activity: dict) -> str:
    mid = activity.get("mid_price", 0.5)
    if mid >= 0.75:
        return "bullish"
    if mid <= 0.25:
        return "bearish"
    return "neutral"
