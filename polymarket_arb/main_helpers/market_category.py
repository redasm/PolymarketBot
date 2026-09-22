"""Heuristic market-category classifier.

Polymarket markets don't expose a single canonical category field
(`MarketInfo.raw` may contain tags but they are inconsistent across
event types), so we classify from the question/slug/event title text.

The categories track Becker 2025's empirical maker-taker gaps:

    Finance       0.17 pp  →  near-efficient, avoid taking
    Politics      1.02 pp
    Sports        2.23 pp
    Entertainment 4.79 pp
    World Events  7.32 pp  →  most extractable as a maker

This is a coarse classifier. False positives are tolerated: a
finance-cluster question wrongly tagged as "world_events" only relaxes
the long-horizon gate slightly; we are not making routing decisions
that fail catastrophically on misclassification.

Pure / stateless / order doesn't matter beyond category iteration —
keep it that way so the function stays cheap to call once per cycle.
"""

from __future__ import annotations

import re
from typing import Iterable

from polymarket_arb.models import MarketInfo

# Category keyword bundles. Match is token-aware (re.split on non-alnum)
# so e.g. "ethereum" doesn't match "ether-something-else" or "together".
# Order of dictionary keys defines tie-breaking: crypto wins over finance
# wins over politics, etc. — matches the article's gap ranking.
_CATEGORY_KEYWORDS: dict[str, frozenset[str]] = {
    # Crypto is broken out from Finance because keyword filtering on the
    # bot side (ARB_MARKET_FOCUS_KEYWORDS=btc,bitcoin,...) targets these
    # explicitly. Behaviourally crypto inherits Finance's near-efficiency.
    "crypto": frozenset({
        "btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp",
        "doge", "ada", "cardano", "ltc", "litecoin", "shib", "shiba",
        "crypto", "blockchain", "defi", "nft", "stablecoin", "usdc",
        "usdt", "binance", "coinbase", "kraken", "saylor", "microstrategy",
    }),
    "finance": frozenset({
        "stocks", "stock", "sp500", "spx", "nasdaq", "dow", "djia",
        "gdp", "cpi", "inflation", "recession", "rate", "rates",
        "fed", "fomc", "powell", "treasury", "bond", "yield",
        "bank", "banks", "tesla", "apple", "nvidia", "openai",
        "valuation", "ipo", "earnings",
    }),
    "politics": frozenset({
        "election", "elections", "president", "presidential", "congress",
        "senate", "senator", "house", "governor", "primary", "primaries",
        "vote", "voting", "ballot", "trump", "biden", "harris", "desantis",
        "kamala", "republican", "democrat", "democratic", "gop",
        "speaker", "impeachment", "nominee", "midterm",
    }),
    "world_events": frozenset({
        "war", "ceasefire", "truce", "treaty", "summit", "invade",
        "invasion", "attack", "missile", "strike", "russia", "ukraine",
        "israel", "iran", "gaza", "hamas", "china", "taiwan", "nato",
        "putin", "zelensky", "netanyahu", "xi", "kim", "north", "korea",
        "nuclear", "diplomat", "embassy", "coup",
    }),
    "sports": frozenset({
        "nba", "nfl", "mlb", "nhl", "fifa", "uefa", "ufc", "wnba",
        "lakers", "celtics", "warriors", "cowboys", "patriots",
        "soccer", "football", "basketball", "baseball", "hockey",
        "golf", "tennis", "boxing", "f1", "formula", "olympics",
        "worldcup", "superbowl", "championship", "playoff", "playoffs",
    }),
    "entertainment": frozenset({
        "oscar", "oscars", "grammy", "grammys", "emmy", "emmys",
        "tony", "tonys", "billboard", "song", "album", "single",
        "movie", "film", "netflix", "disney", "hbo", "youtube",
        "spotify", "taylor", "swift", "beyonce", "kanye", "drake",
        "ariana", "rihanna", "tvshow", "boxoffice", "gta", "rockstar",
    }),
}

# Fallback used when no keyword matches.
_FALLBACK_CATEGORY = "other"


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def _market_text(market: MarketInfo) -> str:
    parts: Iterable[str] = (
        market.question,
        market.slug,
        market.event_slug,
        market.event_title or "",
        market.event_ticker or "",
    )
    return " ".join(p for p in parts if p)


def classify_market_category(market: MarketInfo) -> str:
    """Return one of: crypto, finance, politics, world_events, sports, entertainment, other.

    Falls back to ``"other"`` when nothing matches. Tie-breaks by
    ``_CATEGORY_KEYWORDS`` insertion order (crypto wins over finance,
    finance over politics, etc.). The bot uses this only to *adjust
    thresholds*, never to gate execution on its own — so a few mis-tags
    won't lose money, they just relax / tighten an edge bar by 1 tier.
    """
    tokens = set(_tokenize(_market_text(market)))
    if not tokens:
        return _FALLBACK_CATEGORY
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if tokens & keywords:
            return category
    return _FALLBACK_CATEGORY


# Maker-taker gap (pp) per category, from Becker 2025 Table 3. Used as
# a numeric scoring input by tier-routing logic; keep in sync with the
# categories above.
CATEGORY_MAKER_TAKER_GAP_PP: dict[str, float] = {
    "crypto": 0.17,        # inherits Finance
    "finance": 0.17,
    "politics": 1.02,
    "sports": 2.23,
    "entertainment": 4.79,
    "world_events": 7.32,
    "other": 1.50,         # midpoint default for unclassified
}


def is_near_efficient(category: str) -> bool:
    """Markets where the maker-taker game is near-zero. Taker strategies
    here need a much bigger edge to overcome fees + adverse selection."""
    return CATEGORY_MAKER_TAKER_GAP_PP.get(category, 1.5) < 0.5


def is_high_gap(category: str) -> bool:
    """Markets where maker captures meaningful structural edge. Makers
    should be deployed preferentially here."""
    return CATEGORY_MAKER_TAKER_GAP_PP.get(category, 0.0) >= 3.0
