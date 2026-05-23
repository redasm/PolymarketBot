"""Collectors for research signals."""

from research_signal.collectors.base import (
    GenericHTTPJSONCollector,
    GenericRSSCollector,
    PolymarketEventCollector,
    WebSearchCollector,
)
from research_signal.collectors.coingecko import CoinGeckoCollector
from research_signal.collectors.crypto_macro import CryptoMacroCollector
from research_signal.collectors.defillama import DeFiLlamaCollector
from research_signal.collectors.econ_calendar import EconCalendarCollector
from research_signal.collectors.funding_rate import FundingRateCollector
from research_signal.collectors.polymarket_activity import PolymarketActivityCollector

__all__ = [
    "CoinGeckoCollector",
    "CryptoMacroCollector",
    "DeFiLlamaCollector",
    "EconCalendarCollector",
    "FundingRateCollector",
    "GenericHTTPJSONCollector",
    "GenericRSSCollector",
    "PolymarketActivityCollector",
    "PolymarketEventCollector",
    "WebSearchCollector",
]
