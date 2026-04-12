"""main_loop 辅助逻辑测试：dry-run 不应算作真实执行成功."""

from pathlib import Path
import time

from polymarket_arb.dashboard_api import _enrich_ai_decision
from polymarket_arb.main_loop import (
    _estimate_ai_trade_outcome,
    _focus_keywords,
    _find_pending_signal_overlay,
    _build_ws_status,
    _create_research_signal_service,
    _is_live_execution_success,
    _matches_focus,
    _refresh_market_universe,
    _select_event_candidates,
    _select_scan_candidates,
    _TELEMETRY_HEARTBEAT_SEC,
    _serialize_opportunity_event,
    _serialize_trade_execution,
    _start_ws_feed,
)
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.models import EventInfo, MarketInfo, OrderBookLevel, OrderSide, TokenInfo, TradeRecord, TradeStatus
from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator, StrategySignal, StrategyTier
from polymarket_arb.tick_recorder import TickRecorder
from polymarket_arb.dashboard_api import DashboardState

from tests.test_execution_engine import _make_opp
from tests.conftest import make_test_config


class _StubExecutor:
    def __init__(self, result: bool):
        self.result = result

    def is_successful_execution(self, opp, trades):
        return self.result


def test_is_live_execution_success_returns_false_in_dry_run():
    config = make_test_config(dry_run=True)
    assert _is_live_execution_success(config, _StubExecutor(True), _make_opp(), []) is False


def test_is_live_execution_success_uses_executor_in_live_mode():
    config = make_test_config(dry_run=False)
    assert _is_live_execution_success(config, _StubExecutor(True), _make_opp(), []) is True


def test_dashboard_state_can_store_initializing_ws_phase():
    state = DashboardState()
    state.update(
        ws_status={
            "enabled": True,
            "connected": False,
            "phase": "initializing",
            "subscribed_tokens": 0,
            "scanned_orderbooks": 25,
            "scanned_events": 3,
        }
    )
    snap = state.snapshot()

    assert snap["ws_status"]["phase"] == "initializing"
    assert snap["ws_status"]["scanned_orderbooks"] == 25


def test_build_ws_status_requires_first_book_before_connected():
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    store.set_connected(True)

    initializing = _build_ws_status(
        config=make_test_config(ws_enabled=True),
        enhanced_store=store,
        ws_target_ids=["yes", "no"],
        phase_hint="scan_complete",
    )
    assert initializing["connected"] is False
    assert initializing["phase"] == "initializing"

    store.update_yes([(0.4, 10)], [(0.42, 12)], 1000)
    store.update_no([(0.57, 11)], [(0.60, 9)], 1000)
    store.set_connected(True)
    connected = _build_ws_status(
        config=make_test_config(ws_enabled=True),
        enhanced_store=store,
        ws_target_ids=["yes", "no"],
        phase_hint="scan_complete",
    )
    assert connected["connected"] is True
    assert connected["phase"] == "connected"


def test_start_ws_feed_registers_tick_recorder_and_writes_snapshot(tmp_path: Path, monkeypatch):
    class _FakeWebSocketFeed:
        def __init__(self, mirror, ws_url=None, enhanced_store=None):
            self.mirror = mirror
            self.enhanced_store = enhanced_store
            self.subscribed = []

        def subscribe(self, token_ids):
            self.subscribed.extend(token_ids)

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr("polymarket_arb.main_loop.WebSocketFeed", _FakeWebSocketFeed)

    recorder = TickRecorder(output_dir=str(tmp_path), enabled=True)
    store = EnhancedBookStore()
    market = MarketInfo(
        condition_id="cond1",
        question="Will BTC go up?",
        slug="btc-up",
        tokens=[
            TokenInfo(token_id="yes-token", outcome="Yes"),
            TokenInfo(token_id="no-token", outcome="No"),
        ],
    )

    _, mirror = _start_ws_feed([market], store, recorder)
    mirror.apply_snapshot(
        "yes-token",
        bids=[{"price": 0.44, "size": 10}],
        asks=[{"price": 0.45, "size": 12}],
    )
    mirror.stop()
    recorder.close()

    files = list(tmp_path.glob("*.ndjson"))
    assert files, "tick recorder should write at least one NDJSON file"
    content = files[0].read_text(encoding="utf-8")
    assert "yes-token" in content


def test_create_research_signal_service_gracefully_disables_on_import_error(monkeypatch):
    config = make_test_config(research_signal_enabled=True)

    def _raise_import_error(name, package=None):
        if name == "research_signal.service":
            raise ImportError("broken module")
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr("polymarket_arb.main_loop.importlib.import_module", _raise_import_error)

    assert _create_research_signal_service(config) is None


