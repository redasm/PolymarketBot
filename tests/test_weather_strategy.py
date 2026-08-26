from __future__ import annotations

from datetime import date

import pytest

from polymarket_arb.models import MarketInfo, OrderBookLevel, OrderBookSnapshot, TokenInfo
from polymarket_arb.strategies.weather_strategy import (
    WeatherEstimate,
    collect_weather_strategy_signals,
    parse_weather_market,
)
from tests.conftest import make_test_config


def _market(question: str = "Will the high temperature in New York exceed 75°F on August 27, 2026?") -> MarketInfo:
    return MarketInfo(
        condition_id="weather-1", question=question, slug="weather-nyc",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        volume_24h=10.0, liquidity=100.0,
    )


def _book(token_id: str, mid: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id, best_bid=mid - 0.002, best_ask=mid + 0.002,
        bids=[OrderBookLevel(mid - 0.002, 100.0)], asks=[OrderBookLevel(mid + 0.002, 100.0)],
    )


class _Books:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def get_snapshot(self, token_id):
        return self.snapshots.get(token_id)


class _Provider:
    def estimate(self, spec):
        assert spec.city_key == "nyc"
        return WeatherEstimate(0.78, 0.90, 77.0, 2.0, 31, "test", 1.0)


def test_parse_weather_market_extracts_contract():
    spec = parse_weather_market(_market(), today=date(2026, 8, 26))
    assert spec is not None
    assert (spec.city_key, spec.threshold_f, spec.metric, spec.direction) == ("nyc", 75.0, "high", "above")
    assert spec.target_date == date(2026, 8, 27)


def test_parse_weather_market_supports_temperature_bucket():
    market = _market("Will NYC high temperature be between 70°F and 71°F on August 27, 2026?")
    spec = parse_weather_market(market, today=date(2026, 8, 26))
    assert spec is not None
    assert spec.direction == "range"
    assert spec.threshold_f == 70.0
    assert spec.upper_threshold_f == 71.0


def test_weather_collector_emits_normal_t2_buy_yes_signal():
    cfg = make_test_config(weather_strategy_enabled=True, weather_min_edge=0.10, weather_min_confidence=0.70)
    books = _Books({"yes-1": _book("yes-1", 0.60), "no-1": _book("no-1", 0.40)})
    signals = collect_weather_strategy_signals(
        config=cfg, candidate_markets=[_market()], ob_analyzer=books, provider=_Provider(), now=date(2026, 8, 26)
    )
    assert len(signals) == 1
    signal = signals[0]
    assert signal.signal_type == "weather_buy_yes"
    assert signal.payload["category"] == "weather"
    assert signal.payload["action"] == "BUY_YES"
    assert signal.expected_edge == pytest.approx(1800.0)


def test_weather_collector_skips_small_edge():
    cfg = make_test_config(weather_strategy_enabled=True, weather_min_edge=0.25)
    books = _Books({"yes-1": _book("yes-1", 0.60), "no-1": _book("no-1", 0.40)})
    assert collect_weather_strategy_signals(
        config=cfg, candidate_markets=[_market()], ob_analyzer=books, provider=_Provider(), now=date(2026, 8, 26)
    ) == []
