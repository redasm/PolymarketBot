"""Pure helpers for selecting / ranking scan candidates.

These functions decide *which* markets and events the scan loop should
process this cycle, given an optional comma-separated focus keyword list
(`ARB_MARKET_FOCUS_KEYWORDS`). They are deliberately side-effect free so
they can be unit-tested in isolation and reused by future sub-pipelines
(e.g. observation mode, paper trading, dashboards).

Why a separate module:
- The original implementations lived inline in `main_loop.py`, intermixed
  with ~3 000 lines of orchestration code, which made it impossible to
  exercise focus / ranking behaviour without booting the whole loop.
- All functions here are pure transformations of `MarketInfo` / event-like
  objects. Anything that needs IO (orderbook prefetch) takes its dependency
  as an explicit argument.
"""

from __future__ import annotations

import re
from typing import Any

from polymarket_arb.models import MarketInfo
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer

# Token aliases let callers write `btc` and still match `bitcoin` style
# slugs / questions. Kept module-private; extend here when adding a new
# canonical-form short code.
_FOCUS_ALIASES: dict[str, tuple[str, ...]] = {
    "btc": ("btc", "bitcoin"),
    "eth": ("eth", "ethereum"),
    "sol": ("sol", "solana"),
    "arb": ("arb", "arbitrum"),
}


def focus_keywords(raw: str) -> list[str]:
    """Parse a comma-separated focus list into normalized lowercase keywords."""
    return [item.strip().lower() for item in (raw or "").split(",") if item.strip()]


def market_focus_text(market: MarketInfo) -> str:
    """Concatenate every searchable field of a market into one lowercase blob."""
    parts = [
        market.question,
        market.slug,
        market.event_slug,
        getattr(market, "event_title", ""),
        getattr(market, "event_ticker", ""),
        str((market.raw or {}).get("description") or ""),
        " ".join(market.outcomes or []),
        " ".join((token.outcome or "") for token in market.tokens),
    ]
    return " ".join(part for part in parts if part).lower()


def event_focus_text(event: Any) -> str:
    parts = [getattr(event, "title", ""), getattr(event, "slug", "")]
    for market in getattr(event, "markets", []) or []:
        parts.append(getattr(market, "question", ""))
        parts.append(getattr(market, "slug", ""))
    return " ".join(part for part in parts if part).lower()


def matches_focus(text: str, keywords: list[str]) -> bool:
    """True when `text` matches at least one keyword (or no filter is given).

    Matching is token-aware: the haystack is split on non-alphanumerics so
    a 3-letter ticker like `eth` won't accidentally match inside `together`.
    Aliases declared in `_FOCUS_ALIASES` are expanded so `btc` also fires
    on `bitcoin`-style slugs.
    """
    if not keywords:
        return True
    normalized_tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", text.lower())
        if token
    ]
    for keyword in keywords:
        aliases = _FOCUS_ALIASES.get(keyword)
        if aliases is not None:
            if any(token == alias or token.startswith(f"{alias}-") for alias in aliases for token in normalized_tokens):
                return True
            continue
        if len(keyword) <= 3:
            if keyword in normalized_tokens:
                return True
            continue
        if any(token == keyword or token.startswith(keyword) for token in normalized_tokens):
            return True
    return False


def market_priority_score(market: MarketInfo) -> tuple[float, float, float]:
    """Sort key: binary > multi-outcome, then by 24h volume, then liquidity."""
    binary_boost = 1.0 if len(market.tokens) == 2 else 0.0
    return (
        binary_boost,
        float(market.volume_24h or 0.0),
        float(market.liquidity or 0.0),
    )


def event_priority_score(event: Any) -> tuple[float, float, int]:
    markets = list(getattr(event, "markets", []) or [])
    total_volume = sum(float(getattr(market, "volume_24h", 0.0) or 0.0) for market in markets)
    total_liquidity = sum(float(getattr(market, "liquidity", 0.0) or 0.0) for market in markets)
    return (
        total_volume,
        total_liquidity,
        len(markets),
    )


def select_scan_candidates(
    markets: list[MarketInfo],
    max_count: int,
    *,
    focus_keywords: list[str] | None = None,
) -> list[MarketInfo]:
    active = [
        market for market in markets
        if market.active and not market.closed and matches_focus(market_focus_text(market), focus_keywords or [])
    ]
    active.sort(key=market_priority_score, reverse=True)
    return active[:max_count]


def select_event_candidates(
    events: list[Any],
    max_count: int,
    *,
    focus_keywords: list[str] | None = None,
) -> list[Any]:
    active = [
        event for event in events
        if getattr(event, "active", True)
        and not getattr(event, "closed", False)
        and matches_focus(event_focus_text(event), focus_keywords or [])
    ]
    active.sort(key=event_priority_score, reverse=True)
    return active[:max_count]


def merge_focus_event_markets(
    markets: list[MarketInfo],
    events: list[Any],
    max_count: int,
    *,
    focus_keywords: list[str] | None = None,
) -> list[MarketInfo]:
    """Merge per-event markets into the flat market list.

    Multi-outcome events expose their child markets only via the event
    record; this helper folds them back into the flat list so downstream
    scanning sees both. Event metadata (event_id / slug / title) is lazily
    backfilled onto child markets so later code paths don't need to look
    it up.
    """
    merged: dict[str, MarketInfo] = {
        market.condition_id: market
        for market in markets
        if market.active and not market.closed
    }

    for event in events:
        for market in getattr(event, "markets", []) or []:
            if not market.active or market.closed or len(market.tokens) != 2:
                continue
            if not market.event_id:
                market.event_id = getattr(event, "event_id", "")
            if not market.event_slug:
                market.event_slug = getattr(event, "slug", "")
            if not getattr(market, "event_title", ""):
                market.event_title = getattr(event, "title", "")
            if focus_keywords and not matches_focus(market_focus_text(market), focus_keywords):
                continue
            merged.setdefault(market.condition_id, market)

    ranked = list(merged.values())
    ranked.sort(key=market_priority_score, reverse=True)
    return ranked[:max_count]


def select_ws_targets(
    markets: list[MarketInfo],
    max_count: int,
) -> list[MarketInfo]:
    """Pick the top-N binary markets to mirror over WebSocket.

    Ranked by `volume * liquidity` because both dimensions matter: high
    volume with thin liquidity gets eaten quickly, while deep books with
    no flow waste the WS subscription budget.
    """
    binary = [m for m in markets if len(m.tokens) == 2 and not m.closed]
    binary.sort(key=lambda m: m.volume_24h * m.liquidity, reverse=True)
    return binary[:max_count]


def prime_candidate_orderbooks(
    *,
    candidate_markets: list[MarketInfo],
    candidate_events: list[Any],
    ob_analyzer: OrderBookAnalyzer,
) -> dict[str, Any]:
    """Pre-fetch orderbook snapshots for every active candidate token in one batch.

    Avoids per-token round trips during the scan loop. Token IDs are
    de-duplicated while preserving insertion order so the snapshot map's
    iteration order remains deterministic for debug logs.
    """
    token_ids: list[str] = []
    for market in candidate_markets:
        if len(market.tokens) == 2 and market.active and not market.closed:
            token_ids.extend(token.token_id for token in market.tokens if token.token_id)
    for event in candidate_events:
        for market in getattr(event, "markets", []) or []:
            if market.closed or not market.active:
                continue
            token_ids.extend(token.token_id for token in market.tokens if token.token_id)
    if not token_ids:
        return {}
    return ob_analyzer.batch_get_snapshots(list(dict.fromkeys(token_ids)), delay=0.0)
