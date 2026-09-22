"""Tests for strategy signal telemetry compression."""

from __future__ import annotations

from polymarket_arb.main_helpers.signal_telemetry import StrategySignalTelemetryCompressor


def _payload(*, submitted: bool = False, market_id: str = "m1") -> dict:
    return {
        "tier": "STATISTICAL_ARB",
        "signal_type": "statistical_buy_no",
        "market_id": market_id,
        "submitted": submitted,
        "expected_edge": 200,
        "payload": {"execution_check": {"action": "BUY_NO"}},
    }


def test_unsubmitted_duplicate_signals_are_suppressed_until_cooldown() -> None:
    compressor = StrategySignalTelemetryCompressor(cooldown_sec=60)

    first = compressor.consume(_payload(), now=100.0)
    second = compressor.consume(_payload(), now=105.0)
    third = compressor.consume(_payload(), now=160.0)

    assert len(first) == 1
    assert first[0]["duplicate_count"] == 1
    assert second == []
    assert len(third) == 1
    assert third[0]["compressed"] is True
    assert third[0]["duplicate_count"] == 2
    assert third[0]["duplicate_window_sec"] == 60.0


def test_submitted_signals_are_never_suppressed() -> None:
    compressor = StrategySignalTelemetryCompressor(cooldown_sec=60)

    assert len(compressor.consume(_payload(submitted=True), now=100.0)) == 1
    assert len(compressor.consume(_payload(submitted=True), now=101.0)) == 1


def test_distinct_markets_have_distinct_compression_buckets() -> None:
    compressor = StrategySignalTelemetryCompressor(cooldown_sec=60)

    assert len(compressor.consume(_payload(market_id="m1"), now=100.0)) == 1
    assert len(compressor.consume(_payload(market_id="m2"), now=101.0)) == 1
