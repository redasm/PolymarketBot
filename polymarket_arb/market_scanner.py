"""市场扫描器：从 Gamma API 批量拉取活跃市场和事件，构建本地缓存."""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, Optional

import requests

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import EventInfo, MarketInfo, ResearchSignal, ResearchSignalReport, TokenInfo

if TYPE_CHECKING:
    # research_signal is an optional sub-package; main_loop.py guards its
    # import behind ArbConfig.research_signal_enabled. Keep this module
    # importable even when the sub-package is missing or broken so the bot
    # still boots in the default (disabled) configuration.
    from research_signal.service import ResearchSignalService

LOG = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None
_MOJIBAKE_MARKERS = ("â", "Ã", "Â", "\x80", "\x82", "\x84", "\x85", "\x91", "\x92", "\x93", "\x94", "\x96", "\x97")


class APIResponseValidationError(ValueError):
    """远端 API 返回结构与预期不符."""


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"Accept": "application/json"})
    return _SESSION


def _load_json_payload(resp: requests.Response, *, expected_type: type, endpoint: str) -> Any:
    try:
        payload = resp.json()
    except ValueError as e:
        raise APIResponseValidationError(f"{endpoint} 返回了无法解析的 JSON") from e
    if not isinstance(payload, expected_type):
        raise APIResponseValidationError(
            f"{endpoint} 返回类型异常: expected={expected_type.__name__}, got={type(payload).__name__}"
        )
    return payload


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""

    fixed = text
    if any(marker in fixed for marker in _MOJIBAKE_MARKERS):
        try:
            repaired = fixed.encode("latin-1").decode("utf-8")
            if repaired:
                fixed = repaired
        except UnicodeError:
            LOG.debug("文本修复失败，保留原始内容: value=%r", text[:120])

    fixed = fixed.replace("\ufffd", "")
    fixed = re.sub(r"\s+", " ", fixed).strip()
    return fixed


def _parse_market(raw: dict) -> Optional[MarketInfo]:
    """将 Gamma API 返回的单个 market JSON 转为 MarketInfo."""
    condition_id = raw.get("condition_id") or raw.get("conditionId") or ""
    if not condition_id:
        return None

    events_raw = raw.get("events") or []
    primary_event = events_raw[0] if isinstance(events_raw, list) and events_raw else {}
    if not isinstance(primary_event, dict):
        primary_event = {}

    event_id = raw.get("event_id") or raw.get("eventId") or primary_event.get("id") or ""
    event_slug = _normalize_text(
        raw.get("event_slug")
        or raw.get("eventSlug")
        or primary_event.get("slug")
        or ""
    )
    event_title = _normalize_text(
        raw.get("event_title")
        or raw.get("eventTitle")
        or primary_event.get("title")
        or ""
    )
    event_ticker = _normalize_text(
        raw.get("event_ticker")
        or raw.get("eventTicker")
        or primary_event.get("ticker")
        or ""
    )

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
    outcome_prices = [
        _coerce_float(p)
        for p in outcome_prices_raw
        if p is not None
    ]

    tokens_raw = raw.get("tokens") or []
    tokens: list[TokenInfo] = []
    for t in tokens_raw:
        if not isinstance(t, dict):
            continue
        token_id = t.get("token_id") or t.get("tokenId") or t.get("clobTokenId") or ""
        outcome = _normalize_text(t.get("outcome") or "")
        price = _coerce_float(t.get("price"))
        winner = t.get("winner")
        if winner is not None:
            winner = bool(winner)
        if token_id:
            tokens.append(TokenInfo(token_id=token_id, outcome=outcome, price=price, winner=winner))

    if not tokens:
        clob_token_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids") or []
        if isinstance(clob_token_ids, str):
            try:
                import json
                clob_token_ids = json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = [part.strip() for part in clob_token_ids.split(",") if part.strip()]
        for idx, token_id in enumerate(clob_token_ids):
            outcome = _normalize_text(outcomes[idx]) if idx < len(outcomes) else f"Outcome {idx + 1}"
            price = outcome_prices[idx] if idx < len(outcome_prices) else 0.0
            if token_id:
                tokens.append(TokenInfo(token_id=str(token_id), outcome=str(outcome), price=_coerce_float(price)))

    neg_risk = raw.get("neg_risk") or raw.get("negRisk") or False
    if isinstance(neg_risk, str):
        neg_risk = neg_risk.lower() in ("true", "1")

    return MarketInfo(
        condition_id=condition_id,
        question=_normalize_text(raw.get("question") or raw.get("title") or ""),
        slug=_normalize_text(raw.get("market_slug") or raw.get("slug") or ""),
        tokens=tokens,
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        volume_24h=_coerce_float(raw.get("volume_num_24hr") or raw.get("volume24hr")),
        liquidity=_coerce_float(raw.get("liquidity")),
        event_id=str(event_id),
        event_slug=event_slug,
        event_title=event_title,
        event_ticker=event_ticker,
        outcomes=[_normalize_text(outcome) for outcome in outcomes],
        outcome_prices=outcome_prices,
        neg_risk=bool(neg_risk),
        end_date=_normalize_text(raw.get("end_date_iso") or raw.get("endDate") or ""),
        raw=raw,
    )