def test_build_ws_status_reads_snapshot_only_once(monkeypatch):
    store = EnhancedBookStore()
    store.set_market("m1", "yes", "no")
    store.set_connected(True)
    store.update_yes([(0.4, 10)], [(0.42, 10)], 1000)
    store.update_no([(0.57, 10)], [(0.60, 10)], 1000)

    call_count = {"count": 0}
    original_snapshot = store.snapshot

    def counting_snapshot():
        call_count["count"] += 1
        return original_snapshot()

    monkeypatch.setattr(store, "snapshot", counting_snapshot)

    status = _build_ws_status(
        config=make_test_config(ws_enabled=True),
        enhanced_store=store,
        ws_target_ids=["yes", "no"],
        phase_hint="scan_complete",
    )

    assert status["connected"] is True
    assert call_count["count"] == 1


def test_estimate_ai_trade_outcome_uses_realized_cost_on_failed_partial_fill():
    opp = _make_opp()
    trades = [
        TradeRecord(
            trade_id="t1",
            arb_id="a1",
            token_id="yes",
            condition_id="c1",
            side=OrderSide.BUY,
            price=0.45,
            size=5,
            status=TradeStatus.PARTIAL,
            fill_size=2,
            economic_cost=0.45,
        ),
        TradeRecord(
            trade_id="t2",
            arb_id="a1",
            token_id="no",
            condition_id="c1",
            side=OrderSide.BUY,
            price=0.50,
            size=5,
            status=TradeStatus.FAILED,
            fill_size=0,
            economic_cost=0.50,
        ),
    ]

    outcome = _estimate_ai_trade_outcome(opp, trades, arb_success=False, adj_size=5)

    assert outcome == -0.9


def test_estimate_ai_trade_outcome_uses_smallest_filled_leg_on_success():
    opp = _make_opp()
    trades = [
        TradeRecord(
            trade_id="t1",
            arb_id="a1",
            token_id="yes",
            condition_id="c1",
            side=OrderSide.BUY,
            price=0.45,
            size=5,
            status=TradeStatus.FILLED,
            fill_size=3,
            economic_cost=0.45,
        ),
        TradeRecord(
            trade_id="t2",
            arb_id="a1",
            token_id="no",
            condition_id="c1",
            side=OrderSide.BUY,
            price=0.50,
            size=5,
            status=TradeStatus.FILLED,
            fill_size=5,
            economic_cost=0.50,
        ),
    ]

    outcome = _estimate_ai_trade_outcome(opp, trades, arb_success=True, adj_size=5)

    assert outcome == opp.net_edge * 3


def test_find_pending_signal_overlay_reads_overlay_from_orchestrator_copy():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="ai_buy_yes",
        market_id="market-1",
        description="signal",
        expected_edge=10.0,
        confidence=0.6,
        recommended_size_usdc=50.0,
        payload={"action": "BUY_YES"},
    )

    orchestrator.submit_signal(signal)

    overlay = _find_pending_signal_overlay(orchestrator, signal)

    assert overlay["applied"] is False
    assert signal.payload == {"action": "BUY_YES"}


def test_enrich_ai_decision_overrides_stale_current_price_from_market_catalog():
    decision = {
        "market_id": "market-1",
        "action": "BUY_YES",
        "decision_price": 0.44,
        "current_price": 0.44,
        "timestamp": 0.0,
    }
    market_catalog = {
        "market-1": {
            "question": "Will BTC go up?",
            "yes_price": 0.51,
            "volume_24h": 1000.0,
            "liquidity": 2000.0,
        }
    }

    enriched = _enrich_ai_decision(decision, market_catalog)

    assert enriched["current_price"] == 0.51


def test_serialize_opportunity_event_includes_leg_details():
    opp = _make_opp()

    payload = _serialize_opportunity_event(opp, stage="detected")

    assert payload["stage"] == "detected"
    assert payload["event_id"] == opp.event_id
    assert len(payload["legs"]) == 2
    assert payload["legs"][0]["token_id"] == "yes"


def test_serialize_trade_execution_includes_trade_rows():
    opp = _make_opp()
    trades = [
        TradeRecord(
            trade_id="t1",
            arb_id="a1",
            token_id="yes",
            condition_id="c1",
            side=OrderSide.BUY,
            price=0.45,
            size=5,
            status=TradeStatus.FILLED,
            fill_price=0.45,
            fill_size=5,
            economic_cost=0.45,
            order_id="oid-1",
        )
    ]

    payload = _serialize_trade_execution(opp, trades, arb_success=False, adj_size=5)

    assert payload["event_id"] == opp.event_id
    assert payload["trades"][0]["trade_id"] == "t1"
    assert payload["trades"][0]["fill_size"] == 5


