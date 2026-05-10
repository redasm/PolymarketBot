"""Tests for `polymarket_arb.main_helpers.dashboard_cycle_payload`.

Pins the per-cycle dashboard schema. Adding/removing top-level keys
or changing any field name in `risk_state` / `execution_summary` /
`research_signal_status` etc. is a breaking change for the dashboard
panels and the NDJSON consumers; this test file makes such drift
visible immediately.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymarket_arb.main_helpers.dashboard_cycle_payload import (
    build_dashboard_cycle_payload,
)
from polymarket_arb.models import MarketInfo, TokenInfo


# ---------- shared fixtures ---------------------------------------------------


def _market(condition_id: str = "c1", question: str = "Q?") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question=question,
        slug="s",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
    )


def _config(**overrides):
    base = dict(
        hot_market_pool_size=50,
        hot_event_pool_size=20,
        max_open_positions=10,
        portfolio_sync_enabled=True,
        portfolio_sync_max_consecutive_failures=3,
        research_signal_enabled=True,
        research_signal_max_items=5,
        backtest_reports_dir="/nonexistent/backtest",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _risk_state(**overrides):
    base = dict(
        is_halted=False,
        halt_reason="",
        open_positions=2,
        total_exposure=123.45,
        daily_pnl=4.56,
        unrealized_pnl=-1.25,
        total_pnl=3.31,
        current_position_value=77.0,
        consecutive_failures=0,
        last_portfolio_sync_ts=1000.0,
        portfolio_sync_ok=True,
        portfolio_sync_error="",
        portfolio_sync_consecutive_failures=0,
        positions=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _orchestrator():
    return SimpleNamespace(get_status=lambda: {"T0": {"current_exposure": 0.0}})


def _enhanced_store():
    return SimpleNamespace(get_summary=lambda: {"yes_mid": 0.5, "spread": 0.01})


def _counters(**overrides):
    base = dict(
        total_theoretical_opportunities=12,
        total_live_successes=3,
        total_simulated_successes=5,
        total_live_submissions=7,
        total_simulated_submissions=9,
        total_live_expected_profit=1.234567890,
        total_simulated_expected_profit=2.345678901,
    )
    base.update(overrides)
    return base


def _build(**overrides):
    """Construct a payload with sensible defaults; overrides applied on top."""
    base = dict(
        config=_config(),
        cycle=42,
        scanned_markets=[_market("a"), _market("b")],
        universe_markets=[_market("a"), _market("b"), _market("c")],
        cached_universe_markets=[_market("a"), _market("b"), _market("c"), _market("d")],
        event_candidates=[SimpleNamespace(), SimpleNamespace()],
        focus_keywords=["btc", "eth"],
        last_universe_refresh_ts=1234.5,
        universe_refreshed=True,
        risk_state=_risk_state(),
        vol_snapshot={"fast": 0.05, "slow": 0.03},
        edge_decision=SimpleNamespace(to_dict=lambda: {"direction": "BUY_YES", "edge_bps": 15}),
        enhanced_store=_enhanced_store(),
        ws_status={"connected": True, "subscribed_tokens": 4},
        orchestrator=_orchestrator(),
        research_report=None,
        research_signals=[],
        research_signal_enabled=False,
        counters=_counters(),
    )
    base.update(overrides)
    return build_dashboard_cycle_payload(**base)


# ---------- top-level keys ---------------------------------------------------


def test_top_level_keys_pinned() -> None:
    payload = _build()
    expected = {
        "cycle_count",
        "arbs_found",
        "arbs_executed",
        "markets_scanned",
        "universe_status",
        "risk_state",
        "volatility",
        "edge_decision",
        "book_summary",
        "ws_status",
        "market_catalog",
        "strategy_status",
        "execution_summary",
        "research_signal_status",
        "backtest_last_report",
        "current_positions",
    }
    assert set(payload.keys()) == expected


# ---------- counters & basic fields ------------------------------------------


def test_counters_drive_arbs_and_execution_summary() -> None:
    payload = _build()
    assert payload["cycle_count"] == 42
    assert payload["arbs_found"] == 12  # total_theoretical_opportunities
    assert payload["arbs_executed"] == 3  # total_live_successes
    assert payload["markets_scanned"] == 2  # len(scanned_markets)

    summary = payload["execution_summary"]
    assert summary["theoretical_opportunities"] == 12
    assert summary["live_successes"] == 3
    assert summary["simulated_successes"] == 5
    assert summary["live_submissions"] == 7
    assert summary["simulated_submissions"] == 9
    # Profit fields rounded to 6 decimal places.
    assert summary["live_profit_total"] == pytest.approx(1.234568)
    assert summary["simulated_profit_total"] == pytest.approx(2.345679)


# ---------- universe_status --------------------------------------------------


def test_universe_status_uses_cached_count_for_total() -> None:
    payload = _build()
    us = payload["universe_status"]
    assert us["universe_market_count"] == 4  # cached_universe_markets
    assert us["selected_market_count"] == 2  # scanned_markets
    assert us["selected_event_count"] == 2
    assert us["focus_keywords"] == ["btc", "eth"]
    assert us["last_universe_refresh_ts"] == 1234.5
    assert us["universe_refreshed"] is True


def test_universe_status_zero_refresh_ts_normalises_to_none() -> None:
    payload = _build(last_universe_refresh_ts=0.0)
    assert payload["universe_status"]["last_universe_refresh_ts"] is None


# ---------- risk_state -------------------------------------------------------


def test_risk_state_pulls_from_risk_state_dataclass_and_config() -> None:
    payload = _build()
    rs = payload["risk_state"]
    assert rs["is_halted"] is False
    assert rs["open_positions"] == 2
    assert rs["max_positions"] == 10  # from config
    assert rs["total_exposure"] == 123.45
    assert rs["daily_pnl"] == 4.56
    assert rs["realized_daily_pnl"] == 4.56
    assert rs["unrealized_pnl"] == -1.25
    assert rs["total_pnl"] == pytest.approx(3.31)
    assert rs["current_position_value"] == 77.0
    assert rs["portfolio_pnl_stale"] is False
    assert rs["portfolio_sync_enabled"] is True
    assert rs["portfolio_sync_max_consecutive_failures"] == 3


def test_risk_state_normalises_zero_sync_ts_to_none() -> None:
    payload = _build(risk_state=_risk_state(last_portfolio_sync_ts=0.0))
    assert payload["risk_state"]["last_portfolio_sync_ts"] is None


# ---------- edge_decision ----------------------------------------------------


def test_edge_decision_serialised_when_present() -> None:
    payload = _build()
    assert payload["edge_decision"] == {"direction": "BUY_YES", "edge_bps": 15}


def test_edge_decision_none_when_falsy() -> None:
    payload = _build(edge_decision=None)
    assert payload["edge_decision"] is None


# ---------- market catalog source --------------------------------------------


def test_market_catalog_prefers_universe_over_scanned() -> None:
    """When universe_markets is non-empty, it's used (broader coverage)."""
    payload = _build()
    catalog = payload["market_catalog"]
    # universe_markets has 3 entries; scanned only has 2.
    assert len(catalog) == 3


