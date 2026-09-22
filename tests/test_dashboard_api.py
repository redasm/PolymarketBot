"""Dashboard API state tests for research/backtest endpoints."""

from polymarket_arb.dashboard_api import DashboardState


def test_dashboard_state_snapshot_includes_research_and_backtest_fields():
    state = DashboardState()
    state.update(
        universe_status={"universe_market_count": 42, "focus_keywords": ["btc"]},
        research_signal_status={"enabled": True, "count": 1},
        execution_summary={
            "theoretical_opportunities": 3,
            "simulated_successes": 2,
            "live_successes": 1,
            "simulated_profit_total": 0.42,
        },
        backtest_last_report={
            "enabled": True,
            "strategy_name": "t0_structural_arbitrage",
            "recent_trade_rows": [{"ts_ms": 1000, "realized_pnl": 0.01}],
        },
    )

    snap = state.snapshot()

    assert snap["universe_status"]["universe_market_count"] == 42
    assert snap["research_signal_status"]["enabled"] is True
    assert snap["execution_summary"]["simulated_successes"] == 2
    assert snap["backtest_last_report"]["strategy_name"] == "t0_structural_arbitrage"
    assert snap["backtest_last_report"]["recent_trade_rows"][0]["ts_ms"] == 1000


def test_dashboard_snapshot_preserves_trade_and_opportunity_modes():
    state = DashboardState()
    state.append_opportunity({
        "event_title": "Vermont Governor Election Winner",
        "arb_type": "multi_outcome",
        "stage": "verified",
        "mode": "theoretical",
        "net_edge": 0.031,
    })
    state.append_trade({
        "event_title": "Vermont Governor Election Winner",
        "arb_type": "multi_outcome",
        "status": "filled",
        "mode": "simulated",
        "expected_profit": 0.1632,
    })

    snap = state.snapshot()

    assert snap["recent_opportunities"][0]["mode"] == "theoretical"
    assert snap["recent_trades"][0]["mode"] == "simulated"


def test_dashboard_snapshot_preserves_research_signal_freshness():
    state = DashboardState()
    state.update(
        research_signal_status={
            "enabled": True,
            "count": 1,
            "items": [{"topic_id": "btc", "freshness_sec": 42.0, "summary": "sample"}],
        }
    )

    snap = state.snapshot()

    assert snap["research_signal_status"]["items"][0]["freshness_sec"] == 42.0


def test_dashboard_state_disabled_becomes_noop():
    state = DashboardState(enabled=False)
    state.update(cycle_count=99, arbs_found=12)
    state.append_opportunity({"event_title": "noop"})
    state.append_trade({"trade_id": "noop"})
    state.append_error({"message": "noop"})
    state.append_pnl_point({"timestamp": 1.0})

    snap = state.snapshot()
    assert snap["enabled"] is False
    assert snap["cycle_count"] == 0
    assert snap["arbs_found"] == 0
    assert snap["recent_opportunities"] == []
    assert snap["recent_trades"] == []
    assert snap["recent_errors"] == []
    assert snap["pnl_history"] == []
