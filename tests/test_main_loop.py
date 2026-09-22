"""main_loop 辅助逻辑测试：dry-run 不应算作真实执行成功."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.main_loop import (
    _BoundedDedupeSet,
    _build_cycle_summary_payload,
    _build_dashboard_trade_rows,
    _build_directional_opportunity_from_signal,
    _emit_cycle_metrics,
    _collect_cross_platform_strategy_signals,
    _collect_maker_strategy_signals,
    _prime_candidate_orderbooks,
    _advance_research_refresh,
    _ResearchRefreshState,
    _collect_statistical_strategy_signals,
    _evaluate_t2_market_quality,
    main,
    _estimate_trade_outcome,
    _build_run_instance_id,
    _build_t2_related_market_context,
    _extract_market_deadline,
    _extract_market_temporal_stem,
    _execute_strategy_signal,
    _focus_keywords,
    _find_pending_signal,
    _find_pending_signal_overlay,
    _serialize_strategy_signal,
    _build_ws_status,
    _create_research_signal_service,
    _is_live_execution_success,
    _apply_maker_fill_to_inventory,
    _matches_focus,
    _merge_focus_event_markets,
    _refresh_market_universe,
    _select_event_candidates,
    _select_scan_candidates,
    _TELEMETRY_HEARTBEAT_SEC,
    _serialize_opportunity_event,
    _serialize_trade_execution,
)
from polymarket_arb.main_helpers.cycle_runners import start_ws_feed as _start_ws_feed
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.models import EventInfo, MarketInfo, OrderBookLevel, OrderSide, ResearchSignal, ResearchSignalReport, TokenInfo, TradeRecord, TradeStatus
from polymarket_arb.strategies.cross_platform import CrossPlatformOpportunity, CrossPlatformPair
from polymarket_arb.strategies.maker_strategy import MakerStrategy
from polymarket_arb.strategies.statistical_model import StatisticalMispricingDetector
from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator, StrategySignal, StrategyTier
from polymarket_arb.tick_recorder import TickRecorder
from polymarket_arb.dashboard_api import DashboardState
from research_signal.service import ResearchSignalService

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


def test_bounded_dedupe_set_caps_memory_and_allows_evicted_key_again():
    seen = _BoundedDedupeSet(max_keys=2)

    assert seen.add_new(("a",)) is True
    assert seen.add_new(("a",)) is False
    assert seen.add_new(("b",)) is True
    assert seen.add_new(("c",)) is True
    assert seen.add_new(("a",)) is True


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

    # The helper now lives in `main_helpers.cli_setup`; `main_loop` keeps
    # only the underscore-aliased re-import. Patching the original module
    # ensures the import failure is exercised wherever the real call site is.
    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cli_setup.importlib.import_module",
        _raise_import_error,
    )

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


def test_estimate_trade_outcome_uses_realized_cost_on_failed_partial_fill():
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

    outcome = _estimate_trade_outcome(opp, trades, arb_success=False, adj_size=5)

    assert outcome == -0.9


def test_estimate_trade_outcome_uses_smallest_filled_leg_on_success():
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

    outcome = _estimate_trade_outcome(opp, trades, arb_success=True, adj_size=5)

    assert outcome == opp.net_edge * 3


def test_find_pending_signal_overlay_reads_overlay_from_orchestrator_copy():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="test_buy_yes",
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


def test_serialize_trade_execution_marks_dry_run_fills_as_simulated_success():
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
            simulated=True,
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
            fill_price=0.50,
            fill_size=5,
            economic_cost=0.50,
            simulated=True,
        ),
    ]

    payload = _serialize_trade_execution(opp, trades, arb_success=False, adj_size=5)

    assert payload["simulated"] is True
    assert payload["live_execution_success"] is False
    assert payload["arb_success"] is True
    assert payload["trade_outcome_estimate"] == opp.net_edge * 5


def test_build_dashboard_trade_rows_marks_simulated_execution_mode():
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
            simulated=True,
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
            fill_price=0.50,
            fill_size=5,
            economic_cost=0.50,
            simulated=True,
        ),
    ]

    rows = _build_dashboard_trade_rows(
        opp=opp,
        trades=trades,
        live_execution_success=False,
        dashboard_execution_success=True,
    )

    assert len(rows) == 2
    assert rows[0]["mode"] == "simulated"
    assert rows[0]["execution_success"] is True
    assert rows[0]["event_title"] == opp.event_title
    assert rows[0]["expected_profit"] == opp.net_edge * 5


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


def test_prime_candidate_orderbooks_dedupes_market_and_event_tokens():
    markets = [
        MarketInfo(
            condition_id="cond-1",
            question="Will BTC rise?",
            slug="btc-rise",
            tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        ),
        MarketInfo(
            condition_id="cond-2",
            question="Will ETH rise?",
            slug="eth-rise",
            tokens=[TokenInfo("yes-2", "Yes"), TokenInfo("no-2", "No")],
        ),
    ]
    events = [
        EventInfo(
            event_id="event-1",
            slug="crypto-event",
            title="Crypto event",
            markets=[
                markets[0],
                MarketInfo(
                    condition_id="cond-3",
                    question="Will SOL rise?",
                    slug="sol-rise",
                    tokens=[TokenInfo("yes-3", "Yes"), TokenInfo("no-3", "No")],
                ),
            ],
        )
    ]

    class _StubOrderBookAnalyzer:
        def __init__(self):
            self.calls = []

        def batch_get_snapshots(self, token_ids, delay=0.0):
            self.calls.append((list(token_ids), delay))
            return {}

    ob_analyzer = _StubOrderBookAnalyzer()

    _prime_candidate_orderbooks(
        candidate_markets=markets,
        candidate_events=events,
        ob_analyzer=ob_analyzer,
    )

    assert ob_analyzer.calls == [
        (["yes-1", "no-1", "yes-2", "no-2", "yes-3", "no-3"], 0.0)
    ]


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


def test_matches_focus_does_not_match_partial_word_fragments():
    keywords = _focus_keywords("eth,sol,arb")

    assert _matches_focus("Will ETH be above 3000 by Friday?", keywords) is True
    assert _matches_focus("Will Solana ETF launch this year?", keywords) is True
    assert _matches_focus("Will Netherlands win the 2026 FIFA World Cup?", keywords) is False
    assert _matches_focus("Which Caribbean team advances?", keywords) is False
    assert _matches_focus("Will Dominic Solanke score 20 goals this season?", keywords) is False


def test_select_scan_candidates_can_filter_by_focus_keywords():
    markets = [
        MarketInfo(condition_id="c1", question="Will BTC hit 120k?", slug="btc-120k", tokens=[TokenInfo("t1", "Yes"), TokenInfo("t2", "No")], volume_24h=1000, liquidity=1000),
        MarketInfo(condition_id="c2", question="Will Fed cut rates?", slug="fed-rates", tokens=[TokenInfo("t3", "Yes"), TokenInfo("t4", "No")], volume_24h=2000, liquidity=2000),
    ]

    selected = _select_scan_candidates(markets, 10, focus_keywords=["btc"])

    assert [market.condition_id for market in selected] == ["c1"]


def test_select_scan_candidates_can_filter_by_inherited_event_metadata():
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will it happen by Friday?",
            slug="happen-by-friday",
            event_title="Bitcoin treasury event",
            event_slug="bitcoin-treasury-event",
            event_ticker="BTC-TREASURY",
            tokens=[TokenInfo("t1", "Yes"), TokenInfo("t2", "No")],
            volume_24h=1000,
            liquidity=1000,
        ),
        MarketInfo(
            condition_id="c2",
            question="Will Fed cut rates?",
            slug="fed-rates",
            event_title="Fed event",
            tokens=[TokenInfo("t3", "Yes"), TokenInfo("t4", "No")],
            volume_24h=2000,
            liquidity=2000,
        ),
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


def test_merge_focus_event_markets_adds_binary_markets_from_focus_events():
    selected_markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC hit 120k?",
            slug="btc-120k",
            tokens=[TokenInfo("t1", "Yes"), TokenInfo("t2", "No")],
            volume_24h=1000,
            liquidity=1000,
        )
    ]
    focus_events = [
        EventInfo(
            event_id="e1",
            slug="bitcoin-event",
            title="Bitcoin event",
            markets=[
                MarketInfo(
                    condition_id="c2",
                    question="Will it happen by Friday?",
                    slug="happen-by-friday",
                    tokens=[TokenInfo("t3", "Yes"), TokenInfo("t4", "No")],
                    volume_24h=800,
                    liquidity=900,
                ),
                MarketInfo(
                    condition_id="c3",
                    question="Will other outcome happen?",
                    slug="other-outcome",
                    tokens=[TokenInfo("t5", "Only")],
                    volume_24h=900,
                    liquidity=950,
                ),
            ],
        )
    ]

    merged = _merge_focus_event_markets(selected_markets, focus_events, max_count=10)

    assert [market.condition_id for market in merged] == ["c1", "c2"]
    assert merged[1].event_id == "e1"
    assert merged[1].event_slug == "bitcoin-event"
    assert merged[1].event_title == "Bitcoin event"


def test_telemetry_heartbeat_constant_is_one_minute():
    assert _TELEMETRY_HEARTBEAT_SEC == 60.0


def test_build_run_instance_id_includes_pid_and_timestamp(monkeypatch):
    monkeypatch.setattr("polymarket_arb.main_loop.os.getpid", lambda: 4321)

    run_id = _build_run_instance_id(now_ts=1_776_054_476.0)

    assert run_id.startswith("run-4321-")
    assert " " not in run_id


def test_build_cycle_summary_payload_includes_book_stats_and_timing():
    payload = _build_cycle_summary_payload(
        run_id="run-1",
        cycle=12,
        markets_scanned=6,
        universe_market_count=195,
        selected_event_count=4,
        theoretical_opportunities_total=1,
        live_successes_total=0,
        simulated_successes_total=2,
        live_submissions_total=1,
        simulated_submissions_total=3,
        ws_status={"connected": True, "subscribed_tokens": 12},
        research_count=3,
        daily_pnl=0.0,
        open_positions=0,
        focus_keywords=["btc"],
        book_stats={
            "requests": 20,
            "ws_hit": 9,
            "cache_hit": 7,
            "rest_fallback": 4,
            "rest_success": 3,
            "rest_error": 1,
            "missing_orderbook": 0,
            "cooldown_skip": 0,
        },
        timing_stats={
            "universe_refresh_sec": 1.2,
            "prewarm_sec": 0.4,
            "scan_cycle_sec": 2.3,
            "strategy_sec": 0.8,
            "total_cycle_sec": 5.1,
        },
    )

    assert payload["event"] == "cycle_summary"
    assert payload["book_stats"]["ws_hit"] == 9
    assert payload["book_stats"]["rest_error"] == 1
    assert payload["timing"]["prewarm_sec"] == 0.4
    assert payload["timing"]["total_cycle_sec"] == 5.1
    assert payload["simulated_successes_total"] == 2
    assert payload["live_submissions_total"] == 1
    assert payload["cycle_status"] == "ok"


def test_emit_cycle_metrics_writes_cycle_metrics_and_returns_payload():
    class _StubAnalyzer:
        def snapshot_stats(self, reset=False):
            assert reset is True
            return {
                "requests": 8,
                "ws_hit": 5,
                "cache_hit": 2,
                "rest_fallback": 1,
                "rest_success": 1,
                "rest_error": 0,
                "missing_orderbook": 0,
                "cooldown_skip": 0,
            }

    class _StubRecorder:
        def __init__(self):
            self.is_enabled = True
            self.events = []

        def write_event(self, category, payload):
            self.events.append((category, payload))

    recorder = _StubRecorder()
    payload = _emit_cycle_metrics(
        event_recorder=recorder,
        ob_analyzer=_StubAnalyzer(),
        cycle_perf_start=time.perf_counter() - 0.25,
        cycle_timing={"scan_cycle_sec": 0.2},
        run_id="run-1",
        cycle=9,
        markets_scanned=6,
        universe_market_count=195,
        selected_event_count=4,
        theoretical_opportunities_total=0,
        live_successes_total=0,
        simulated_successes_total=1,
        live_submissions_total=0,
        simulated_submissions_total=2,
        ws_status={"connected": True, "subscribed_tokens": 12},
        research_count=1,
        daily_pnl=0.0,
        open_positions=0,
        focus_keywords=["btc"],
    )

    assert payload["book_stats"]["ws_hit"] == 5
    assert payload["timing"]["total_cycle_sec"] >= 0.2
    assert payload["simulated_submissions_total"] == 2
    assert payload["cycle_status"] == "ok"
    assert recorder.events == [("cycle_metrics", payload)]


def test_emit_cycle_metrics_can_mark_error_cycles():
    class _StubAnalyzer:
        def snapshot_stats(self, reset=False):
            assert reset is True
            return {
                "requests": 1,
                "ws_hit": 0,
                "cache_hit": 0,
                "rest_fallback": 1,
                "rest_success": 0,
                "rest_error": 1,
                "missing_orderbook": 0,
                "cooldown_skip": 0,
            }

    class _StubRecorder:
        def __init__(self):
            self.is_enabled = True
            self.events = []

        def write_event(self, category, payload):
            self.events.append((category, payload))

    recorder = _StubRecorder()
    payload = _emit_cycle_metrics(
        event_recorder=recorder,
        ob_analyzer=_StubAnalyzer(),
        cycle_perf_start=time.perf_counter() - 0.1,
        cycle_timing={"scan_cycle_sec": 0.05},
        run_id="run-err",
        cycle=2,
        markets_scanned=0,
        universe_market_count=10,
        selected_event_count=0,
        theoretical_opportunities_total=0,
        live_successes_total=0,
        simulated_successes_total=0,
        live_submissions_total=0,
        simulated_submissions_total=0,
        ws_status={"connected": False, "subscribed_tokens": 0},
        research_count=0,
        daily_pnl=0.0,
        open_positions=0,
        focus_keywords=[],
        cycle_status="error",
    )

    assert payload["cycle_status"] == "error"
    assert recorder.events == [("cycle_metrics", payload)]


def test_advance_research_refresh_is_non_blocking_and_reuses_completed_report():
    class _SlowService:
        def collect_report(self, markets, window_sec):
            time.sleep(0.15)
            return ResearchSignalReport(
                generated_at=time.time(),
                window_sec=window_sec,
                market_count=len(markets),
                row_count=1,
                topic_count=1,
                signals=[
                    ResearchSignal(
                        topic_id="event:e1",
                        summary="BTC signal",
                        sources=["test"],
                        confidence=0.8,
                    )
                ],
            )

    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo("yes", "Yes"), TokenInfo("no", "No")],
            event_id="e1",
        )
    ]
    state = _ResearchRefreshState()

    with ThreadPoolExecutor(max_workers=1) as executor:
        start = time.perf_counter()
        report = _advance_research_refresh(
            research_signal_service=_SlowService(),
            research_executor=executor,
            state=state,
            universe_markets=markets,
            scanned_markets=[],
            max_items=5,
            window_sec=86400,
            refresh_interval_sec=300.0,
            now_ts=1.0,
        )
        elapsed = time.perf_counter() - start

        assert report is None
        assert state.pending_future is not None
        assert elapsed < 0.1

        state.pending_future.result(timeout=1.0)
        report = _advance_research_refresh(
            research_signal_service=_SlowService(),
            research_executor=executor,
            state=state,
            universe_markets=markets,
            scanned_markets=[],
            max_items=5,
            window_sec=86400,
            refresh_interval_sec=300.0,
            now_ts=2.0,
        )

    assert report is not None
    assert report.signals[0].summary == "BTC signal"


def test_research_stale_rows_are_cleared_when_no_fresh_report_is_available():
    scanner = MarketScanner(make_test_config())
    markets = [
        MarketInfo(
            condition_id="c1",
            question="Will BTC go up this week?",
            slug="btc-up",
            tokens=[TokenInfo("yes", "Yes"), TokenInfo("no", "No")],
            event_id="e1",
            raw={"research_signals": [{"topic_id": "stale-topic"}]},
        )
    ]

    scanner.enrich_markets_with_research(
        markets,
        SimpleNamespace(
            attach_to_markets=lambda markets, signals: ResearchSignalService().attach_to_markets(markets, signals)
        ),  # type: ignore[arg-type]
        signals=[],
    )

    assert markets[0].raw["research_signals"] == []


def test_collect_statistical_strategy_signals_scan_multiple_candidate_markets():
    markets = [
        MarketInfo(
            condition_id="cond-1",
            question="Will BTC rise?",
            slug="btc-rise",
            tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        ),
        MarketInfo(
            condition_id="cond-2",
            question="Will ETH rise?",
            slug="eth-rise",
            tokens=[TokenInfo("yes-2", "Yes"), TokenInfo("no-2", "No")],
        ),
    ]

    class _StubOrderBookAnalyzer:
        def __init__(self, snapshots):
            self.snapshots = snapshots

        def get_snapshot(self, token_id):
            return self.snapshots.get(token_id)

    class _Snapshot:
        def __init__(self, best_bid, best_ask, bid_size, ask_size, tick_size=0.01):
            self.best_bid = best_bid
            self.best_ask = best_ask
            self.tick_size = tick_size
            self.bids = [type("Level", (), {"price": best_bid, "size": bid_size})()]
            self.asks = [type("Level", (), {"price": best_ask, "size": ask_size})()]

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

        @property
        def best_bid_size(self):
            return self.bids[0].size

        @property
        def best_ask_size(self):
            return self.asks[0].size

    ob_analyzer = _StubOrderBookAnalyzer(
        {
            "yes-1": _Snapshot(0.399, 0.401, 900, 100),
            "no-1": _Snapshot(0.599, 0.601, 100, 900),
            "yes-2": _Snapshot(0.49, 0.50, 200, 200),
            "no-2": _Snapshot(0.50, 0.51, 200, 200),
        }
    )
    detector = StatisticalMispricingDetector(min_deviation=0.005, min_confidence=0.1)

    signals = _collect_statistical_strategy_signals(
        config=make_test_config(default_order_size_usdc=7.5),
        candidate_markets=markets,
        ob_analyzer=ob_analyzer,
        detector=detector,
    )

    assert len(signals) == 1
    assert signals[0].tier == StrategyTier.STATISTICAL_ARB
    assert signals[0].signal_type == "statistical_buy_yes"
    assert signals[0].market_id == "cond-1"
    assert signals[0].recommended_size_usdc == 7.5
    assert signals[0].expected_edge > 0
    assert signals[0].payload["quality"]["passes"] is True


def test_collect_statistical_strategy_signals_filters_poor_quality_markets():
    markets = [
        MarketInfo(
            condition_id="cond-1",
            question="Will BTC rise?",
            slug="btc-rise",
            tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        )
    ]

    class _StubOrderBookAnalyzer:
        def __init__(self, snapshots):
            self.snapshots = snapshots

        def get_snapshot(self, token_id):
            return self.snapshots.get(token_id)

    class _Snapshot:
        def __init__(self, best_bid, best_ask, bid_size, ask_size, tick_size=0.01):
            self.best_bid = best_bid
            self.best_ask = best_ask
            self.tick_size = tick_size
            self.bids = [type("Level", (), {"price": best_bid, "size": bid_size})()]
            self.asks = [type("Level", (), {"price": best_ask, "size": ask_size})()]

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

        @property
        def spread(self):
            return self.best_ask - self.best_bid

        @property
        def best_bid_size(self):
            return self.bids[0].size

        @property
        def best_ask_size(self):
            return self.asks[0].size

    ob_analyzer = _StubOrderBookAnalyzer(
        {
            "yes-1": _Snapshot(0.30, 0.40, 900, 50),
            "no-1": _Snapshot(0.59, 0.60, 50, 900),
        }
    )
    detector = StatisticalMispricingDetector(min_deviation=0.005, min_confidence=0.1)

    signals = _collect_statistical_strategy_signals(
        config=make_test_config(t2_max_spread_bps=100.0, t2_min_top_depth=100.0, t2_max_complement_error_bps=200.0),
        candidate_markets=markets,
        ob_analyzer=ob_analyzer,
        detector=detector,
    )

    assert signals == []


def test_extract_market_temporal_stem_and_deadline_for_ladder_questions():
    question = "Will Bitcoin hit $150k by December 31, 2026?"

    stem = _extract_market_temporal_stem(question)
    deadline = _extract_market_deadline(question)

    assert stem == "bitcoin hit $150k"
    assert deadline is not None
    assert (deadline.year, deadline.month, deadline.day) == (2026, 12, 31)


def test_build_t2_related_market_context_adds_time_ladder_bounds():
    markets = [
        MarketInfo(
            condition_id="cond-early",
            question="Will Bitcoin hit $150k by June 30, 2026?",
            slug="btc-150k-june",
            event_id="event-btc-150k",
            tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        ),
        MarketInfo(
            condition_id="cond-late",
            question="Will Bitcoin hit $150k by December 31, 2026?",
            slug="btc-150k-dec",
            event_id="event-btc-150k",
            tokens=[TokenInfo("yes-2", "Yes"), TokenInfo("no-2", "No")],
        ),
    ]

    class _StubOrderBookAnalyzer:
        def __init__(self, snapshots):
            self.snapshots = snapshots

        def get_snapshot(self, token_id):
            return self.snapshots.get(token_id)

    class _Snapshot:
        def __init__(self, best_bid, best_ask):
            self.best_bid = best_bid
            self.best_ask = best_ask

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

    context = _build_t2_related_market_context(
        markets,
        _StubOrderBookAnalyzer(
            {
                "yes-1": _Snapshot(0.39, 0.41),
                "yes-2": _Snapshot(0.59, 0.61),
            }
        ),
    )

    assert context["cond-early"]["cond-late"]["relation"] == "upper_bound"
    assert context["cond-late"]["cond-early"]["relation"] == "lower_bound"


def test_collect_statistical_strategy_signals_detects_ladder_inconsistency():
    markets = [
        MarketInfo(
            condition_id="cond-early",
            question="Will Bitcoin hit $150k by June 30, 2026?",
            slug="btc-150k-june",
            event_id="event-btc-150k",
            tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        ),
        MarketInfo(
            condition_id="cond-late",
            question="Will Bitcoin hit $150k by December 31, 2026?",
            slug="btc-150k-dec",
            event_id="event-btc-150k",
            tokens=[TokenInfo("yes-2", "Yes"), TokenInfo("no-2", "No")],
        ),
    ]

    class _StubOrderBookAnalyzer:
        def __init__(self, snapshots):
            self.snapshots = snapshots

        def get_snapshot(self, token_id):
            return self.snapshots.get(token_id)

    class _Snapshot:
        def __init__(self, best_bid, best_ask, bid_size, ask_size, tick_size=0.01):
            self.best_bid = best_bid
            self.best_ask = best_ask
            self.tick_size = tick_size
            self.bids = [type("Level", (), {"price": best_bid, "size": bid_size})()]
            self.asks = [type("Level", (), {"price": best_ask, "size": ask_size})()]

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

        @property
        def spread(self):
            return self.best_ask - self.best_bid

        @property
        def best_bid_size(self):
            return self.bids[0].size

        @property
        def best_ask_size(self):
            return self.asks[0].size

    ob_analyzer = _StubOrderBookAnalyzer(
        {
            "yes-1": _Snapshot(0.93, 0.95, 500, 500),
            "no-1": _Snapshot(0.05, 0.07, 500, 500),
            "yes-2": _Snapshot(0.53, 0.55, 500, 500),
            "no-2": _Snapshot(0.45, 0.47, 500, 500),
        }
    )
    detector = StatisticalMispricingDetector(min_deviation=0.01, min_confidence=0.1)

    signals = _collect_statistical_strategy_signals(
        config=make_test_config(default_order_size_usdc=7.5, t2_max_spread_bps=1000.0, t2_min_top_depth=100.0),
        candidate_markets=markets,
        ob_analyzer=ob_analyzer,
        detector=detector,
    )

    signal_types = {signal.signal_type for signal in signals}
    assert "statistical_buy_yes" in signal_types
    assert any(signal.payload["related_context_count"] > 0 for signal in signals)


def test_evaluate_t2_market_quality_reports_reasons():
    class _Snapshot:
        def __init__(self, best_bid, best_ask, bid_size, ask_size):
            self.best_bid = best_bid
            self.best_ask = best_ask
            self.bids = [type("Level", (), {"price": best_bid, "size": bid_size})()]
            self.asks = [type("Level", (), {"price": best_ask, "size": ask_size})()]

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

        @property
        def spread(self):
            return self.best_ask - self.best_bid

        @property
        def best_ask_size(self):
            return self.asks[0].size

    quality = _evaluate_t2_market_quality(
        config=make_test_config(t2_max_spread_bps=80.0, t2_min_top_depth=100.0, t2_max_complement_error_bps=150.0),
        snap=_Snapshot(0.30, 0.40, 500, 50),
        no_snap=_Snapshot(0.59, 0.60, 500, 90),
    )

    assert quality["passes"] is False
    assert "spread_too_wide" in quality["reasons"]
    assert "top_depth_too_low" in quality["reasons"]


def test_build_directional_opportunity_uses_clob_fee_shape():
    class _Snapshot:
        def __init__(self, best_ask):
            self.best_ask = best_ask
            self.best_bid = best_ask - 0.01
            self.asks = [type("Level", (), {"price": best_ask, "size": 100.0})()]
            self.bids = [type("Level", (), {"price": self.best_bid, "size": 100.0})()]

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

        @property
        def best_ask_size(self):
            return self.asks[0].size

    class _StubOrderBookAnalyzer:
        def get_snapshot(self, token_id):
            return _Snapshot(0.50)

        def get_executable_ask_price(self, token_id, target_size):
            return (0.50, target_size)

    market = MarketInfo(
        condition_id="cond-1",
        question="Will BTC rise?",
        slug="btc-rise",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
    )
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="cond-1",
        description="test",
        expected_edge=200.0,
        confidence=0.8,
        recommended_size_usdc=1.0,
        payload={"deviation": 0.08, "model_prob": 0.58, "market_prob": 0.50},
    )

    opportunity, target_size, reason = _build_directional_opportunity_from_signal(
        config=make_test_config(polymarket_taker_fee_rate=0.072),
        signal=signal,
        market=market,
        ob_analyzer=_StubOrderBookAnalyzer(),
    )

    # Both legs are taker: entry is charged at the fill price, exit at the
    # fair value the position is expected to be closed into.
    entry_fee = 0.072 * 0.50 * 0.50
    exit_fee = 0.072 * 0.58 * 0.42
    assert reason == ""
    assert opportunity is not None
    assert target_size == pytest.approx(2.0)
    assert opportunity.net_edge == pytest.approx(0.08 - entry_fee - exit_fee)
    check = signal.payload["execution_check"]
    assert check["fee_estimate"] == pytest.approx(entry_fee)
    assert check["exit_fee_estimate"] == pytest.approx(exit_fee)
    assert check["roundtrip_fee"] == pytest.approx(entry_fee + exit_fee)
    assert check["net_edge_bps"] == pytest.approx((0.08 - entry_fee - exit_fee) * 10_000.0)


def test_live_directional_opportunity_rejects_edge_below_live_buffer():
    class _Snapshot:
        best_ask = 0.50
        best_bid = 0.49
        asks = [type("Level", (), {"price": 0.50, "size": 100.0})()]
        bids = [type("Level", (), {"price": 0.49, "size": 100.0})()]

    class _StubOrderBookAnalyzer:
        def get_snapshot(self, token_id):
            return _Snapshot()

        def get_executable_ask_price(self, token_id, target_size):
            return (0.50, target_size)

    market = MarketInfo(
        condition_id="cond-1",
        question="Will BTC rise?",
        slug="btc-rise",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
    )
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="cond-1",
        description="test",
        expected_edge=200.0,
        confidence=0.8,
        recommended_size_usdc=1.0,
        # Sized so the edge clears the round trip (~359 bps at rate 0.072)
        # by ~26 bps — through the fee gate, but short of the 30 bps live bar.
        payload={"deviation": 0.0385, "model_prob": 0.5385, "market_prob": 0.50},
    )

    opportunity, _, reason = _build_directional_opportunity_from_signal(
        config=make_test_config(
            dry_run=False,
            polymarket_taker_fee_rate=0.072,
            live_min_net_edge_bps=30.0,
            live_min_net_edge_usd=0.003,
        ),
        signal=signal,
        market=market,
        ob_analyzer=_StubOrderBookAnalyzer(),
    )

    assert opportunity is None
    assert reason == "live_edge_below_buffer"
    assert signal.payload["execution_check"]["reason"] == "live_edge_below_buffer"


def test_live_directional_opportunity_rejects_unhealthy_orderbook_feed():
    class _StubOrderBookAnalyzer:
        def feed_health(self, **kwargs):
            return {"healthy": False, "reason": "ws_hit_ratio_low"}

    market = MarketInfo(
        condition_id="cond-1",
        question="Will BTC rise?",
        slug="btc-rise",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
    )
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="cond-1",
        description="test",
        expected_edge=500.0,
        confidence=0.8,
        recommended_size_usdc=1.0,
        payload={"deviation": 0.05},
    )

    opportunity, _, reason = _build_directional_opportunity_from_signal(
        config=make_test_config(dry_run=False),
        signal=signal,
        market=market,
        ob_analyzer=_StubOrderBookAnalyzer(),
    )

    assert opportunity is None
    assert reason == "orderbook_feed_unhealthy"
    assert signal.payload["execution_check"]["feed_health_reason"] == "ws_hit_ratio_low"


def test_collect_maker_strategy_signals_use_snapshot_tick_size():
    class _Snapshot:
        def __init__(self, best_bid, best_ask, bid_size, ask_size, tick_size):
            self.best_bid = best_bid
            self.best_ask = best_ask
            self.tick_size = tick_size
            self.bids = [type("Level", (), {"price": best_bid, "size": bid_size})()]
            self.asks = [type("Level", (), {"price": best_ask, "size": ask_size})()]

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

    class _StubOrderBookAnalyzer:
        def __init__(self, snapshots):
            self.snapshots = snapshots

        def get_snapshot(self, token_id):
            return self.snapshots.get(token_id)

    markets = [
        MarketInfo(
            condition_id="cond-1",
            question="Will BTC rise?",
            slug="btc-rise",
            tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        )
    ]
    ob_analyzer = _StubOrderBookAnalyzer({"yes-1": _Snapshot(0.48, 0.50, 500, 500, 0.01)})
    fair_values = {"cond-1": 0.55}

    signals = _collect_maker_strategy_signals(
        candidate_markets=markets,
        ob_analyzer=ob_analyzer,
        maker_strategy=MakerStrategy(default_size=12.0),
        fair_values_by_market=fair_values,
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.tier == StrategyTier.MARKET_MAKING
    assert signal.signal_type == "maker_quote"
    assert signal.payload["quote"]["bid_price"] == 0.53
    assert signal.payload["quote"]["ask_price"] == 0.58
    assert signal.recommended_size_usdc == 12.0


def test_collect_maker_strategy_signals_can_compute_fair_value_without_t2_signal():
    class _Snapshot:
        def __init__(self, best_bid, best_ask, bid_size, ask_size, tick_size):
            self.best_bid = best_bid
            self.best_ask = best_ask
            self.tick_size = tick_size
            self.bids = [type("Level", (), {"price": best_bid, "size": bid_size})()]
            self.asks = [type("Level", (), {"price": best_ask, "size": ask_size})()]

        @property
        def mid(self):
            return (self.best_bid + self.best_ask) / 2.0

    class _StubOrderBookAnalyzer:
        def __init__(self, snapshots):
            self.snapshots = snapshots

        def get_snapshot(self, token_id):
            return self.snapshots.get(token_id)

    markets = [
        MarketInfo(
            condition_id="cond-1",
            question="Will BTC rise?",
            slug="btc-rise",
            tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        )
    ]
    ob_analyzer = _StubOrderBookAnalyzer({"yes-1": _Snapshot(0.48, 0.50, 900, 100, 0.01)})

    signals = _collect_maker_strategy_signals(
        candidate_markets=markets,
        ob_analyzer=ob_analyzer,
        maker_strategy=MakerStrategy(default_size=12.0),
        fair_values_by_market={},
        detector=StatisticalMispricingDetector(min_deviation=0.5, min_confidence=0.9),
    )

    assert len(signals) == 1
    assert signals[0].market_id == "cond-1"


def test_execute_maker_quote_converts_yes_ask_to_no_bid():
    class _StubExecutor:
        def __init__(self):
            self.submitted = None

        def ensure_sufficient_collateral(self, amount):
            return True, "", amount

        def submit_limit_order(self, **kwargs):
            self.submitted = kwargs
            return TradeRecord(
                trade_id="trade-1",
                arb_id="arb-1",
                token_id=kwargs["token_id"],
                condition_id=kwargs["condition_id"],
                side=kwargs["side"],
                price=kwargs["price"],
                size=kwargs["size"],
                status=TradeStatus.PENDING,
                simulated=True,
                post_only=kwargs["post_only"],
                order_type_name=kwargs["order_type_name"],
                economic_cost=kwargs["price"],
            )

    class _StubRiskManager:
        def __init__(self):
            self.opp = None

        def pre_trade_check(self, opp, size):
            self.opp = opp
            return True, "", size

        def record_execution(self, opp, trades, count_pending_as_failure=True):
            self.opp = opp

    class _StubEventRecorder:
        is_enabled = True

        def __init__(self):
            self.events = []

        def write_event(self, category, payload):
            self.events.append((category, payload))

    class _StubNotifier:
        def notify_trade_success(self, **kwargs):
            pass

        def notify_trade_failure(self, **kwargs):
            pass

    market = MarketInfo(
        condition_id="cond-1",
        question="Will BTC rise?",
        slug="btc-rise",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
    )
    signal = StrategySignal(
        tier=StrategyTier.MARKET_MAKING,
        signal_type="maker_quote",
        market_id="cond-1",
        description="ask-only maker quote",
        expected_edge=350.0,
        confidence=0.5,
        recommended_size_usdc=1.0,
        payload={
            "quote": {
                "bid_price": None,
                "ask_price": 0.07,
                "bid_size": 1.0,
                "ask_size": 1.0,
                "fair_value": 0.025,
                "spread": 0.0,
            }
        },
    )
    executor = _StubExecutor()
    event_recorder = _StubEventRecorder()
    orchestrator = StrategyOrchestrator(total_bankroll=3.0)

    executed, reason, delta = _execute_strategy_signal(
        signal=signal,
        config=make_test_config(dry_run=True),
        active_markets=[market],
        ob_analyzer=SimpleNamespace(),
        executor=executor,
        risk_mgr=_StubRiskManager(),
        orchestrator=orchestrator,
        dash_state=DashboardState(),
        event_recorder=event_recorder,
        maker_strategy=MakerStrategy(default_size=1.0),
        notifier=_StubNotifier(),
    )

    assert executed is True
    assert reason == ""
    assert delta.simulated_submissions == 1
    assert executor.submitted["token_id"] == "no-1"
    assert executor.submitted["outcome"] == "No"
    assert round(executor.submitted["price"], 6) == 0.93
    assert event_recorder.events[-1][1]["maker_side"] == "buy_no_from_yes_ask"
    assert orchestrator.get_status()["T3"]["current_exposure"] == 0.0


def test_execute_maker_quote_prefers_selling_existing_inventory():
    class _StubExecutor:
        def __init__(self):
            self.submitted = None

        def ensure_sufficient_collateral(self, amount):
            raise AssertionError("sell inventory should not require collateral")

        def submit_limit_order(self, **kwargs):
            self.submitted = kwargs
            return TradeRecord(
                trade_id="trade-1",
                arb_id="arb-1",
                token_id=kwargs["token_id"],
                condition_id=kwargs["condition_id"],
                side=kwargs["side"],
                price=kwargs["price"],
                size=kwargs["size"],
                status=TradeStatus.PENDING,
                simulated=True,
                post_only=kwargs["post_only"],
                order_type_name=kwargs["order_type_name"],
                economic_cost=kwargs["price"],
            )

    class _StubRiskManager:
        def pre_trade_check(self, opp, size):
            raise AssertionError("sell inventory should not open new risk")

        def record_execution(self, opp, trades, count_pending_as_failure=True):
            raise AssertionError("sell inventory should not increase exposure")

    class _StubEventRecorder:
        is_enabled = True

        def __init__(self):
            self.events = []

        def write_event(self, category, payload):
            self.events.append((category, payload))

    class _StubNotifier:
        def notify_trade_success(self, **kwargs):
            pass

        def notify_trade_failure(self, **kwargs):
            pass

    market = MarketInfo(
        condition_id="cond-1",
        question="Will BTC rise?",
        slug="btc-rise",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
    )
    signal = StrategySignal(
        tier=StrategyTier.MARKET_MAKING,
        signal_type="maker_quote",
        market_id="cond-1",
        description="two-sided maker quote",
        expected_edge=350.0,
        confidence=0.5,
        recommended_size_usdc=1.0,
        payload={
            "quote": {
                "bid_price": 0.02,
                "ask_price": 0.07,
                "bid_size": 1.0,
                "ask_size": 1.0,
                "fair_value": 0.025,
                "spread": 0.05,
            }
        },
    )
    maker_strategy = MakerStrategy(default_size=1.0)
    maker_strategy.update_inventory("yes-1", "BUY", 2.0)
    executor = _StubExecutor()
    event_recorder = _StubEventRecorder()

    executed, reason, delta = _execute_strategy_signal(
        signal=signal,
        config=make_test_config(dry_run=True),
        active_markets=[market],
        ob_analyzer=SimpleNamespace(),
        executor=executor,
        risk_mgr=_StubRiskManager(),
        orchestrator=StrategyOrchestrator(total_bankroll=3.0),
        dash_state=DashboardState(),
        event_recorder=event_recorder,
        maker_strategy=maker_strategy,
        notifier=_StubNotifier(),
    )

    assert executed is True
    assert reason == ""
    assert delta.simulated_submissions == 1
    assert executor.submitted["token_id"] == "yes-1"
    assert executor.submitted["side"] == OrderSide.SELL
    assert executor.submitted["price"] == 0.07
    assert event_recorder.events[-1][1]["maker_side"] == "sell_yes_inventory"
    assert event_recorder.events[-1][1]["side"] == "SELL"


def test_apply_maker_fill_to_inventory_accounts_only_new_fill_delta():
    maker_strategy = MakerStrategy(default_size=1.0)
    trade = TradeRecord(
        "trade-1",
        "arb-1",
        "yes-1",
        "cond-1",
        OrderSide.SELL,
        0.07,
        3.0,
        status=TradeStatus.PARTIAL,
        fill_size=1.0,
        post_only=True,
        order_type_name="GTC",
    )
    maker_strategy.update_inventory("yes-1", "BUY", 3.0)

    first_delta = _apply_maker_fill_to_inventory(maker_strategy, trade)
    trade.fill_size = 2.5
    second_delta = _apply_maker_fill_to_inventory(maker_strategy, trade)
    third_delta = _apply_maker_fill_to_inventory(maker_strategy, trade)

    assert first_delta == 1.0
    assert second_delta == 1.5
    assert third_delta == 0.0
    assert maker_strategy.get_inventory("yes-1") == 0.5


def test_collect_cross_platform_strategy_signals_maps_opportunities():
    class _StubScanner:
        def scan(self):
            pair = CrossPlatformPair(
                pair_id="pair-1",
                event_description="BTC vs Kalshi",
                polymarket_condition_id="cond-poly",
                polymarket_token_id_yes="yes-token",
                polymarket_slug="btc",
                kalshi_ticker="KXBTC-YES",
                kalshi_event_ticker="KXBTC",
            )
            return [
                CrossPlatformOpportunity(
                    pair=pair,
                    direction="poly_yes_kalshi_no",
                    poly_cost=0.41,
                    kalshi_cost=0.46,
                    total_cost=0.87,
                    gross_edge=0.13,
                    net_edge=0.12,
                    edge_pct=13.79,
                    confidence=0.91,
                )
            ]

    signals = _collect_cross_platform_strategy_signals(
        config=make_test_config(default_order_size_usdc=9.0),
        scanner=_StubScanner(),
    )

    assert len(signals) == 1
    assert signals[0].tier == StrategyTier.CROSS_PLATFORM
    assert signals[0].signal_type == "cross_platform_poly_yes_kalshi_no"
    assert signals[0].market_id == "cond-poly"
    assert signals[0].recommended_size_usdc == 9.0


def test_serialize_strategy_signal_uses_overlay_adjusted_pending_signal():
    orchestrator = StrategyOrchestrator(total_bankroll=1000)
    market = MarketInfo(
        condition_id="cond-1234567890abcdef",
        question="Will BTC break 100k before June?",
        slug="btc-100k",
        tokens=[TokenInfo(token_id="yes", outcome="Yes"), TokenInfo(token_id="no", outcome="No")],
        event_id="event-1",
        raw={
            "research_signals": [{
                "topic_id": "event:event-1",
                "event_candidates": ["event-1"],
                "summary": "BTC momentum remains strong",
                "sources": ["google_news_rss"],
                "confidence": 0.72,
                "freshness_sec": 60.0,
                "stance": "bullish",
            }]
        },
    )
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="statistical_buy_yes",
        market_id="cond-12345678",
        description="signal",
        expected_edge=80.0,
        confidence=0.60,
        recommended_size_usdc=100.0,
    )

    submitted = orchestrator.submit_signal(signal, active_markets=[market], research_report=None, research_signals=[])
    pending = _find_pending_signal(orchestrator, signal)

    payload = _serialize_strategy_signal(pending, submitted=submitted, research_overlay=_find_pending_signal_overlay(orchestrator, signal))

    assert submitted is True
    assert pending is not None
    assert payload["confidence"] > 0.60
    assert payload["recommended_size_usdc"] > 100.0


def test_main_does_not_shadow_signal_module_name():
    """`main()` 调用 signal.signal(SIGINT/SIGTERM)，任何同名绑定都会把它变成
    局部变量，启动时直接 UnboundLocalError。

    注意 Python 3.12 的 PEP 709：list/dict/set 推导式被内联进外层函数，
    迭代变量因此出现在 `co_varnames` 里。按 PEP，推导式内的绑定是隔离的，
    外层引用仍编译成 LOAD_GLOBAL，所以那种情况不会真的崩；但它会让这里的
    检测手段失效。于是规则收严为"main 里任何绑定都不许叫 signal"，包括
    推导式 —— 在一个调用 signal.signal() 的函数里复用这个名字本来就该避免。
    """
    assert "signal" not in main.__code__.co_varnames
