"""Per-opportunity T0 (structural-arb) execution chain.

`execute_t0_opportunity` is the side-effect-heavy block that turns a
single detected `ArbOpportunity` into trades. The chain runs in this
fixed order so failures bail early and the dashboard / event log get
the right reject reason:

1. Notify (chat) + log the opportunity text.
2. Compute `target_size` from default order size + venue depth ceiling.
3. `detector.verify_opportunity_with_depth` — re-check the book depth
   under the requested size; if the venue can't actually fill it any
   more, bail with `depth_verification_failed`.
4. `risk_mgr.pre_trade_check` — exposure / open-positions / daily-loss
   guardrails. Bails with `pre_trade_reject`.
5. `executor.ensure_sufficient_collateral` — wallet balance check.
   Bails with `balance_reject`.
6. `executor.execute_arbitrage` — fire all legs (atomic with rollback
   on partial failure inside `ExecutionEngine`).
7. Reconcile risk-manager state (live trades only — simulated never
   touch the venue), serialise the trade payload, mirror to dashboard.
8. Notify success / failure + feed AI advisor (live only).

Returns an `ExecutionDelta` with the four counters populated:
`live_successes`, `simulated_successes`, `live_profit_total`,
`simulated_profit_total`. The caller adds those to its running totals.
"""

from __future__ import annotations

import logging

from polymarket_arb.ai_advisor import AIAdvisor
from polymarket_arb.arbitrage_detector import ArbitrageDetector
from polymarket_arb.config import ArbConfig
from polymarket_arb.dashboard_api import DashboardState
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.main_helpers.dashboard_serializers import (
    build_dashboard_trade_rows,
    estimate_ai_trade_outcome,
    has_simulated_trades,
    is_live_execution_success,
    serialize_opportunity_event,
    serialize_trade_execution,
)
from polymarket_arb.main_helpers.strategy_execution import ExecutionDelta
from polymarket_arb.models import ArbOpportunity
from polymarket_arb.arbitrage_detector import format_arb_opportunity_zh
from polymarket_arb.notifier import NotificationManager
from polymarket_arb.risk_manager import RiskManager
import time

LOG = logging.getLogger("main_loop")


def execute_t0_opportunity(
    *,
    opp: ArbOpportunity,
    config: ArbConfig,
    detector: ArbitrageDetector,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    notifier: NotificationManager,
    ai_advisor: AIAdvisor | None,
    dash_state: DashboardState,
    event_recorder: EventRecorder,
) -> ExecutionDelta:
    """Run one opportunity through the verify → risk → fund → fire chain.

    Returns an `ExecutionDelta` where exactly one of (live_successes,
    simulated_successes) is set on success — the caller adds these to
    its running totals. On any rejection (depth / risk / collateral),
    returns an empty delta and writes a stable-identifier reject event
    to `event_recorder`.

    Side effects per call:

    - Always: one chat notification (`notify_arb_found`).
    - On reject: one `risk_events` entry naming the reject reason.
    - On accept: one `opportunities` entry (verified stage), one
      `trades` entry, one dashboard opportunity card, one trade row
      per leg, optional AI outcome update (live only), optional
      success/failure notification.
    """
    delta = ExecutionDelta()

    arb_text = format_arb_opportunity_zh(opp)
    LOG.info("\n%s", arb_text)
    notifier.notify_arb_found(arb_text)

    target_size = min(
        config.default_order_size_usdc / opp.total_cost if opp.total_cost > 0 else 0,
        opp.max_executable_size,
    )

    verified = detector.verify_opportunity_with_depth(opp, target_size)
    if verified is None:
        LOG.info("深度验证失败，跳过")
        event_recorder.write_event("risk_events", {
            "event": "depth_verification_failed",
            "event_id": opp.event_id,
            "arb_type": opp.arb_type.value,
            "target_size": target_size,
        })
        return delta

    event_recorder.write_event("opportunities", serialize_opportunity_event(verified, stage="verified"))
    dash_state.append_opportunity({
        "arb_type": verified.arb_type.value,
        "mode": "theoretical",
        "stage": "verified",
        "event_title": verified.event_title,
        "total_cost": verified.total_cost,
        "net_edge": verified.net_edge,
        "edge_pct": verified.edge_pct,
        "confidence": verified.confidence,
        "max_size": verified.max_executable_size,
        "legs": len(verified.legs),
        "timestamp": time.time(),
    })

    can_trade, reason, adj_size = risk_mgr.pre_trade_check(verified, target_size)
    if not can_trade:
        LOG.info("风控拒绝: %s", reason)
        event_recorder.write_event("risk_events", {
            "event": "pre_trade_reject",
            "event_id": verified.event_id,
            "arb_type": verified.arb_type.value,
            "reason": reason,
            "target_size": target_size,
            "adjusted_size": adj_size,
        })
        return delta

    balance_ok, balance_reason, _ = executor.ensure_sufficient_collateral(verified.total_cost * adj_size)
    if not balance_ok:
        LOG.info("余额校验拒绝: %s", balance_reason)
        event_recorder.write_event("risk_events", {
            "event": "balance_reject",
            "event_id": verified.event_id,
            "arb_type": verified.arb_type.value,
            "reason": balance_reason,
            "adjusted_size": adj_size,
        })
        return delta

    trades = executor.execute_arbitrage(verified, adj_size)
    # Only reconcile risk for live trades — simulated never reach the
    # venue, so reserving exposure for them would skew the books.
    if not has_simulated_trades(trades):
        risk_mgr.record_execution(verified, trades)
    live_execution_success = is_live_execution_success(config, executor, verified, trades)
    trade_payload = serialize_trade_execution(verified, trades, live_execution_success, adj_size)
    event_recorder.write_event("trades", trade_payload)
    dashboard_execution_success = bool(trade_payload.get("arb_success", False))
    if live_execution_success:
        delta.live_successes = 1
        delta.live_profit_total = float(trade_payload.get("trade_outcome_estimate") or 0.0)
    elif bool(trade_payload.get("simulated", False)) and dashboard_execution_success:
        delta.simulated_successes = 1
        delta.simulated_profit_total = float(trade_payload.get("trade_outcome_estimate") or 0.0)

    for trade_row in build_dashboard_trade_rows(
        opp=verified,
        trades=trades,
        live_execution_success=live_execution_success,
        dashboard_execution_success=dashboard_execution_success,
    ):
        dash_state.append_trade(trade_row)

    filled = [t for t in trades if t.status.value == "filled"]
    if ai_advisor is not None and not config.dry_run:
        ai_advisor.record_trade_outcome(
            estimate_ai_trade_outcome(verified, trades, live_execution_success, adj_size)
        )
    if live_execution_success and filled:
        notifier.notify_trade_success(
            event_title=opp.event_title,
            arb_type=opp.arb_type.value,
            filled_legs=len(filled),
            total_legs=len(opp.legs),
            expected_profit=opp.net_edge * adj_size,
            simulated=bool(trade_payload.get("simulated", False)),
        )
    elif trades and not config.dry_run:
        failure_reasons = sorted({
            str(getattr(trade, "error", "")).strip()
            for trade in trades
            if str(getattr(trade, "error", "")).strip()
        })
        notifier.notify_trade_failure(
            event_title=opp.event_title,
            filled_legs=len(filled),
            total_legs=len(opp.legs),
            simulated=bool(trade_payload.get("simulated", False)),
            details="; ".join(failure_reasons[:2]),
        )

    return delta