def test_market_catalog_falls_back_to_scanned_when_universe_empty() -> None:
    payload = _build(universe_markets=[])
    catalog = payload["market_catalog"]
    assert len(catalog) == 2


# ---------- research_signal_status -------------------------------------------


def test_research_signal_status_empty_when_disabled() -> None:
    payload = _build(config=_config(research_signal_enabled=False))
    assert payload["research_signal_status"] == {}


def test_research_signal_status_uses_report_metadata() -> None:
    report = SimpleNamespace(
        topic_count=4, row_count=20, cache_hit=True, source_counts={"rss": 12}
    )
    sig = SimpleNamespace(to_dict=lambda: {"id": "s1", "topic": "btc"})
    payload = _build(
        config=_config(research_signal_enabled=True, research_signal_max_items=3),
        research_report=report,
        research_signals=[sig, sig, sig, sig],
        research_signal_enabled=True,
    )
    rs = payload["research_signal_status"]
    assert rs["enabled"] is True
    assert rs["count"] == 4  # all signals counted
    assert rs["topic_count"] == 4
    assert rs["row_count"] == 20
    assert rs["cache_hit"] is True
    assert rs["source_counts"] == {"rss": 12}
    # items truncated to research_signal_max_items
    assert len(rs["items"]) == 3


def test_research_signal_status_handles_missing_report_gracefully() -> None:
    payload = _build(
        config=_config(research_signal_enabled=True),
        research_report=None,
        research_signals=[],
        research_signal_enabled=True,
    )
    rs = payload["research_signal_status"]
    assert rs["enabled"] is True
    assert rs["count"] == 0
    assert rs["topic_count"] == 0
    assert rs["row_count"] == 0
    assert rs["cache_hit"] is False
    assert rs["source_counts"] == {}
    assert rs["items"] == []


# ---------- current_positions ------------------------------------------------


def test_current_positions_serialises_each_position() -> None:
    pos = SimpleNamespace(
        token_id="tok-yes",
        condition_id="cond-1",
        outcome="Yes",
        size=10.0,
        avg_price=0.45,
        current_value=4.5,
        unrealized_pnl=0.1,
    )
    payload = _build(risk_state=_risk_state(positions=[pos, pos]))
    positions = payload["current_positions"]
    assert len(positions) == 2
    assert positions[0]["token_id"] == "tok-yes"
    assert positions[0]["avg_price"] == 0.45


def test_current_positions_empty_when_no_positions() -> None:
    payload = _build()
    assert payload["current_positions"] == []
