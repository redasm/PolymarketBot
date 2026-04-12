"""Dashboard API state tests for research/backtest endpoints."""

import time

from fastapi.testclient import TestClient

from polymarket_arb.dashboard_api import DashboardState, app, set_state


def test_dashboard_state_snapshot_includes_research_and_backtest_fields():
    state = DashboardState()
    state.update(
        universe_status={"universe_market_count": 42, "focus_keywords": ["btc"]},
        research_signal_status={"enabled": True, "count": 1},
        backtest_last_report={
            "enabled": True,
            "strategy_name": "t0_structural_arbitrage",
            "recent_trade_rows": [{"ts_ms": 1000, "realized_pnl": 0.01}],
        },
    )

    snap = state.snapshot()

    assert snap["universe_status"]["universe_market_count"] == 42
    assert snap["research_signal_status"]["enabled"] is True
    assert snap["backtest_last_report"]["strategy_name"] == "t0_structural_arbitrage"
    assert snap["backtest_last_report"]["recent_trade_rows"][0]["ts_ms"] == 1000


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


def test_api_ai_enriches_decisions_with_market_validation():
    state = DashboardState()
    state.update(
        ai_status={"enabled": True, "provider": "openai", "model": "gpt-test", "call_count": 3, "daily_cost_usd": 0.0123},
        market_catalog={
            "c1": {
                "question": "Will BTC go up this week?",
                "yes_price": 0.62,
                "volume_24h": 1000,
                "liquidity": 500,
            }
        },
    )
    state.append_ai_decision({
        "action": "BUY_YES",
        "market_id": "c1",
        "confidence": 0.78,
        "recommended_size_pct": 0.05,
        "reasoning": "Momentum and research are aligned.",
        "decision_price": 0.55,
        "timestamp": time.time() - 600,
        "submitted": True,
    })
    set_state(state)

    client = TestClient(app)
    response = client.get("/api/ai")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["enabled"] is True
    assert payload["decisions"][0]["market_question"] == "Will BTC go up this week?"
    assert payload["decisions"][0]["current_price"] == 0.62
    assert payload["decisions"][0]["validation"]["status"] == "correct"
