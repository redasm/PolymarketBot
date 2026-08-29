"""RTDS 现货源与双源合成 (结算口径 vs 流动性口径)."""

from __future__ import annotations

import pytest

from polymarket_arb.rtds_feed import (
    CompositeSpotFeed,
    RtdsSpotFeed,
    normalize_rtds_symbol,
    parse_rtds_crypto_payload,
)


# --------- symbol 归一化 ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BTC/USD", "btc"),
        ("BTCUSDT", "btc"),
        ("btc-usd", "btc"),
        ("ETH_USDC", "eth"),
        ("SOL", "sol"),
        ("", ""),
        (None, ""),
        ("0xabc123", ""),
        ("a-very-long-symbol-name", ""),
    ],
)
def test_normalize_symbol(raw, expected):
    assert normalize_rtds_symbol(raw) == expected


# --------- payload 解析 ----------


def test_parse_single_payload_object():
    out = parse_rtds_crypto_payload(
        {
            "topic": "crypto_prices",
            "timestamp": 1700000000000,
            "payload": {"symbol": "BTC/USD", "value": "65000.5"},
        }
    )
    assert out == [("btc", 65000.5, 1700000000.0)]


def test_parse_payload_array():
    out = parse_rtds_crypto_payload(
        {
            "topic": "crypto_prices",
            "payload": [
                {"symbol": "BTCUSDT", "price": 65000, "timestamp": 1700000000},
                {"symbol": "ETHUSDT", "price": 3200, "timestamp": 1700000000},
            ],
        }
    )
    assert [row[0] for row in out] == ["btc", "eth"]


def test_parse_ignores_other_topics():
    assert parse_rtds_crypto_payload({"topic": "comments", "payload": {"symbol": "btc", "value": 1}}) == []


def test_parse_drops_rows_without_symbol_or_price():
    out = parse_rtds_crypto_payload(
        {
            "topic": "crypto_prices",
            "payload": [
                {"value": 65000},
                {"symbol": "BTC/USD"},
                {"symbol": "BTC/USD", "value": "nope"},
                {"symbol": "BTC/USD", "value": -5},
                {"symbol": "BTC/USD", "value": 65000},
            ],
        }
    )
    assert out == [("btc", 65000.0, pytest.approx(out[0][2]))]


def test_parse_tolerates_non_dict_payload():
    assert parse_rtds_crypto_payload({"topic": "crypto_prices", "payload": "junk"}) == []


# --------- feed 状态 ----------


def _rtds(symbols=("btc",)) -> RtdsSpotFeed:
    return RtdsSpotFeed(symbols=list(symbols), window_secs=[900])


def _msg(symbol="BTC/USD", value=65000.0, ts=1700000000.0):
    return {
        "topic": "crypto_prices",
        "payload": {"symbol": symbol, "value": value, "timestamp": ts},
    }


def test_feed_tracks_spot_and_age():
    feed = _rtds()
    assert feed.handle_message(_msg()) == 1
    assert feed.get_spot("btc") == pytest.approx(65000.0)
    assert feed.get_spot_age("btc", now=1700000030.0) == pytest.approx(30.0)
    assert feed.get_spot("eth") is None


def test_feed_ignores_symbols_it_did_not_subscribe():
    feed = _rtds(("btc",))
    assert feed.handle_message(_msg(symbol="ETH/USD")) == 0
    assert feed.stats()["unknown_symbols"] == 1


def test_feed_locks_window_reference_price():
    feed = _rtds()
    slot_start = 1700000100.0  # 15m 对齐点
    slot = feed.ref_tracker.slot_for(slot_start, 900)
    feed.ref_tracker._start_ts = 0.0  # 允许测试观测历史窗口
    feed.handle_message(_msg(value=64000.0, ts=slot_start))
    feed.handle_message(_msg(value=66000.0, ts=slot_start + 60))
    # 窗口起点价一旦锁定就不再被窗口内的后续 tick 覆盖。
    assert feed.ref_tracker.get_ref("btc", 900, slot) == pytest.approx(64000.0)


def test_feed_parses_raw_frames():
    import json

    feed = _rtds()
    feed._handle_frame("PING")
    feed._handle_frame("not json")
    feed._handle_frame(json.dumps(_msg()))
    assert feed.get_spot("btc") == pytest.approx(65000.0)


def test_subscription_payload_shape():
    feed = RtdsSpotFeed(symbols=["btc"], window_secs=[900], topics=["crypto_prices"])
    payload = feed._subscription_payload()
    assert payload["action"] == "subscribe"
    assert payload["subscriptions"] == [{"topic": "crypto_prices", "type": "*"}]


# --------- 双源合成 ----------