def _safe_parse_market(raw: Any, *, context: str) -> Optional[MarketInfo]:
    if not isinstance(raw, dict):
        LOG.warning("%s 跳过非对象 market 行: type=%s", context, type(raw).__name__)
        return None
    try:
        return _parse_market(raw)
    except Exception as exc:
        condition_id = raw.get("condition_id") or raw.get("conditionId") or ""
        LOG.warning("%s 跳过无法解析的 market 行 condition=%s: %s", context, condition_id, exc)
        return None


def _parse_event(raw: dict) -> Optional[EventInfo]:
    """将 Gamma API 返回的单个 event JSON 转为 EventInfo."""
    event_id = str(raw.get("id") or "")
    if not event_id:
        return None

    event_slug = _normalize_text(raw.get("slug") or "")
    event_title = _normalize_text(raw.get("title") or "")

    markets_raw = raw.get("markets") or []
    markets: list[MarketInfo] = []
    for m in markets_raw:
        parsed = _safe_parse_market(m, context=f"Gamma /events event={event_id}")
        if parsed:
            if not parsed.event_id:
                parsed.event_id = event_id
            if not parsed.event_slug:
                parsed.event_slug = event_slug
            if not parsed.event_title:
                parsed.event_title = event_title
            markets.append(parsed)

    return EventInfo(
        event_id=event_id,
        slug=event_slug,
        title=event_title,
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

        self._market_cache.clear()
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
            request_id = f"markets-{offset}-{int(time.time() * 1000)}"
            try:
                resp = session.get(f"{self._gamma_host}/markets", params=params, timeout=15)
                resp.raise_for_status()
                rows = _load_json_payload(resp, expected_type=list, endpoint="Gamma /markets")
            except (requests.RequestException, APIResponseValidationError) as e:
                LOG.error("[cid=%s] Gamma /markets 请求失败 (offset=%d): %s", request_id, offset, e)
                break

            if not rows:
                break

            for raw in rows:
                market = _safe_parse_market(raw, context=f"Gamma /markets offset={offset}")
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
        self._event_cache.clear()
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
            request_id = f"events-{offset}-{int(time.time() * 1000)}"
            try:
                resp = session.get(f"{self._gamma_host}/events", params=params, timeout=15)
                resp.raise_for_status()
                rows = _load_json_payload(resp, expected_type=list, endpoint="Gamma /events")
            except (requests.RequestException, APIResponseValidationError) as e:
                LOG.error("[cid=%s] Gamma /events 请求失败 (offset=%d): %s", request_id, offset, e)
                break

            if not rows:
                break

            for raw in rows:
                if not isinstance(raw, dict):
                    LOG.warning("Gamma /events 跳过非对象 event 行: type=%s", type(raw).__name__)
                    continue
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

    def enrich_markets_with_research(
        self,
        markets: list[MarketInfo],
        signal_service: "ResearchSignalService | None",
        *,
        window_sec: int = 86400,
        signals: list[ResearchSignal] | None = None,
        report: ResearchSignalReport | None = None,
    ) -> list[MarketInfo]:
        """用研究信号对市场做只读 enrichment."""
        if signal_service is None or not markets:
            return markets
        if report is not None:
            signals = report.signals
        if signals is None:
            signals = signal_service.get_signals(markets, window_sec)
        return signal_service.attach_to_markets(markets, signals)
