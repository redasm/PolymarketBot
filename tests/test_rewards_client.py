"""RewardsClient: /rewards/markets 解析、缓存、降级、后台预热."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from polymarket_arb.rewards_client import (
    RewardsClient,
    RewardsConfig,
    parse_rewards_payload,
)


class _StubResponse:
    def __init__(self, payload, *, status: int = 200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self):
        return self._payload


class _StubSession:
    def __init__(self, responses):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, timeout=None):  # noqa: ARG002
        self.calls.append(url)
        item = self._responses.pop(0) if self._responses else _StubResponse({"data": []})
        if isinstance(item, Exception):
            raise item
        return item


def _payload(max_spread=3.0, min_size=50.0, daily=5.0):
    return {
        "data": [
            {
                "condition_id": "c1",
                "rewards_max_spread": max_spread,
                "rewards_min_size": min_size,
                "rates": [{"asset_address": "0x0", "rewards_daily_rate": daily}],
            }
        ]
    }


# --------- 解析 ----------


def test_parse_wraps_data_list():
    cfg = parse_rewards_payload("c1", _payload())
    assert cfg is not None
    assert cfg.rewards_max_spread == 3.0
    assert cfg.rewards_min_size == 50.0
    assert cfg.daily_rate_usdc == 5.0
    # 3 cent -> 0.03 in price space
    assert cfg.reward_delta == pytest.approx(0.03)
    assert cfg.is_incentivized is True


def test_parse_accepts_bare_list_and_bare_dict():
    row = {"rewards_max_spread": 2.0}
    assert parse_rewards_payload("c1", [row]).reward_delta == pytest.approx(0.02)
    assert parse_rewards_payload("c1", row).reward_delta == pytest.approx(0.02)


def test_parse_empty_returns_none():
    assert parse_rewards_payload("c1", {"data": []}) is None
    assert parse_rewards_payload("c1", None) is None
    assert parse_rewards_payload("c1", "garbage") is None


def test_parse_tolerates_garbage_fields():
    cfg = parse_rewards_payload(
        "c1", {"data": [{"rewards_max_spread": "n/a", "rates": "nope"}]}
    )
    assert cfg is not None
    assert cfg.reward_delta == 0.0
    assert cfg.daily_rate_usdc == 0.0


def test_insane_delta_is_rejected():
    """量纲错误（例如把 bps 当 cent 返回）必须按无奖励带处理."""
    cfg = RewardsConfig(condition_id="c1", rewards_max_spread=300.0)
    assert cfg.reward_delta == 0.0
    assert cfg.is_incentivized is False


# --------- 缓存与降级 ----------


def test_get_caches_by_ttl():
    session = _StubSession([_StubResponse(_payload())])
    client = RewardsClient("https://clob.test", session=session, ttl_sec=600.0)
    first = client.get("c1")
    second = client.get("c1")
    assert first is second
    assert len(session.calls) == 1
    assert session.calls[0] == "https://clob.test/rewards/markets/c1"
    assert client.stats()["hits"] == 1


def test_http_failure_degrades_to_zero_delta_and_negative_caches():
    session = _StubSession([RuntimeError("boom")])
    client = RewardsClient("https://clob.test", session=session, negative_ttl_sec=600.0)
    assert client.get("c1") is None
    assert client.reward_delta("c1") == 0.0
    # negative cache: no second HTTP call
    assert len(session.calls) == 1
    assert client.stats()["errors"] == 1


def test_expired_entry_is_refetched():
    session = _StubSession([_StubResponse(_payload()), _StubResponse(_payload(4.0))])
    client = RewardsClient("https://clob.test", session=session, ttl_sec=0.0)
    assert client.get("c1").reward_delta == pytest.approx(0.03)
    assert client.get("c1").reward_delta == pytest.approx(0.04)
    assert len(session.calls) == 2


def test_disabled_client_never_calls_network():
    session = _StubSession([_StubResponse(_payload())])
    client = RewardsClient("https://clob.test", session=session, enabled=False)
    assert client.get("c1") is None
    assert client.reward_delta("c1") == 0.0
    assert client.request(["c1"]) == 0
    assert client.prefetch(["c1"]) == 0
    assert session.calls == []


def test_empty_host_disables_client():
    assert RewardsClient("").enabled is False


# --------- 热路径只读缓存 ----------


def test_cached_never_hits_network():
    session = _StubSession([_StubResponse(_payload())])
    client = RewardsClient("https://clob.test", session=session)
    assert client.cached("c1") is None
    assert client.cached_reward_delta("c1") == 0.0
    assert session.calls == []
    client.get("c1")
    assert client.cached_reward_delta("c1") == pytest.approx(0.03)


def test_request_prefetches_in_background():
    session = _StubSession([_StubResponse(_payload()), _StubResponse(_payload(2.0))])
    client = RewardsClient(
        "https://clob.test", session=session, fetch_interval_sec=0.0
    )
    try:
        assert client.request(["c1", "c2"]) == 2
        deadline = time.time() + 3.0
        while time.time() < deadline and len(session.calls) < 2:
            time.sleep(0.01)
        assert len(session.calls) == 2
        assert client.cached_reward_delta("c1") == pytest.approx(0.03)
    finally:
        client.close()


def test_request_skips_already_cached():
    session = _StubSession([_StubResponse(_payload())])
    client = RewardsClient("https://clob.test", session=session, ttl_sec=600.0)
    client.get("c1")
    assert client.request(["c1"]) == 0


def test_prefetch_respects_max_fetches():
    session = _StubSession([_StubResponse(_payload()) for _ in range(5)])
    client = RewardsClient("https://clob.test", session=session)
    assert client.prefetch(["c1", "c2", "c3"], max_fetches=2) == 2
    assert len(session.calls) == 2
