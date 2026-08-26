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


def is_updown_market(market: MarketInfo) -> bool:
    """True for short-horizon spot-anchored UP/DOWN markets (e.g. btc-updown-15m).

    These are the only markets `fair_value_model.compute_fair_updown` can price,
    because ref_px is fixed at the window start and the question is purely
    directional ("will price be higher at window end?").

    Detection is deliberately two-pronged and conservative:
    - Primary: the slug contains an `updown` token (Polymarket uses
      `btc-updown-15m-{slot}` style slugs). Slug is far more stable than the
      free-text question, which varies across event families.
    - Secondary: the two token outcomes are exactly {up, down}. This catches
      any UP/DOWN market whose slug convention differs, without false-firing on
      ordinary Yes/No binaries.
    """
    slug = (market.slug or "").lower()
    event_slug = (getattr(market, "event_slug", "") or "").lower()
    if "updown" in slug or "up-down" in slug or "updown" in event_slug or "up-down" in event_slug:
        return True
    outcomes = {
        (token.outcome or "").strip().lower()
        for token in market.tokens
        if (token.outcome or "").strip()
    }
    if not outcomes:
        outcomes = {str(o).strip().lower() for o in (market.outcomes or []) if str(o).strip()}
    return outcomes == {"up", "down"}


def is_weather_market(market: MarketInfo) -> bool:
    """Conservative text classification for weather/temperature contracts."""
    text = market_focus_text(market)
    return bool(re.search(r"\b(?:weather|temperature|temp|degrees?)\b|°\s*f\b", text))


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


def market_priority_score(
    market: MarketInfo,
    updown_boost: float = 0.0,
    weather_boost: float = 0.0,
) -> tuple[float, float, float, float]:
    """Sort key: UPDOWN boost > binary > multi-outcome, then 24h volume, then liquidity.

    `updown_boost` (>0 only when `T2_UPDOWN_ENABLED`) lifts short-horizon
    spot-anchored UP/DOWN markets above high-volume long-horizon markets so
    they aren't squeezed out of the hot pool / WS budget. When 0 the leading
    term is constant and the ordering is identical to the pre-UPDOWN behaviour.
    """
    updown_term = updown_boost if (updown_boost > 0.0 and is_updown_market(market)) else 0.0
    weather_term = weather_boost if (weather_boost > 0.0 and is_weather_market(market)) else 0.0
    binary_boost = 1.0 if len(market.tokens) == 2 else 0.0
    return (
        updown_term + weather_term,
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
    updown_boost: float = 0.0,
    weather_boost: float = 0.0,
) -> list[MarketInfo]:
    active = [
        market for market in markets
        if market.active and not market.closed and matches_focus(market_focus_text(market), focus_keywords or [])
    ]
    active.sort(key=lambda m: market_priority_score(m, updown_boost, weather_boost), reverse=True)
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
    updown_boost: float = 0.0,
    weather_boost: float = 0.0,
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
    ranked.sort(key=lambda m: market_priority_score(m, updown_boost, weather_boost), reverse=True)
    return ranked[:max_count]


def select_ws_targets(
    markets: list[MarketInfo],
    max_count: int,
    *,
    updown_boost: float = 0.0,
    weather_boost: float = 0.0,
) -> list[MarketInfo]:
    """Pick the top-N binary markets to mirror over WebSocket.

    Ranked by `volume * liquidity` because both dimensions matter: high
    volume with thin liquidity gets eaten quickly, while deep books with
    no flow waste the WS subscription budget.

    When `updown_boost > 0` (T2_UPDOWN_ENABLED), short-horizon UP/DOWN
    markets are placed ahead of the volume*liquidity ranking so they always
    secure a WS slot — their per-market 24h volume is small and they would
    otherwise never make the WS budget despite being the T2 substrate.
    """
    binary = [m for m in markets if len(m.tokens) == 2 and not m.closed]
    use_updown = updown_boost > 0.0
    binary.sort(
        key=lambda m: (
            (1.0 if (use_updown and is_updown_market(m)) else 0.0)
            + (weather_boost if (weather_boost > 0.0 and is_weather_market(m)) else 0.0),
            m.volume_24h * m.liquidity,
        ),
        reverse=True,
    )
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
