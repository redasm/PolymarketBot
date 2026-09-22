"""Deterministic weather-market pricing and T2 signal generation.

The weather repositories reviewed for this project all use the same useful
pattern: parse the contract into a city/date/threshold, obtain an ensemble
forecast, and only trade when the probability gap survives spread, fees and
confidence filters.  This module keeps that path small and synchronous so it
can be called from the existing scan loop.  The provider is injectable, which
keeps tests offline and allows a cached NWS/ECMWF provider to be substituted.
"""

from __future__ import annotations

import logging
import re
import statistics
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

import requests

from polymarket_arb.config import ArbConfig
from polymarket_arb.main_helpers.signal_helpers import spread_bps_from_snapshot
from polymarket_arb.models import MarketInfo
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier

LOG = logging.getLogger("main_loop")


CITY_CONFIG: dict[str, dict[str, Any]] = {
    "nyc": {"name": "New York City", "lat": 40.7128, "lon": -74.0060},
    "chicago": {"name": "Chicago", "lat": 41.8781, "lon": -87.6298},
    "miami": {"name": "Miami", "lat": 25.7617, "lon": -80.1918},
    "los_angeles": {"name": "Los Angeles", "lat": 34.0522, "lon": -118.2437},
    "denver": {"name": "Denver", "lat": 39.7392, "lon": -104.9903},
    "austin": {"name": "Austin", "lat": 30.2672, "lon": -97.7431},
}
CITY_ALIASES = {
    "new york city": "nyc", "new york": "nyc", "nyc": "nyc",
    "chicago": "chicago", "miami": "miami", "los angeles": "los_angeles",
    "la": "los_angeles", "denver": "denver", "austin": "austin",
}
_MONTHS = {
    name: idx for idx, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"), 1
    )
}
_MONTHS.update({name[:3]: idx for name, idx in list(_MONTHS.items()) if len(name) > 3})


@dataclass(frozen=True)
class WeatherMarketSpec:
    city_key: str
    city_name: str
    target_date: date
    threshold_f: float
    metric: str
    direction: str
    upper_threshold_f: float | None = None


@dataclass(frozen=True)
class WeatherEstimate:
    probability: float
    confidence: float
    mean_f: float
    std_f: float
    members: int
    source: str
    fetched_at: float


class WeatherProvider(Protocol):
    def estimate(self, spec: WeatherMarketSpec) -> WeatherEstimate | None: ...


def parse_weather_market(market: MarketInfo, *, today: date | None = None) -> WeatherMarketSpec | None:
    """Parse common Polymarket weather titles; return None for other markets."""
    text = " ".join(
        part for part in (market.question, market.slug, market.event_title, market.event_slug)
        if part
    ).lower()
    if re.search(r"\b(?:weather|temperature|temp|degrees?)\b|°\s*f\b", text) is None:
        return None
    # Match aliases as words.  A plain substring check makes the ``la`` alias
    # accidentally classify Dallas/Atlanta/Milan as Los Angeles.
    city_key = next(
        (key for alias, key in sorted(CITY_ALIASES.items(), key=lambda item: -len(item[0]))
         if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text)),
        None,
    )
    if city_key is None:
        return None
    range_match = re.search(
        r"(?:between\s+)?(-?\d+(?:\.\d+)?)\s*°?\s*f?\s*(?:-|to|and)\s*"
        r"(-?\d+(?:\.\d+)?)\s*°?\s*f(?:ahrenheit)?\b",
        text,
    )
    upper_threshold: float | None = None
    if range_match is not None:
        threshold = float(range_match.group(1))
        upper_threshold = float(range_match.group(2))
        if upper_threshold <= threshold:
            return None
    else:
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*f(?:ahrenheit)?\b", text)
        if match is None:
            match = re.search(r"(-?\d+(?:\.\d+)?)\s*degrees?\b", text)
        if match is None:
            return None
        threshold = float(match.group(1))
    metric = "low" if re.search(r"\blow\b|minimum|min", text) else "high"
    direction = "range" if upper_threshold is not None else (
        "below" if any(word in text for word in ("below", "under", "less than", "at most")) else "above"
    )
    target = _parse_target_date(text, today=today or date.today())
    if target is None or target < (today or date.today()) or target > (today or date.today()) + timedelta(days=7):
        return None
    return WeatherMarketSpec(city_key, CITY_CONFIG[city_key]["name"], target, threshold, metric, direction, upper_threshold)


def _parse_target_date(text: str, *, today: date) -> date | None:
    pattern = r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\s+(\d{1,2})(?:,?\s*(\d{4}))?\b"
    for match in re.finditer(pattern, text):
        month = _MONTHS[match.group(1)]
        year = int(match.group(3) or today.year)
        try:
            result = date(year, month, int(match.group(2)))
        except ValueError:
            continue
        # Titles without a year can refer to the next occurrence of a month.
        if not match.group(3) and result < today:
            try:
                result = date(year + 1, month, int(match.group(2)))
            except ValueError:
                continue
        return result
    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", text)
    if match:
        try:
            result = date(int(match.group(3) or today.year), int(match.group(1)), int(match.group(2)))
            if not match.group(3) and result < today:
                result = date(today.year + 1, result.month, result.day)
            return result
        except ValueError:
            return None
    return None


