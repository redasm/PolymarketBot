"""市场扫描器：从 Gamma API 批量拉取活跃市场和事件，构建本地缓存."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import EventInfo, MarketInfo, TokenInfo

LOG = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"Accept": "application/json"})
    return _SESSION


def _parse_market(raw: dict) -> Optional[MarketInfo]:
    """将 Gamma API 返回的单个 market JSON 转为 MarketInfo."""
    condition_id = raw.get("condition_id") or raw.get("conditionId") or ""
    if not condition_id:
        return None

    tokens_raw = raw.get("tokens") or []
    tokens: list[TokenInfo] = []
    for t in tokens_raw:
        token_id = t.get("token_id") or ""
        outcome = t.get("outcome") or ""
        price = float(t.get("price") or 0)
        winner = t.get("winner")
        if winner is not None:
            winner = bool(winner)
        if token_id:
            tokens.append(TokenInfo(token_id=token_id, outcome=outcome, price=price, winner=winner))

    outcomes = raw.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            import json
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []

    outcome_prices_raw = raw.get("outcomePrices") or raw.get("outcome_prices") or []
    if isinstance(outcome_prices_raw, str):
        try:
            import json
            outcome_prices_raw = json.loads(outcome_prices_raw)
        except Exception:
            outcome_prices_raw = []
    outcome_prices = [float(p) for p in outcome_prices_raw if p is not None]

    neg_risk = raw.get("neg_risk") or raw.get("negRisk") or False
    if isinstance(neg_risk, str):
        neg_risk = neg_risk.lower() in ("true", "1")

    return MarketInfo(
        condition_id=condition_id,
        question=raw.get("question") or raw.get("title") or "",
        slug=raw.get("market_slug") or raw.get("slug") or "",
        tokens=tokens,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        volume_24h=float(raw.get("volume_num_24hr") or raw.get("volume24hr") or 0),
        liquidity=float(raw.get("liquidity") or 0),
        event_id=raw.get("event_id") or raw.get("eventId") or "",
        event_slug=raw.get("event_slug") or "",
        outcomes=outcomes,
        outcome_prices=outcome_prices,
        neg_risk=bool(neg_risk),
        end_date=raw.get("end_date_iso") or raw.get("endDate") or "",
        raw=raw,
    )


def _parse_event(raw: dict) -> Optional[EventInfo]:
    """将 Gamma API 返回的单个 event JSON 转为 EventInfo."""
    event_id = str(raw.get("id") or "")
    if not event_id:
        return None

    markets_raw = raw.get("markets") or []
    markets: list[MarketInfo] = []
    for m in markets_raw:
        parsed = _parse_market(m)
        if parsed:
            markets.append(parsed)

    return EventInfo(
        event_id=event_id,
        slug=raw.get("slug") or "",
        title=raw.get("title") or "",
        markets=markets,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
    )


class MarketScanner:
    """从 Gamma API 扫描市场和事件."""

    def __init__(self, config: ArbConfig):
        self._config = config
        self._gamma_host = config.gamma_host.rstrip("/")
        self._market_cache: dict[str, MarketInfo] = {}
        self._event_cache: dict[str, EventInfo] = {}
        self._last_fetch_ts: float = 0.0

    def fetch_active_markets(
        self,
        *,
        limit: int = 0,
        min_liquidity: float = 0,
        min_volume_24h: float = 0,
    ) -> list[MarketInfo]:
        """拉取活跃且未关闭的市场列表."""
        if limit <= 0:
            limit = self._config.market_fetch_limit

        all_markets: list[MarketInfo] = []
        offset = 0
        session = _get_session()

        while True:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": min(limit, 100),
                "offset": offset,
                "order": "volume_24hr",
                "ascending": "false",
            }
            try:
                resp = session.get(f"{self._gamma_host}/markets", params=params, timeout=15)
                resp.raise_for_status()
                rows = resp.json()
            except Exception as e:
                LOG.error("Gamma /markets 请求失败 (offset=%d): %s", offset, e)
                break

            if not rows:
                break

            for raw in rows:
                market = _parse_market(raw)
                if market is None:
                    continue
                if min_liquidity > 0 and market.liquidity < min_liquidity:
                    continue
                if min_volume_24h > 0 and market.volume_24h < min_volume_24h:
                    continue
                all_markets.append(market)
                self._market_cache[market.condition_id] = market

            offset += len(rows)
            if len(rows) < min(limit, 100) or len(all_markets) >= limit:
                break
            time.sleep(0.2)

        self._last_fetch_ts = time.time()
        LOG.info("拉取到 %d 个活跃市场（筛选后）", len(all_markets))
        return all_markets

    def fetch_active_events(self, *, limit: int = 50) -> list[EventInfo]:
        """拉取活跃事件（含嵌套的 markets），用于多结果套利检测."""
        all_events: list[EventInfo] = []
        offset = 0
        session = _get_session()

        while True:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": min(limit, 100),
                "offset": offset,
                "order": "volume_24hr",
                "ascending": "false",
            }
            try:
                resp = session.get(f"{self._gamma_host}/events", params=params, timeout=15)
                resp.raise_for_status()
                rows = resp.json()
            except Exception as e:
                LOG.error("Gamma /events 请求失败 (offset=%d): %s", offset, e)
                break

            if not rows:
                break

            for raw in rows:
                event = _parse_event(raw)
                if event is None:
                    continue
                if len(event.markets) >= 2:
                    all_events.append(event)
                    self._event_cache[event.event_id] = event

            offset += len(rows)
            if len(rows) < min(limit, 100) or len(all_events) >= limit:
                break
            time.sleep(0.2)

        LOG.info("拉取到 %d 个多市场事件", len(all_events))
        return all_events

    def get_cached_market(self, condition_id: str) -> Optional[MarketInfo]:
        return self._market_cache.get(condition_id)

    def get_cached_event(self, event_id: str) -> Optional[EventInfo]:
        return self._event_cache.get(event_id)