def test_select_scan_candidates_prefers_binary_liquid_markets():
    markets = [
        MarketInfo(condition_id="c1", question="q1", slug="q1", tokens=[TokenInfo("t1", "Yes")], volume_24h=500, liquidity=500),
        MarketInfo(condition_id="c2", question="q2", slug="q2", tokens=[TokenInfo("t2a", "Yes"), TokenInfo("t2b", "No")], volume_24h=400, liquidity=400),
        MarketInfo(condition_id="c3", question="q3", slug="q3", tokens=[TokenInfo("t3a", "Yes"), TokenInfo("t3b", "No")], volume_24h=300, liquidity=300),
    ]

    selected = _select_scan_candidates(markets, 2)

    assert [market.condition_id for market in selected] == ["c2", "c3"]


def test_select_event_candidates_prefers_high_volume_events():
    events = [
        EventInfo(event_id="e1", slug="e1", title="e1", markets=[MarketInfo(condition_id="c1", question="q1", slug="q1", tokens=[TokenInfo("t1", "Yes")], volume_24h=100, liquidity=100)]),
        EventInfo(event_id="e2", slug="e2", title="e2", markets=[MarketInfo(condition_id="c2", question="q2", slug="q2", tokens=[TokenInfo("t2", "Yes")], volume_24h=500, liquidity=200)]),
    ]

    selected = _select_event_candidates(events, 1)

    assert [event.event_id for event in selected] == ["e2"]


def test_refresh_market_universe_reuses_cache_before_interval():
    class _Scanner:
        def __init__(self):
            self.market_calls = 0
            self.event_calls = 0

        def fetch_active_markets(self, **kwargs):
            self.market_calls += 1
            return [MarketInfo(condition_id="c1", question="q1", slug="q1", tokens=[TokenInfo("t1", "Yes")])]

        def fetch_active_events(self, limit=0):
            self.event_calls += 1
            return [EventInfo(event_id="e1", slug="e1", title="e1")]

    scanner = _Scanner()
    config = make_test_config(market_universe_refresh_sec=600.0)
    cached_markets = [MarketInfo(condition_id="cached", question="cached", slug="cached", tokens=[TokenInfo("t", "Yes")])]
    cached_events = [EventInfo(event_id="cached-e", slug="cached-e", title="cached-e")]

    markets, events, last_ts, refreshed = _refresh_market_universe(
        scanner=scanner,
        config=config,
        cached_markets=cached_markets,
        cached_events=cached_events,
        last_refresh_ts=time.time(),
    )

    assert markets == cached_markets
    assert events == cached_events
    assert refreshed is False
    assert scanner.market_calls == 0
    assert scanner.event_calls == 0


def test_focus_keywords_parsing_and_matching():
    keywords = _focus_keywords("btc, crypto , politics")

    assert keywords == ["btc", "crypto", "politics"]
    assert _matches_focus("will btc go up this week", keywords) is True
    assert _matches_focus("federal reserve decision", keywords) is False


def test_select_scan_candidates_can_filter_by_focus_keywords():
    markets = [
        MarketInfo(condition_id="c1", question="Will BTC hit 120k?", slug="btc-120k", tokens=[TokenInfo("t1", "Yes"), TokenInfo("t2", "No")], volume_24h=1000, liquidity=1000),
        MarketInfo(condition_id="c2", question="Will Fed cut rates?", slug="fed-rates", tokens=[TokenInfo("t3", "Yes"), TokenInfo("t4", "No")], volume_24h=2000, liquidity=2000),
    ]

    selected = _select_scan_candidates(markets, 10, focus_keywords=["btc"])

    assert [market.condition_id for market in selected] == ["c1"]


def test_select_event_candidates_can_filter_by_focus_keywords():
    events = [
        EventInfo(event_id="e1", slug="btc", title="BTC event", markets=[MarketInfo(condition_id="c1", question="Will BTC hit 120k?", slug="btc-120k", tokens=[TokenInfo("t1", "Yes")])]),
        EventInfo(event_id="e2", slug="fed", title="Fed event", markets=[MarketInfo(condition_id="c2", question="Will Fed cut rates?", slug="fed-rates", tokens=[TokenInfo("t2", "Yes")])]),
    ]

    selected = _select_event_candidates(events, 10, focus_keywords=["btc"])

    assert [event.event_id for event in selected] == ["e1"]


def test_telemetry_heartbeat_constant_is_one_minute():
    assert _TELEMETRY_HEARTBEAT_SEC == 60.0