class OpenMeteoEnsembleProvider:
    """Cached Open-Meteo GFS ensemble provider (free, no API key)."""

    def __init__(self, *, ttl_sec: float = 900.0, timeout_sec: float = 10.0, session: requests.Session | None = None):
        self.ttl_sec = max(30.0, float(ttl_sec))
        self.timeout_sec = max(1.0, float(timeout_sec))
        self._session = session or requests.Session()
        self._cache: dict[tuple[str, date], tuple[float, WeatherEstimate]] = {}

    def estimate(self, spec: WeatherMarketSpec) -> WeatherEstimate | None:
        key = (spec.city_key, spec.target_date)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.ttl_sec:
            return cached[1]
        city = CITY_CONFIG.get(spec.city_key)
        if city is None:
            return None
        try:
            response = self._session.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params={
                    "latitude": city["lat"], "longitude": city["lon"],
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "temperature_unit": "fahrenheit", "start_date": spec.target_date.isoformat(),
                    "end_date": spec.target_date.isoformat(), "models": "gfs_seamless",
                },
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
            daily = response.json().get("daily", {})
            prefix = "temperature_2m_min" if spec.metric == "low" else "temperature_2m_max"
            values = [float(values[0]) for key, values in daily.items() if key.startswith(prefix) and isinstance(values, list) and values and values[0] is not None]
            if len(values) < 5:
                LOG.info("weather forecast skipped: city=%s date=%s members=%d", spec.city_key, spec.target_date, len(values))
                return None
            mean = statistics.mean(values)
            std = statistics.pstdev(values) if len(values) > 1 else 0.0
            if spec.upper_threshold_f is not None:
                probability = sum(spec.threshold_f <= value < spec.upper_threshold_f for value in values) / len(values)
            else:
                above = sum(value > spec.threshold_f for value in values) / len(values)
                probability = above if spec.direction == "above" else 1.0 - above
            # Confidence rewards ensemble agreement but never pretends a small
            # ensemble is certain. This is a sizing input, not a probability.
            agreement = max(probability, 1.0 - probability)
            confidence = min(0.95, max(0.50, agreement * min(1.0, len(values) / 31.0)))
            estimate = WeatherEstimate(probability, confidence, mean, std, len(values), "open_meteo_gfs_ensemble", now)
            self._cache[key] = (now, estimate)
            return estimate
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            LOG.warning("weather forecast unavailable city=%s date=%s: %s", spec.city_key, spec.target_date, exc)
            return None


def collect_weather_strategy_signals(
    *, config: ArbConfig, candidate_markets: list[MarketInfo], ob_analyzer: OrderBookAnalyzer,
    provider: WeatherProvider, now: date | None = None,
) -> list[StrategySignal]:
    """Create normal BUY_YES/BUY_NO T2 signals for weather contracts."""
    signals: list[StrategySignal] = []
    min_dev = max(0.0, float(getattr(config, "weather_min_edge", 0.10)))
    min_conf = max(0.0, float(getattr(config, "weather_min_confidence", 0.70)))
    max_spread = max(0.0, float(getattr(config, "weather_max_spread_bps", 180.0)))
    min_depth = max(0.0, float(getattr(config, "weather_min_top_depth", 25.0)))
    for market in candidate_markets:
        if not market.active or market.closed or len(market.tokens) != 2:
            continue
        spec = parse_weather_market(market, today=now)
        if spec is None:
            continue
        yes = next((token for token in market.tokens if token.outcome.lower() == "yes"), market.tokens[0])
        no = next((token for token in market.tokens if token.outcome.lower() == "no"), market.tokens[-1])
        yes_snap = ob_analyzer.get_snapshot(yes.token_id)
        no_snap = ob_analyzer.get_snapshot(no.token_id)
        if yes_snap is None or no_snap is None or yes_snap.mid is None or no_snap.mid is None:
            continue
        spreads = [spread_bps_from_snapshot(snap) for snap in (yes_snap, no_snap)]
        if any(value is None or float(value) > max_spread for value in spreads):
            continue
        if min(float(yes_snap.best_ask_size), float(no_snap.best_ask_size)) < min_depth:
            continue
        estimate = provider.estimate(spec)
        if estimate is None or estimate.confidence < min_conf:
            continue
        market_prob = float(yes_snap.mid)
        deviation = float(estimate.probability) - market_prob
        if abs(deviation) < min_dev:
            continue
        action = "BUY_YES" if deviation > 0 else "BUY_NO"
        signals.append(StrategySignal(
            tier=StrategyTier.STATISTICAL_ARB,
            signal_type=f"weather_{action.lower()}", market_id=market.condition_id,
            description=f"{spec.city_name} {spec.target_date} {spec.metric} {spec.direction} {spec.threshold_f:g}F | p={estimate.probability:.3f} mkt={market_prob:.3f}",
            expected_edge=abs(deviation) * 10_000.0, confidence=estimate.confidence,
            recommended_size_usdc=float(config.default_order_size_usdc), urgency=0.9,
            payload={
                "action": action, "outcome": "YES", "model_prob": estimate.probability,
                "market_prob": market_prob, "deviation": deviation,
                "deviation_pct": deviation / market_prob if market_prob else 0.0,
                "category": "weather", "weather_source": estimate.source,
                "weather": {"city_key": spec.city_key, "city_name": spec.city_name,
                            "target_date": spec.target_date.isoformat(), "threshold_f": spec.threshold_f,
                            "upper_threshold_f": spec.upper_threshold_f,
                            "metric": spec.metric, "direction": spec.direction,
                            "forecast_mean_f": estimate.mean_f, "forecast_std_f": estimate.std_f,
                            "ensemble_members": estimate.members, "forecast_fetched_at": estimate.fetched_at},
            },
        ))
    return signals
