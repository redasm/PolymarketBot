"""Tests for the Manifold Markets research signal collector."""

from __future__ import annotations

import time
from typing import Any

import pytest
import requests

from research_signal.collectors.manifold import ManifoldCollector


def _market(
    *,
    question: str = "Will Foo happen by 2027?",
    probability: float = 0.7,
    volume: float = 500.0,
    outcome_type: str = "BINARY",
    is_resolved: bool = False,
    close_time_ms: float | None = None,
    last_updated_ms: float | None = None,
    creator: str = "alice",
    slug: str = "foo-by-2027",
    market_id: str = "m1",
    url: str | None = None,
    unique_bettors: int = 42,
) -> dict[str, Any]:
    now_ms = time.time() * 1000.0
    return {
        "id": market_id,
        "question": question,
        "probability": probability,
        "volume": volume,
        "outcomeType": outcome_type,
        "isResolved": is_resolved,
        "closeTime": close_time_ms if close_time_ms is not None else now_ms + 86_400_000,
        "lastUpdatedTime": last_updated_ms if last_updated_ms is not None else now_ms,
        "creatorUsername": creator,
        "slug": slug,
        "url": url,
        "uniqueBettorCount": unique_bettors,
    }


class _StubResponse:
    def __init__(self, payload: Any, status_ok: bool = True):
        self._payload = payload
        self._status_ok = status_ok

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise requests.HTTPError("stub error")


def _patch_get(monkeypatch, payload: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return _StubResponse(payload)

    monkeypatch.setattr(
        "research_signal.collectors.manifold.requests.get",
        fake_get,
    )
    return calls


def test_collect_returns_rows_for_binary_markets(monkeypatch):
    payload = [_market(question="Will BTC hit $100k by EOY?", probability=0.68)]
    _patch_get(monkeypatch, payload)
    collector = ManifoldCollector(enabled=True, max_results_per_topic=2)

    rows = collector.collect(["btc 100k"])

    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "manifold"
    assert row["topic"] == "btc 100k"
    assert row["stance"] == "bullish"
    assert "Manifold:" in row["summary"]
    assert "68%" in row["summary"]
    assert row["link"].startswith("https://manifold.markets/")
    assert row["published_ts"] > 0
    assert row["extras"]["probability"] == pytest.approx(0.68)
    assert row["extras"]["volume_usd"] == pytest.approx(500.0)
    assert row["extras"]["unique_bettors"] == 42
    assert row["extras"]["outcome_type"] == "BINARY"


@pytest.mark.parametrize(
    "prob,expected",
    [
        (0.65, "bullish"),
        (0.60, "bullish"),
        (0.55, "neutral"),
        (0.50, "neutral"),
        (0.45, "neutral"),
        (0.40, "bearish"),
        (0.30, "bearish"),
    ],
)
def test_stance_thresholds(monkeypatch, prob, expected):
    _patch_get(monkeypatch, [_market(probability=prob)])
    collector = ManifoldCollector(enabled=True)

    rows = collector.collect(["topic"])

    assert rows and rows[0]["stance"] == expected


def test_skips_low_volume_markets(monkeypatch):
    _patch_get(monkeypatch, [_market(volume=10.0)])
    collector = ManifoldCollector(enabled=True, min_volume_usd=50.0)

    rows = collector.collect(["topic"])

    assert rows == []


def test_skips_resolved_markets(monkeypatch):
    _patch_get(monkeypatch, [_market(is_resolved=True)])
    collector = ManifoldCollector(enabled=True)

    rows = collector.collect(["topic"])

    assert rows == []


def test_skips_non_binary_markets(monkeypatch):
    _patch_get(monkeypatch, [_market(outcome_type="MULTIPLE_CHOICE")])
    collector = ManifoldCollector(enabled=True)

    rows = collector.collect(["topic"])

    assert rows == []


def test_skips_closed_markets(monkeypatch):
    past_ms = (time.time() - 3600) * 1000.0
    _patch_get(monkeypatch, [_market(close_time_ms=past_ms)])
    collector = ManifoldCollector(enabled=True)

    rows = collector.collect(["topic"])

    assert rows == []


def test_respects_max_results_per_topic(monkeypatch):
    payload = [
        _market(market_id=f"m{i}", slug=f"q-{i}", question=f"Q {i}?")
        for i in range(5)
    ]
    _patch_get(monkeypatch, payload)
    collector = ManifoldCollector(enabled=True, max_results_per_topic=2)

    rows = collector.collect(["topic"])

    assert len(rows) == 2


def test_returns_empty_on_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr("research_signal.collectors.manifold.requests.get", boom)
    collector = ManifoldCollector(enabled=True)

    rows = collector.collect(["topic"])

    assert rows == []


def test_returns_empty_when_disabled(monkeypatch):
    calls = _patch_get(monkeypatch, [_market()])
    collector = ManifoldCollector(enabled=False)

    rows = collector.collect(["topic"])

    assert rows == []
    assert calls == []


def test_cache_hit_avoids_repeat_call(monkeypatch):
    calls = _patch_get(monkeypatch, [_market()])
    collector = ManifoldCollector(enabled=True, cache_ttl_sec=300.0)

    first = collector.collect(["topic"])
    second = collector.collect(["topic"])

    assert len(first) == 1 and len(second) == 1
    assert len(calls) == 1


def test_skips_empty_topics(monkeypatch):
    calls = _patch_get(monkeypatch, [_market()])
    collector = ManifoldCollector(enabled=True)

    rows = collector.collect(["", "   "])

    assert rows == []
    assert calls == []


def test_uses_provided_url_field(monkeypatch):
    explicit_url = "https://manifold.markets/foo/bar-direct"
    _patch_get(monkeypatch, [_market(url=explicit_url)])
    collector = ManifoldCollector(enabled=True)

    rows = collector.collect(["topic"])

    assert rows[0]["link"] == explicit_url
