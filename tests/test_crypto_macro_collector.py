"""Tests for the CryptoMacroCollector (Fear & Greed)."""

from __future__ import annotations

import requests
import pytest

from research_signal.collectors.crypto_macro import (
    CryptoMacroCollector,
    _is_crypto_topic,
    _stance_from_fg,
)


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _patch_requests(monkeypatch, payload, status_code: int = 200):
    calls: list[tuple[str, float]] = []

    def fake_get(url, timeout=None, **kwargs):
        calls.append((url, timeout))
        return _FakeResponse(payload, status_code=status_code)

    monkeypatch.setattr("research_signal.collectors.crypto_macro.requests.get", fake_get)
    return calls


def test_is_crypto_topic_matches_btc_and_eth():
    assert _is_crypto_topic("Will BTC > $100k by end of year?")
    assert _is_crypto_topic("Will ethereum hit $5k?")
    assert _is_crypto_topic("Will Microstrategy buy more BTC?")
    assert not _is_crypto_topic("Will Trump win the next election?")
    assert not _is_crypto_topic("")


def test_stance_from_fg_low_value_is_bullish():
    assert _stance_from_fg(15) == "bullish"
    assert _stance_from_fg(25) == "bullish"
    assert _stance_from_fg(26) == "neutral"
    assert _stance_from_fg(50) == "neutral"
    assert _stance_from_fg(75) == "bearish"
    assert _stance_from_fg(80) == "bearish"
    assert _stance_from_fg(None) == "neutral"
    assert _stance_from_fg("bad") == "neutral"


def test_collector_disabled_emits_nothing():
    coll = CryptoMacroCollector(enabled=False)
    assert coll.collect(["BTC will hit $100k"]) == []


def test_collector_emits_one_row_per_crypto_topic(monkeypatch):
    payload = {"data": [{"value": "18", "value_classification": "Extreme Fear", "timestamp": "1779000000"}]}
    _patch_requests(monkeypatch, payload)
    coll = CryptoMacroCollector()
    rows = coll.collect([
        "Will BTC > $100k by EOY?",
        "Will ethereum break $5k?",
        "Will Trump win 2028?",  # non-crypto — should be filtered
    ])
    assert len(rows) == 2
    sources = {row["source"] for row in rows}
    assert sources == {"fear_greed"}
    stances = {row["stance"] for row in rows}
    assert stances == {"bullish"}  # F&G 18 → Extreme Fear → bullish
    assert all(row["extras"]["fear_greed_value"] == 18 for row in rows)


def test_collector_caches_within_ttl(monkeypatch):
    payload = {"data": [{"value": "55", "value_classification": "Neutral", "timestamp": "1779000000"}]}
    calls = _patch_requests(monkeypatch, payload)
    coll = CryptoMacroCollector(cache_ttl_sec=3600.0)
    coll.collect(["btc"])
    coll.collect(["eth"])
    coll.collect(["sol"])
    # Three collect() calls, but only one HTTP request thanks to caching.
    assert len(calls) == 1


def test_collector_handles_http_failure_gracefully(monkeypatch):
    _patch_requests(monkeypatch, {"data": []}, status_code=500)
    coll = CryptoMacroCollector()
    assert coll.collect(["btc"]) == []


def test_collector_handles_empty_payload(monkeypatch):
    _patch_requests(monkeypatch, {"data": []})
    coll = CryptoMacroCollector()
    assert coll.collect(["btc"]) == []
