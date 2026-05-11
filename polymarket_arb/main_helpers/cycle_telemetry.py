"""Cycle telemetry helpers: build + emit per-cycle summary payloads.

Each scan cycle finishes by writing a `cycle_metrics` event to the
event recorder so the dashboard's "cycle history" panel and downstream
log-grep tooling can plot throughput / latency / WS health over time.

`build_cycle_summary_payload` is pure (data transform → dict);
`emit_cycle_metrics` adds the side effect of stamping `total_cycle_sec`
from a `time.perf_counter()` baseline and writing to the event
recorder. They were extracted from `main_loop.py` so the payload schema
can be unit-tested without booting the loop.
"""

from __future__ import annotations

import time
from typing import Any

from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.main_helpers.cli_setup import round_timing
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer


def build_cycle_summary_payload(
    *,
    run_id: str,
    cycle: int,
    markets_scanned: int,
    universe_market_count: int,
    selected_event_count: int,
    theoretical_opportunities_total: int,
    live_successes_total: int,
    simulated_successes_total: int,
    live_submissions_total: int,
    simulated_submissions_total: int,
    ws_status: dict[str, Any],
    research_count: int,
    daily_pnl: float,
    open_positions: int,
    focus_keywords: list[str],
    book_stats: dict[str, int],
    timing_stats: dict[str, float],
    unrealized_pnl: float = 0.0,
    total_pnl: float | None = None,
    current_position_value: float = 0.0,
    cycle_status: str = "ok",
    theoretical_opportunities_today: int = 0,
    live_successes_today: int = 0,
) -> dict[str, Any]:
    """Build the per-cycle telemetry payload.

    The shape here is the wire contract for downstream consumers
    (dashboard, NDJSON event log, optional Feishu summaries). Two pairs
    of fields are intentionally duplicated for backward compatibility:

    - `arbs_found_total` / `theoretical_opportunities_total`
    - `arbs_executed_total` / `live_successes_total`

    Older dashboards key off the short names; newer event-log consumers
    use the explicit ones. Keep both until the dashboard drops the
    legacy keys.
    """
    return {
        "event": "cycle_summary",
        "cycle_status": cycle_status,
        "run_id": run_id,
        "cycle": cycle,
        "markets_scanned": markets_scanned,
        "universe_market_count": universe_market_count,
        "selected_event_count": selected_event_count,
        "arbs_found_total": theoretical_opportunities_total,
        "arbs_executed_total": live_successes_total,
        "arbs_found_today": theoretical_opportunities_today,
        "arbs_executed_today": live_successes_today,
        "theoretical_opportunities_total": theoretical_opportunities_total,
        "live_successes_total": live_successes_total,
        "simulated_successes_total": simulated_successes_total,
        "live_submissions_total": live_submissions_total,
        "simulated_submissions_total": simulated_submissions_total,
        "ws_connected": ws_status.get("connected", False),
        "ws_tokens": ws_status.get("subscribed_tokens", 0),
        "research_count": research_count,
        "daily_pnl": daily_pnl,
        "realized_daily_pnl": daily_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": daily_pnl + unrealized_pnl if total_pnl is None else total_pnl,
        "current_position_value": current_position_value,
        "open_positions": open_positions,
        "focus_keywords": focus_keywords,
        "book_stats": {
            key: int(book_stats.get(key, 0))
            for key in (
                "requests",
                "ws_hit",
                "cache_hit",
                "rest_fallback",
                "rest_success",
                "rest_error",
                "missing_orderbook",
                "cooldown_skip",
            )
        },
        "timing": {
            key: round_timing(value)
            for key, value in timing_stats.items()
        },
    }


def emit_cycle_metrics(
    *,
    event_recorder: EventRecorder,
    ob_analyzer: OrderBookAnalyzer,
    cycle_perf_start: float,
    cycle_timing: dict[str, float],
    run_id: str,
    cycle: int,
    markets_scanned: int,
    universe_market_count: int,
    selected_event_count: int,
    theoretical_opportunities_total: int,
    live_successes_total: int,
    simulated_successes_total: int,
    live_submissions_total: int,
    simulated_submissions_total: int,
    ws_status: dict[str, Any],
    research_count: int,
    daily_pnl: float,
    open_positions: int,
    focus_keywords: list[str],
    unrealized_pnl: float = 0.0,
    total_pnl: float | None = None,
    current_position_value: float = 0.0,
    cycle_status: str = "ok",
    theoretical_opportunities_today: int = 0,
    live_successes_today: int = 0,
) -> dict[str, Any]:
    """Snapshot orderbook stats, build the payload, and write to the event log.

    `ob_analyzer.snapshot_stats(reset=True)` is intentionally called
    *here* (not in `build_cycle_summary_payload`) so the pure-payload
    builder remains testable without any side effects. Calling it with
    `reset=True` re-zeros the per-cycle counters so each NDJSON row
    represents one cycle's traffic, not cumulative since boot.
    """
    cycle_book_stats = ob_analyzer.snapshot_stats(reset=True)
    timing_stats = dict(cycle_timing)
    timing_stats["total_cycle_sec"] = time.perf_counter() - cycle_perf_start
    payload = build_cycle_summary_payload(
        run_id=run_id,
        cycle=cycle,
        markets_scanned=markets_scanned,
        universe_market_count=universe_market_count,
        selected_event_count=selected_event_count,
        theoretical_opportunities_total=theoretical_opportunities_total,
        live_successes_total=live_successes_total,
        simulated_successes_total=simulated_successes_total,
        live_submissions_total=live_submissions_total,
        simulated_submissions_total=simulated_submissions_total,
        ws_status=ws_status,
        research_count=research_count,
        daily_pnl=daily_pnl,
        open_positions=open_positions,
        unrealized_pnl=unrealized_pnl,
        total_pnl=total_pnl,
        current_position_value=current_position_value,
        focus_keywords=focus_keywords,
        book_stats=cycle_book_stats,
        timing_stats=timing_stats,
        cycle_status=cycle_status,
        theoretical_opportunities_today=theoretical_opportunities_today,
        live_successes_today=live_successes_today,
    )
    if event_recorder.is_enabled:
        event_recorder.write_event("cycle_metrics", payload)
    return payload
