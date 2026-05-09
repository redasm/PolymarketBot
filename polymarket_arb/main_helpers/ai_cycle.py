"""AI evaluation cycle extracted from `main_loop.py`.

`run_ai_cycle` runs one synchronous wrapper around the asynchronous
`AIAdvisor` interaction:

1. Build the market context.
2. Call `evaluate_markets` (with timeout) and submit any non-HOLD
   decisions through the orchestrator.
3. Mirror each decision onto the dashboard + event recorder so the
   UI / NDJSON stream show what AI proposed and whether the
   orchestrator accepted it.
4. Optionally call `adjust_risk_params` and apply the result to
   `RiskManager`.

The call graph and side-effect surface are identical to the inline
implementation; only the home moved.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from polymarket_arb.ai_advisor import AIAdvisor
from polymarket_arb.ai_context import MarketContextBuilder
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.config import ArbConfig
from polymarket_arb.dashboard_api import DashboardState
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.main_helpers.cli_setup import get_or_create_event_loop
from polymarket_arb.main_helpers.cycle_runners import find_pending_signal_overlay
from polymarket_arb.main_helpers.dashboard_serializers import (
    lookup_market_snapshot,
    serialize_recent_trade,
    summarize_market_catalog,
)
from polymarket_arb.models import MarketInfo, ResearchSignalReport
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)
from polymarket_arb.volatility_estimator import VolEstimator

LOG = logging.getLogger("main_loop")

# Default per-call timeout for AIAdvisor RPCs. The previous module-level
# constant in `main_loop.py` is preserved here under the same name so any
# downstream test that monkeypatches it can continue to do so via
# `polymarket_arb.main_helpers.ai_cycle.AI_EVAL_TIMEOUT_SEC`.
AI_EVAL_TIMEOUT_SEC = 20.0


def run_ai_cycle(
    *,
    ai_advisor: AIAdvisor,
    ctx_builder: MarketContextBuilder,
    active_markets: list[MarketInfo],
    recent_trades: list[Any],
    book_store: EnhancedBookStore,
    vol_estimator: VolEstimator,
    edge_decision: Any,
    risk_mgr: RiskManager,
    orchestrator: StrategyOrchestrator,
    dash_state: DashboardState,
    config: ArbConfig,
    research_report: ResearchSignalReport | dict | None = None,
    research_signals: list[Any] | None = None,
    event_recorder: EventRecorder | None = None,
) -> None:
    """Run one AI evaluation pass (synchronously wrapping async calls).

    Flow:

    1. Build a context payload covering active markets / books / vol /
       latest edge signal / recent trades / research overlay.
    2. `await ai_advisor.evaluate_markets(context)` under the per-call
       timeout. On timeout or any other exception, surface a dashboard
       error + event recorder entry and return — the orchestrator will
       continue to run on its own next cycle.
    3. For each non-HOLD decision, build a `StrategySignal`, submit it
       via the orchestrator, then mirror the decision (with research
       overlay attached) onto dashboard + event recorder.
    4. If `config.ai_override_risk` is set, also call
       `adjust_risk_params` and hand the result to `RiskManager`.
    5. Refresh the dashboard's AI / strategy status panels.
    """
    market_catalog = summarize_market_catalog(active_markets)
    context = ctx_builder.build(
        active_markets=active_markets,
        book_store=book_store,
        vol_estimator=vol_estimator,
        edge_signals=[edge_decision.to_dict()] if edge_decision else [],
        recent_trades=[serialize_recent_trade(t) for t in recent_trades],
        risk_state=risk_mgr.state,
        research_report=research_report,
        research_signals=research_signals,
    )

    loop = get_or_create_event_loop()
    try:
        decisions = loop.run_until_complete(
            asyncio.wait_for(ai_advisor.evaluate_markets(context), timeout=AI_EVAL_TIMEOUT_SEC)
        )
    except asyncio.TimeoutError:
        # `asyncio.wait_for` raises `asyncio.TimeoutError`, which is a
        # distinct type from the builtin `TimeoutError` on Python <3.11.
        # Catching the asyncio variant explicitly keeps the timeout
        # branch reachable on both 3.10 and 3.11+.
        LOG.error("AI 评估超时: %.1fs", AI_EVAL_TIMEOUT_SEC)
        dash_state.append_error({"message": "AI error: evaluation_timeout", "timestamp": time.time()})
        if event_recorder is not None:
            event_recorder.write_event("risk_events", {
                "event": "ai_evaluation_timeout",
                "timeout_sec": AI_EVAL_TIMEOUT_SEC,
            })
        return
    except Exception as e:
        LOG.error("AI 评估异常: %s", e, exc_info=True)
        dash_state.append_error({"message": f"AI error: {e}", "timestamp": time.time()})
        if event_recorder is not None:
            event_recorder.write_event("risk_events", {
                "event": "ai_evaluation_error",
                "error": str(e),
            })
        return

    for dec in decisions:
        if dec.action == "HOLD":
            continue
        signal = StrategySignal(
            tier=StrategyTier.STATISTICAL_ARB,
            signal_type=f"ai_{dec.action.lower()}",
            market_id=dec.market_id,
            description=dec.reasoning[:120],
            expected_edge=dec.confidence * 100,
            confidence=dec.confidence,
            recommended_size_usdc=dec.recommended_size_pct * config.max_total_exposure,
            urgency=dec.urgency,
            payload={"action": dec.action},
        )
        submitted = orchestrator.submit_signal(
            signal,
            active_markets=active_markets,
            research_report=research_report,
            research_signals=research_signals,
        )
        overlay_payload = find_pending_signal_overlay(orchestrator, signal)
        market_info = lookup_market_snapshot(dec.market_id, market_catalog)
        dash_state.append_ai_decision({
            **dec.to_dict(),
            "market_question": market_info.get("question", ""),
            "decision_price": market_info.get("yes_price"),
            "submitted": submitted,
            "research_overlay": overlay_payload,
        })
        if event_recorder is not None:
            event_recorder.write_event("ai_decisions", {
                **dec.to_dict(),
                "market_question": market_info.get("question", ""),
                "decision_price": market_info.get("yes_price"),
                "submitted": submitted,
                "research_overlay": overlay_payload,
            })

    if config.ai_override_risk:
        try:
            adjustments = loop.run_until_complete(
                asyncio.wait_for(ai_advisor.adjust_risk_params(context), timeout=AI_EVAL_TIMEOUT_SEC)
            )
            if adjustments:
                risk_mgr.apply_ai_adjustment(adjustments)
        except asyncio.TimeoutError:
            LOG.error("AI 风控调整超时: %.1fs", AI_EVAL_TIMEOUT_SEC)
            if event_recorder is not None:
                event_recorder.write_event("risk_events", {
                    "event": "ai_risk_timeout",
                    "timeout_sec": AI_EVAL_TIMEOUT_SEC,
                })
        except Exception as e:
            LOG.error("AI 风控调整异常: %s", e, exc_info=True)
            if event_recorder is not None:
                event_recorder.write_event("risk_events", {
                    "event": "ai_risk_error",
                    "error": str(e),
                })

    dash_state.update(
        ai_status=ai_advisor.get_status(),
        strategy_status=orchestrator.get_status(),
    )