class _StubFeed:
    def __init__(self, spot=None, sigma=None, age=0.0, connected=True):
        self._spot = spot
        self._sigma = sigma
        self._age = age
        self._connected = connected
        self.started = False
        self.stopped = False
        self.ref_tracker = _StubRefTracker(None)

    def get_spot(self, symbol):
        return self._spot

    def get_spot_age(self, symbol, now=None):
        return self._age

    def get_sigma_15m(self, symbol):
        return self._sigma

    def is_connected(self):
        return self._connected

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _StubRefTracker:
    def __init__(self, ref):
        self.ref = ref

    def get_ref(self, symbol, window_sec, slot):
        return self.ref


def test_shadow_mode_prices_from_fallback():
    primary = _StubFeed(spot=65100.0, sigma=0.02)
    fallback = _StubFeed(spot=65000.0, sigma=0.03)
    composite = CompositeSpotFeed(primary=primary, fallback=fallback, mode="shadow")
    assert composite.get_spot("btc") == pytest.approx(65000.0)
    assert composite.get_sigma_15m("btc") == pytest.approx(0.03)


def test_primary_mode_prices_from_rtds():
    primary = _StubFeed(spot=65100.0, sigma=0.02)
    fallback = _StubFeed(spot=65000.0, sigma=0.03)
    composite = CompositeSpotFeed(primary=primary, fallback=fallback, mode="primary")
    assert composite.get_spot("btc") == pytest.approx(65100.0)
    assert composite.get_sigma_15m("btc") == pytest.approx(0.02)


def test_primary_mode_falls_back_when_rtds_is_stale():
    primary = _StubFeed(spot=65100.0, age=120.0)
    fallback = _StubFeed(spot=65000.0)
    composite = CompositeSpotFeed(
        primary=primary, fallback=fallback, mode="primary", staleness_sec=30.0
    )
    assert composite.get_spot("btc") == pytest.approx(65000.0)


def test_primary_mode_falls_back_when_rtds_has_no_price():
    primary = _StubFeed(spot=None)
    fallback = _StubFeed(spot=65000.0)
    composite = CompositeSpotFeed(primary=primary, fallback=fallback, mode="primary")
    assert composite.get_spot("btc") == pytest.approx(65000.0)


def test_off_mode_never_consults_rtds():
    primary = _StubFeed(spot=65100.0)
    fallback = _StubFeed(spot=65000.0)
    composite = CompositeSpotFeed(primary=primary, fallback=fallback, mode="off")
    composite.start()
    assert composite.get_spot("btc") == pytest.approx(65000.0)
    assert primary.started is False
    assert fallback.started is True


def test_invalid_mode_defaults_to_shadow():
    composite = CompositeSpotFeed(primary=_StubFeed(), fallback=_StubFeed(), mode="nonsense")
    assert composite.mode == "shadow"


def test_ref_tracker_follows_the_active_source():
    primary = _StubFeed(spot=65100.0)
    primary.ref_tracker = _StubRefTracker(64000.0)
    fallback = _StubFeed(spot=65000.0)
    fallback.ref_tracker = _StubRefTracker(63000.0)

    shadow = CompositeSpotFeed(primary=primary, fallback=fallback, mode="shadow")
    assert shadow.ref_tracker.get_ref("btc", 900, 1) == pytest.approx(63000.0)

    live = CompositeSpotFeed(primary=primary, fallback=fallback, mode="primary")
    assert live.ref_tracker.get_ref("btc", 900, 1) == pytest.approx(64000.0)


def test_basis_report_computes_bps_and_source():
    primary = _StubFeed(spot=65065.0, age=1.0)
    fallback = _StubFeed(spot=65000.0)
    composite = CompositeSpotFeed(primary=primary, fallback=fallback, mode="shadow")
    report = composite.basis_report(["btc", ""])
    assert report["mode"] == "shadow"
    assert report["symbols"]["btc"]["basis_bps"] == pytest.approx(10.0, abs=1e-6)
    assert report["symbols"]["btc"]["source"] == "binance"
    assert report["max_abs_basis_bps"] == pytest.approx(10.0, abs=1e-6)
    assert "" not in report["symbols"]


def test_basis_report_without_rtds_price():
    composite = CompositeSpotFeed(
        primary=_StubFeed(spot=None), fallback=_StubFeed(spot=65000.0), mode="shadow"
    )
    report = composite.basis_report(["btc"])
    assert "basis_bps" not in report["symbols"]["btc"]
    assert report["max_abs_basis_bps"] == 0.0


def test_stop_shuts_down_both_feeds():
    primary, fallback = _StubFeed(), _StubFeed()
    CompositeSpotFeed(primary=primary, fallback=fallback).stop()
    assert primary.stopped and fallback.stopped
