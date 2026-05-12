"""Build the per-cycle `DashboardState.update(**payload)` kwargs.

This is a pure dict assembly extracted from `main_loop.main()`.
The function takes the cycle's running counters, the latest snapshots
of risk / vol / book / orchestrator state, plus any research / backtest
context, and returns the exact kwargs the dashboard panels expect.

Keeping it in one place makes it easy to keep the dashboard schema
stable across refactors and easy to unit-test the field shape.
"""

from __future__ import annotations

from typing import Any

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.config import ArbConfig
from polymarket_arb.main_helpers.cli_setup import load_last_backtest_report
from polymarket_arb.main_helpers.dashboard_serializers import summarize_market_catalog
from polymarket_arb.models import MarketInfo, ResearchSignalReport
from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator


def build_dashboard_cycle_payload(
    *,
    config: ArbConfig,
    cycle: int,
    scanned_markets: list[MarketInfo],
    universe_markets: list[MarketInfo],
    cached_universe_markets: list[MarketInfo],
    event_candidates: list[Any],
    focus_keywords: list[str],
    last_universe_refresh_ts: float,
    universe_refreshed: bool,
    risk_state: Any,
    vol_snapshot: Any,
    edge_decision: Any,
    enhanced_store: EnhancedBookStore,
    ws_status: dict,
    orchestrator: StrategyOrchestrator,
    research_report: ResearchSignalReport | None,
    research_signals: list[Any],
    research_signal_enabled: bool,
    counters: dict,
) -> dict[str, Any]:
    """Build the `dash_state.update(**payload)` kwargs for one cycle.

    `counters` carries the per-cycle running totals:
    `total_theoretical_opportunities` (mixed T0+directional, legacy),
    `total_t0_opportunities`, `total_directional_signals`,
    `total_live_successes`, `total_simulated_successes`,
    `total_live_submissions`, `total_simulated_submissions`,
    `total_live_expected_profit`, `total_simulated_expected_profit`.

    The dashboard's headline `arbs_found` field reflects T0 structural
    arbs only (consistent with `progress_cb` and the operator-facing
    state-line log). The mixed value is still preserved under
    `execution_summary.theoretical_opportunities` for backward
    compatibility, alongside the new explicit splits.

    `risk_state` is `risk_mgr.state` (a `RiskState` dataclass) — passed
    in by the caller after `risk_mgr.state` is read once so the
    snapshot is consistent across the panel.

    Returns a dict — the caller does `dash_state.update(**payload)`.
    """
    # Pick the broader market list for catalog/overlay so the dashboard
    # shows everything the orchestrator can act on, not just the hot pool.
    catalog_markets = universe_markets if universe_markets else scanned_markets
    # T0-only headline. Fall back to the legacy mixed counter if the
    # caller hasn't supplied the explicit split yet — keeps older test
    # fixtures + any out-of-tree caller working without a hard crash.
    t0_total = counters.get(
        "total_t0_opportunities", counters["total_theoretical_opportunities"]
    )
    directional_total = counters.get("total_directional_signals", 0)
    return {
        "cycle_count": cycle,
        "arbs_found": t0_total,
        "arbs_executed": counters["total_live_successes"],
        "markets_scanned": len(scanned_markets),
        "universe_status": {
            "universe_market_count": len(cached_universe_markets),
            "hot_market_pool_size": config.hot_market_pool_size,
            "hot_event_pool_size": config.hot_event_pool_size,
            "selected_market_count": len(scanned_markets),
            "selected_event_count": len(event_candidates),
            "focus_keywords": focus_keywords,
            "last_universe_refresh_ts": last_universe_refresh_ts or None,
            "universe_refreshed": universe_refreshed,
        },
        "risk_state": {
            "is_halted": risk_state.is_halted,
            "halt_reason": risk_state.halt_reason,
            "open_positions": risk_state.open_positions,
            "max_positions": config.max_open_positions,
            "total_exposure": risk_state.total_exposure,
            "daily_pnl": risk_state.daily_pnl,
            "realized_daily_pnl": risk_state.daily_pnl,
            "unrealized_pnl": getattr(risk_state, "unrealized_pnl", 0.0),
            "total_pnl": getattr(risk_state, "total_pnl", risk_state.daily_pnl),
            "current_position_value": getattr(risk_state, "current_position_value", 0.0),
            "portfolio_pnl_stale": getattr(risk_state, "portfolio_pnl_stale", False),
            "consecutive_failures": risk_state.consecutive_failures,
            "portfolio_sync_enabled": config.portfolio_sync_enabled,
            "last_portfolio_sync_ts": risk_state.last_portfolio_sync_ts or None,
            "portfolio_sync_ok": risk_state.portfolio_sync_ok,
            "portfolio_sync_error": risk_state.portfolio_sync_error,
            "portfolio_sync_consecutive_failures": risk_state.portfolio_sync_consecutive_failures,
            "portfolio_sync_max_consecutive_failures": config.portfolio_sync_max_consecutive_failures,
        },
        "volatility": vol_snapshot,
        "edge_decision": edge_decision.to_dict() if edge_decision else None,
        "book_summary": enhanced_store.get_summary(),
        "ws_status": ws_status,
        "market_catalog": summarize_market_catalog(catalog_markets),
        "strategy_status": orchestrator.get_status(),
        "execution_summary": {
            "theoretical_opportunities": counters["total_theoretical_opportunities"],
            "t0_opportunities": t0_total,
            "directional_signals": directional_total,
            "live_successes": counters["total_live_successes"],
            "simulated_successes": counters["total_simulated_successes"],
            "live_submissions": counters["total_live_submissions"],
            "simulated_submissions": counters["total_simulated_submissions"],
            "live_profit_total": round(counters["total_live_expected_profit"], 6),
            "simulated_profit_total": round(counters["total_simulated_expected_profit"], 6),
        },
        "research_signal_status": (
            {
                "enabled": research_signal_enabled,
                "count": len(research_signals),
                "topic_count": research_report.topic_count if research_report else 0,
                "row_count": research_report.row_count if research_report else 0,
                "cache_hit": research_report.cache_hit if research_report else False,
                "source_counts": dict(research_report.source_counts) if research_report else {},
                "items": [signal.to_dict() for signal in research_signals[: config.research_signal_max_items]],
            }
            if config.research_signal_enabled
            else {}
        ),
        "backtest_last_report": load_last_backtest_report(config.backtest_reports_dir),
        "current_positions": [
            {
                "token_id": position.token_id,
                "condition_id": position.condition_id,
                "outcome": position.outcome,
                "size": position.size,
                "avg_price": position.avg_price,
                "current_value": position.current_value,
                "unrealized_pnl": position.unrealized_pnl,
            }
            for position in risk_state.positions
        ],
    }
