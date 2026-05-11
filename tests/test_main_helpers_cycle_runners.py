"""Tests for `polymarket_arb.main_helpers.cycle_runners`.

Locks in the per-cycle orchestration helpers extracted from
`main_loop.py`:

- `refresh_market_universe` cache + interval logic
- `start_ws_feed` token subscription bookkeeping
- `scan_cycle` opportunity collection + sorting
- `find_pending_signal` / `find_pending_signal_overlay` lookup matching
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from polymarket_arb.main_helpers.cycle_runners import (
    find_pending_signal,
    find_pending_signal_overlay,
    refresh_market_universe,
    scan_cycle,
    start_ws_feed,
)
from polymarket_arb.models import MarketInfo, TokenInfo


# ---------- shared fixtures ---------------------------------------------------


def _market(condition_id: str = "0xabc", question: str = "Will X happen?") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question=question,
        slug="will-x",
        tokens=[
            TokenInfo(token_id="tok-yes", outcome="Yes", price=0.55),
            TokenInfo(token_id="tok-no", outcome="No", price=0.45),
        ],
    )


# ---------- refresh_market_universe ------------------------------------------


def _scanner_stub(markets, events):
    return SimpleNamespace(
        fetch_active_markets=lambda **_: list(markets),
        fetch_active_events=lambda **_: list(events),
    )


def _config_stub(refresh_sec: float, hot_event_pool_size: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        market_universe_refresh_sec=refresh_sec,
        min_liquidity=100.0,
        min_volume_24h=500.0,
        hot_event_pool_size=hot_event_pool_size,
    )


def test_refresh_universe_cold_start_always_refreshes() -> None:
    scanner = _scanner_stub([_market()], ["evt"])
    cfg = _config_stub(refresh_sec=600)

    markets, events, ts, refreshed = refresh_market_universe(
        scanner=scanner,
        config=cfg,
        cached_markets=[],
        cached_events=[],
        last_refresh_ts=time.time(),  # recent ts shouldn't suppress cold start
    )

    assert refreshed is True
    assert len(markets) == 1
    assert events == ["evt"]
    assert ts == pytest.approx(time.time(), abs=2.0)


def test_refresh_universe_serves_cache_within_interval() -> None:
    scanner = _scanner_stub([_market("0xnew")], ["new-evt"])
    cfg = _config_stub(refresh_sec=600)
    cached_markets = [_market("0xcached")]
    cached_events = ["cached-evt"]
    last_refresh = time.time() - 10  # 10s ago, well below 600s

    markets, events, ts, refreshed = refresh_market_universe(
        scanner=scanner,
        config=cfg,
        cached_markets=cached_markets,
        cached_events=cached_events,
        last_refresh_ts=last_refresh,
    )

    assert refreshed is False
    assert markets is cached_markets
    assert events is cached_events
    assert ts == last_refresh


def test_refresh_universe_refreshes_after_interval_expires() -> None:
    scanner = _scanner_stub([_market("0xnew")], ["new-evt"])
    cfg = _config_stub(refresh_sec=60)
    last_refresh = time.time() - 120  # interval elapsed

    markets, events, ts, refreshed = refresh_market_universe(
        scanner=scanner,
        config=cfg,
        cached_markets=[_market("0xcached")],
        cached_events=["cached-evt"],
        last_refresh_ts=last_refresh,
    )

    assert refreshed is True
    assert markets[0].condition_id == "0xnew"
    assert events == ["new-evt"]
    assert ts > last_refresh


def test_refresh_universe_event_limit_floor_is_50() -> None:
    captured: dict = {}

    def fetch_events(*, limit: int) -> list:
        captured["limit"] = limit
        return []

    scanner = SimpleNamespace(
        fetch_active_markets=lambda **_: [_market()],
        fetch_active_events=fetch_events,
    )
    cfg = _config_stub(refresh_sec=60, hot_event_pool_size=5)  # 5*2 = 10, floored to 50

    refresh_market_universe(
        scanner=scanner,
        config=cfg,
        cached_markets=[],
        cached_events=[],
        last_refresh_ts=0.0,
    )
    assert captured["limit"] == 50


# ---------- start_ws_feed -----------------------------------------------------


@dataclass
class _FakeMirror:
    callbacks: list = field(default_factory=list)

    def register_callback(self, cb) -> None:
        self.callbacks.append(cb)


@dataclass
class _FakeFeed:
    subscribed: list = field(default_factory=list)
    started: bool = False
    init_kwargs: dict = field(default_factory=dict)

    def subscribe(self, token_ids) -> None:
        self.subscribed = list(token_ids)

    def start(self) -> None:
        self.started = True


@dataclass
class _FakeBookStore:
    bound: tuple | None = None

    def set_market(self, condition_id, yes_id, no_id) -> None:
        self.bound = (condition_id, yes_id, no_id)


@dataclass
class _FakeTickRecorder:
    is_enabled: bool = True
    registered: list = field(default_factory=list)

    def register_markets(self, markets) -> None:
        self.registered.extend(markets)

    def on_book_update(self, *_args, **_kwargs) -> None:
        pass


def test_start_ws_feed_binds_primary_market_and_subscribes_all_tokens(monkeypatch) -> None:
    fake_feed = _FakeFeed()
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.WebSocketFeed",
        lambda **_: fake_feed,
    )
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.OrderBookMirror",
        _FakeMirror,
    )
    store = _FakeBookStore()
    primary = _market("0xprimary")
    secondary = _market("0xsecondary", question="Will Y happen?")
    secondary.tokens = [
        TokenInfo(token_id="sec-yes", outcome="Yes", price=0.5),
        TokenInfo(token_id="sec-no", outcome="No", price=0.5),
    ]

    feed, mirror = start_ws_feed([primary, secondary], store, tick_recorder=None)

    assert feed is fake_feed
    assert isinstance(mirror, _FakeMirror)
    assert store.bound == ("0xprimary", "tok-yes", "tok-no")
    assert fake_feed.subscribed == ["tok-yes", "tok-no", "sec-yes", "sec-no"]
    assert fake_feed.started is True


def test_start_ws_feed_registers_tick_recorder_callback_when_enabled(monkeypatch) -> None:
    fake_feed = _FakeFeed()
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.WebSocketFeed",
        lambda **_: fake_feed,
    )
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.OrderBookMirror",
        _FakeMirror,
    )
    recorder = _FakeTickRecorder(is_enabled=True)

    feed, mirror = start_ws_feed([_market()], _FakeBookStore(), tick_recorder=recorder)

    assert recorder.registered, "expected register_markets to be called"
    assert mirror.callbacks == [recorder.on_book_update]


def test_start_ws_feed_skips_disabled_tick_recorder(monkeypatch) -> None:
    fake_feed = _FakeFeed()
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.WebSocketFeed",
        lambda **_: fake_feed,
    )
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.OrderBookMirror",
        _FakeMirror,
    )
    recorder = _FakeTickRecorder(is_enabled=False)

    _feed, mirror = start_ws_feed([_market()], _FakeBookStore(), tick_recorder=recorder)

    assert recorder.registered == []
    assert mirror.callbacks == []


def test_start_ws_feed_registers_flow_ingest_and_passes_trade_callback(monkeypatch) -> None:
    """The flow-ingest token map should be rebuilt on every (re)start.

    We assert (a) the FlowIngest learns the targets' token map, and
    (b) the WebSocketFeed gets a ``trade_callback`` wired to
    ``flow_ingest.on_trade`` so live trades flow into the aggregator.
    """
    fake_feed = _FakeFeed()
    captured_kwargs: dict = {}

    def _factory(**kwargs):
        captured_kwargs.update(kwargs)
        return fake_feed

    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.WebSocketFeed",
        _factory,
    )
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.OrderBookMirror",
        _FakeMirror,
    )

    @dataclass
    class _FakeIngest:
        registered: list = field(default_factory=list)

        def register_markets(self, markets) -> None:
            self.registered.extend(markets)

        def on_trade(self, event) -> None:
            pass

    ingest = _FakeIngest()

    start_ws_feed([_market("0xprimary")], _FakeBookStore(), flow_ingest=ingest)

    assert ingest.registered, "FlowIngest must learn the target market token map"
    assert captured_kwargs.get("trade_callback") == ingest.on_trade


def test_start_ws_feed_omits_trade_callback_when_no_flow_ingest(monkeypatch) -> None:
    fake_feed = _FakeFeed()
    captured_kwargs: dict = {}

    def _factory(**kwargs):
        captured_kwargs.update(kwargs)
        return fake_feed

    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.WebSocketFeed",
        _factory,
    )
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cycle_runners.OrderBookMirror",
        _FakeMirror,
    )

    start_ws_feed([_market()], _FakeBookStore())

    assert captured_kwargs.get("trade_callback") is None


# ---------- scan_cycle --------------------------------------------------------


def _opp(net_edge: float, profitable: bool = True) -> SimpleNamespace:
    return SimpleNamespace(net_edge=net_edge, is_profitable=profitable)


def test_scan_cycle_collects_only_profitable_and_sorts_by_edge() -> None:
    binary_market = _market("0xa")
    binary_market_2 = _market("0xb")
    unprofitable = _market("0xc")

    detector = SimpleNamespace(
        scan_binary_market=lambda m: {
            "0xa": _opp(0.05),
            "0xb": _opp(0.10),
            "0xc": _opp(0.01, profitable=False),
        }[m.condition_id],
        scan_multi_outcome_event=lambda e: _opp(0.07),
    )
    event = SimpleNamespace(markets=[1, 2, 3])

    opps = scan_cycle(
        detector=detector,
        config=SimpleNamespace(),
        candidate_markets=[binary_market, binary_market_2, unprofitable],
        candidate_events=[event],
        universe_market_count=42,
        universe_refreshed=True,
    )

    edges = [o.net_edge for o in opps]
    assert edges == sorted(edges, reverse=True)
    assert 0.10 in edges and 0.07 in edges and 0.05 in edges
    assert 0.01 not in edges


def test_scan_cycle_skips_non_binary_markets() -> None:
    three_outcome = _market("0xa")
    three_outcome.tokens = three_outcome.tokens + [
        TokenInfo(token_id="tok-third", outcome="Maybe", price=0.1)
    ]
    calls: list = []

    detector = SimpleNamespace(
        scan_binary_market=lambda m: calls.append(m) or _opp(0.1),
        scan_multi_outcome_event=lambda e: None,
    )

    opps = scan_cycle(
        detector=detector,
        config=SimpleNamespace(),
        candidate_markets=[three_outcome],
        candidate_events=[],
        universe_market_count=1,
        universe_refreshed=False,
    )
    assert calls == [], "binary scanner must not be called for 3-outcome markets"
    assert opps == []


def test_scan_cycle_skips_single_market_events() -> None:
    detector = SimpleNamespace(
        scan_binary_market=lambda m: None,
        scan_multi_outcome_event=lambda e: pytest.fail("must not be called"),
    )
    one_market_event = SimpleNamespace(markets=[1])

    opps = scan_cycle(
        detector=detector,
        config=SimpleNamespace(),
        candidate_markets=[],
        candidate_events=[one_market_event],
        universe_market_count=0,
        universe_refreshed=False,
    )
    assert opps == []


def test_scan_cycle_progress_callback_emits_phase_pulses() -> None:
    pulses: list[dict] = []
    detector = SimpleNamespace(
        scan_binary_market=lambda m: _opp(0.1),
        scan_multi_outcome_event=lambda e: None,
    )

    scan_cycle(
        detector=detector,
        config=SimpleNamespace(),
        candidate_markets=[_market(f"0x{i}") for i in range(3)],
        candidate_events=[],
        universe_market_count=10,
        universe_refreshed=True,
        progress_cb=lambda **kw: pulses.append(kw),
    )

    phases = [p["phase"] for p in pulses]
    assert phases.count("scanning_books") >= 2  # opening + per-market pulses
    assert "scanning_events" in phases


# ---------- find_pending_signal[_overlay] ------------------------------------


def _signal(market_id: str, signal_type: str, ts: float, overlay: dict | None = None):
    payload: dict = {}
    if overlay is not None:
        payload["research_overlay"] = overlay
    return SimpleNamespace(
        market_id=market_id,
        signal_type=signal_type,
        timestamp=ts,
        payload=payload,
    )


def test_find_pending_signal_matches_on_market_type_and_timestamp() -> None:
    target = _signal("mkt-1", "ai_buy", ts=100.0)
    other = _signal("mkt-2", "ai_buy", ts=100.0)
    orchestrator = SimpleNamespace(_pending_signals=[other, target])

    found = find_pending_signal(orchestrator, _signal("mkt-1", "ai_buy", ts=100.0))
    assert found is target


def test_find_pending_signal_returns_none_when_timestamp_differs() -> None:
    pending = _signal("mkt-1", "ai_buy", ts=100.0)
    orchestrator = SimpleNamespace(_pending_signals=[pending])

    found = find_pending_signal(orchestrator, _signal("mkt-1", "ai_buy", ts=101.0))
    assert found is None


def test_find_pending_signal_overlay_returns_copy_of_payload() -> None:
    overlay = {"applied": True, "boost": 1.2}
    pending = _signal("mkt-1", "ai_buy", ts=42.0, overlay=overlay)
    orchestrator = SimpleNamespace(_pending_signals=[pending])

    result = find_pending_signal_overlay(
        orchestrator, _signal("mkt-1", "ai_buy", ts=42.0)
    )
    assert result == overlay
    result["applied"] = False  # mutating result must not bleed into pending
    assert pending.payload["research_overlay"]["applied"] is True


def test_find_pending_signal_overlay_returns_empty_when_missing() -> None:
    orchestrator = SimpleNamespace(_pending_signals=[])
    assert find_pending_signal_overlay(orchestrator, _signal("x", "y", 1.0)) == {}
