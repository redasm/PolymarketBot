"""Strategy-tier execution dispatch extracted from `main_loop.py`.

`execute_strategy_signal` is the side of the strategy lifecycle that
*acts* on a `StrategySignal` (the orchestrator owns selection and
priority ordering). It dispatches by `signal.tier`:

- `CROSS_PLATFORM` (T1) — dry-run-only simulator that records two
  paired `TradeRecord`s (Polymarket + Kalshi). Live execution requires
  an external Kalshi executor we don't have, so live mode just refuses
  the trade.
- `STATISTICAL_ARB` (T2) — directional buy via `ExecutionEngine`,
  guarded by `RiskManager.pre_trade_check` and the executor's collateral
  check. Successful fills are forwarded to `T2ExitManager` so the exit
  policy (stop-loss / take-profit / time-stop / optimal-stopping) starts
  tracking the position.
- `MARKET_MAKING` (T3) — quote selection (prefers exit fills over new
  inventory, then highest edge) followed by a post-only limit order via
  `ExecutionEngine.submit_limit_order`.

All branches return `(submission_success, reason, ExecutionDelta)`,
where `ExecutionDelta` contains the per-call deltas the cycle's
counters apply to running totals.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.dashboard_api import DashboardState
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.main_helpers.directional_opportunity import (
    build_directional_opportunity_from_signal,
)
from polymarket_arb.main_helpers.strategy_telemetry import (
    compact_book_snapshot,
    ensure_signal_id,
    new_execution_id,
)
from polymarket_arb.main_helpers.signal_helpers import (
    apply_maker_fill_to_inventory,
    find_market_for_signal,
    set_signal_execution_check,
    sum_trade_exposure,
)
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    MarketInfo,
    OrderSide,
    TradeRecord,
    TradeStatus,
)
from polymarket_arb.notifier import NotificationManager
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.maker_strategy import MakerStrategy
from polymarket_arb.strategies.strategy_orchestrator import (
    StrategyOrchestrator,
    StrategySignal,
    StrategyTier,
)

if TYPE_CHECKING:
    from polymarket_arb.strategies.recent_exit_cooldown import RecentExitCooldownStore
    from polymarket_arb.strategies.t2_exit_manager import T2ExitManager


@dataclass
class ExecutionDelta:
    """Per-call counter deltas applied by the cycle aggregator.

    `theoretical_opportunities` is set by the cycle (not by the
    execution dispatcher) — it represents arbs the orchestrator
    *considered*. The other six counters are mutually exclusive within
    a single call: a successful live fill bumps `live_successes` /
    `live_profit_total`; a submitted but not-yet-filled live order
    bumps `live_submissions`; the simulated equivalents apply in
    dry-run.
    """

    theoretical_opportunities: int = 0
    live_successes: int = 0
    simulated_successes: int = 0
    live_submissions: int = 0
    simulated_submissions: int = 0
    live_profit_total: float = 0.0
    simulated_profit_total: float = 0.0


def execute_strategy_signal(
    *,
    signal: StrategySignal,
    config: ArbConfig,
    active_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    orchestrator: StrategyOrchestrator,
    dash_state: DashboardState,
    event_recorder: EventRecorder,
    maker_strategy: MakerStrategy,
    notifier: NotificationManager,
    t2_exit_manager: "T2ExitManager | None" = None,
    cooldown_store: "RecentExitCooldownStore | None" = None,
) -> tuple[bool, str, ExecutionDelta]:
    """Dispatch a `StrategySignal` to the right tier executor.

    Returns `(success, reason, delta)`:

    - `success` is `True` whenever an order was successfully submitted
      (T1: simulated trade recorded; T2: trades executed; T3: limit
      order accepted by the venue with status PENDING/PARTIAL/FILLED).
    - `reason` is a stable identifier for failed paths
      (`"market_not_found"`, `"non_binary_market"`, …) — telemetry
      consumers count these.
    - `delta` accumulates into the running cycle counters.

    Tier dispatch:

    - `CROSS_PLATFORM` — dry-run only. Live mode returns
      `"cross_platform_live_requires_external_executor"`.
    - `STATISTICAL_ARB` — full T2 flow with risk + collateral gating.
      Successful fills are reported to `T2ExitManager` so the exit
      policy starts tracking.
    - `MARKET_MAKING` — quote selection + post-only limit order. Buy
      side runs through risk + collateral; sell-from-inventory side
      skips risk because we already own the asset.
    - Any other tier returns `"unsupported_strategy_tier"`.
    """
    signal_id = ensure_signal_id(signal)
    execution_id = new_execution_id()
    market = find_market_for_signal(signal.market_id, active_markets)
    if signal.tier == StrategyTier.CROSS_PLATFORM:
        if not config.dry_run:
            return False, "cross_platform_live_requires_external_executor", ExecutionDelta()
        pair_cost = max(float(signal.payload.get("total_cost", 0.0) or 0.0), 1e-9)
        bundle_size = max(0.0, float(signal.recommended_size_usdc)) / pair_cost
        trades = [
            TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=str(uuid.uuid4())[:12],
                token_id=str(signal.payload.get("pair_id", "poly")),
                condition_id=signal.market_id,
                side=OrderSide.BUY,
                price=float(signal.payload.get("poly_cost", 0.0) or 0.0),
                size=bundle_size,
                status=TradeStatus.FILLED,
                fill_price=float(signal.payload.get("poly_cost", 0.0) or 0.0),
                fill_size=bundle_size,
                economic_cost=float(signal.payload.get("poly_cost", 0.0) or 0.0),
                simulated=True,
            ),
            TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=str(uuid.uuid4())[:12],
                token_id=f"kalshi:{signal.payload.get('pair_id', 'pair')}",
                condition_id=f"kalshi:{signal.market_id}",
                side=OrderSide.BUY,
                price=float(signal.payload.get("kalshi_cost", 0.0) or 0.0),
                size=bundle_size,
                status=TradeStatus.FILLED,
                fill_price=float(signal.payload.get("kalshi_cost", 0.0) or 0.0),
                fill_size=bundle_size,
                economic_cost=float(signal.payload.get("kalshi_cost", 0.0) or 0.0),
                simulated=True,
            ),
        ]
        if event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "execution_id": execution_id,
                "signal_id": signal_id,
                "tier": signal.tier.name,
                "signal_type": signal.signal_type,
                "market_id": signal.market_id,
                "status": "simulated",
                "trade_count": len(trades),
            })
        return True, "", ExecutionDelta(simulated_successes=1)

    if signal.tier == StrategyTier.STATISTICAL_ARB:
        if market is None:
            set_signal_execution_check(signal, reason="market_not_found")
            return False, "market_not_found", ExecutionDelta()
        opportunity, target_size, build_reason = build_directional_opportunity_from_signal(
            config=config,
            signal=signal,
            market=market,
            ob_analyzer=ob_analyzer,
            cooldown_store=cooldown_store,
        )
        if opportunity is None:
            return False, build_reason, ExecutionDelta()
        can_trade, reason, adj_size = risk_mgr.pre_trade_check(opportunity, target_size)
        if not can_trade:
            return False, reason, ExecutionDelta()
        balance_ok, balance_reason, _ = executor.ensure_sufficient_collateral(opportunity.total_cost * adj_size)
        if not balance_ok:
            return False, balance_reason, ExecutionDelta()
        virtual_fill_context = {
            "signal_id": signal_id,
            "execution_id": execution_id,
            "tier": signal.tier.name,
            "signal_type": signal.signal_type,
            "market_id": signal.market_id,
            "event_title": opportunity.event_title,
        }
        trades = executor.execute_arbitrage(
            opportunity,
            adj_size,
            virtual_fill_context=virtual_fill_context,
        )
        for trade in trades:
            trade.signal_id = signal_id
            trade.execution_id = execution_id
            trade.expected_edge_per_share = opportunity.net_edge
            trade.event_title = opportunity.event_title
        execution_success = executor.is_successful_execution(opportunity, trades)
        simulated_exec = bool(trades) and all(getattr(t, "simulated", False) for t in trades)
        # Always feed RiskManager so position / exposure / cooldown
        # tracking stays correct; in dry_run all trades are simulated
        # and never reach the venue.
        risk_mgr.record_execution(opportunity, trades)
        if t2_exit_manager is not None:
            t2_payload = dict(signal.payload or {})
            t2_payload.setdefault("signal_type", signal.signal_type)
            t2_exit_manager.register_fills(
                signal_payload=t2_payload,
                market=market,
                trades=trades,
            )
        orchestrator.record_execution(
            signal,
            success=execution_success,
            exposure_amount_usdc=sum_trade_exposure(trades, include_simulated=False),
        )
        if event_recorder.is_enabled:
            # `execution_check.category` was set by directional_opportunity
            # for T2 entries; surface it at row top-level so post-hoc
            # analysis doesn't have to dig two layers down.
            exec_check = dict(signal.payload.get("execution_check") or {})
            event_recorder.write_event("strategy_executions", {
                "execution_id": execution_id,
                "signal_id": signal_id,
                "tier": signal.tier.name,
                "signal_type": signal.signal_type,
                "market_id": signal.market_id,
                "status": "executed" if execution_success else "attempted",
                "trade_count": len(trades),
                "arb_type": opportunity.arb_type.value,
                "execution_check": exec_check,
                "our_role": "taker",
                "category": exec_check.get("category", ""),
                "trades": [_trade_link_payload(trade) for trade in trades],
            })
        for trade in trades:
            dash_state.append_trade({
                "trade_id": trade.trade_id,
                "arb_id": trade.arb_id,
                "side": trade.side.value,
                "price": trade.price,
                "size": trade.size,
                "status": trade.status.value,
                "token_id": trade.token_id[:20],
                "timestamp": trade.timestamp,
                "simulated": trade.simulated,
            })
        filled_legs_count = sum(1 for t in trades if t.status == TradeStatus.FILLED)
        if execution_success:
            notifier.notify_trade_success(
                event_title=opportunity.event_title,
                arb_type=f"T2_{opportunity.arb_type.value}",
                filled_legs=filled_legs_count,
                total_legs=len(opportunity.legs),
                expected_profit=opportunity.net_edge * adj_size,
                simulated=simulated_exec,
            )
        elif trades and not config.dry_run:
            failure_reasons = sorted({
                str(getattr(t, "error", "")).strip()
                for t in trades
                if str(getattr(t, "error", "")).strip()
            })
            notifier.notify_trade_failure(
                event_title=opportunity.event_title,
                filled_legs=filled_legs_count,
                total_legs=len(opportunity.legs),
                simulated=simulated_exec,
                details="; ".join(failure_reasons[:2]),
            )
        delta = ExecutionDelta()
        if execution_success:
            if simulated_exec:
                delta.simulated_successes = 1
                delta.simulated_profit_total = opportunity.net_edge * adj_size
            else:
                delta.live_successes = 1
                delta.live_profit_total = opportunity.net_edge * adj_size
        return True, "", delta

    if signal.tier == StrategyTier.MARKET_MAKING:
        if market is None:
            return False, "market_not_found", ExecutionDelta()
        if len(market.tokens) < 2:
            return False, "non_binary_market", ExecutionDelta()
        quote = signal.payload.get("quote", {}) if isinstance(signal.payload.get("quote", {}), dict) else {}
        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        no_token = next((t for t in market.tokens if (t.outcome or "").lower() == "no"), market.tokens[-1])
        fair_value = float(quote.get("fair_value") or 0.0)
        bid_price = float(quote.get("bid_price") or 0.0)
        ask_price = float(quote.get("ask_price") or 0.0)
        bid_size = float(quote.get("bid_size") or 0.0)
        ask_size = float(quote.get("ask_size") or 0.0)
        bid_edge = max(0.0, fair_value - bid_price) if bid_price > 0 else 0.0
        ask_edge = max(0.0, ask_price - fair_value) if ask_price > 0 else 0.0

        yes_inventory = max(0.0, float(maker_strategy.get_inventory(yes_token.token_id)))
        no_inventory = max(0.0, float(maker_strategy.get_inventory(no_token.token_id)))
        candidates: list[dict[str, Any]] = []
        if yes_inventory > 0 and ask_edge > 0 and ask_price > 0 and ask_size > 0:
            candidates.append({
                "is_exit": True,
                "maker_side": "sell_yes_inventory",
                "token": yes_token,
                "outcome": "Yes",
                "side": OrderSide.SELL,
                "price": ask_price,
                "size": min(ask_size, yes_inventory),
                "edge": ask_edge,
            })
        if no_inventory > 0 and bid_edge > 0 and bid_price > 0 and bid_size > 0:
            candidates.append({
                "is_exit": True,
                "maker_side": "sell_no_inventory",
                "token": no_token,
                "outcome": "No",
                "side": OrderSide.SELL,
                "price": 1.0 - bid_price,
                "size": min(bid_size, no_inventory),
                "edge": bid_edge,
            })
        if bid_edge > 0 and bid_price > 0 and bid_size > 0:
            candidates.append({
                "is_exit": False,
                "maker_side": "buy_yes",
                "token": yes_token,
                "outcome": "Yes",
                "side": OrderSide.BUY,
                "price": bid_price,
                "size": bid_size,
                "edge": bid_edge,
            })
        if ask_edge > 0 and ask_price > 0 and ask_size > 0:
            candidates.append({
                "is_exit": False,
                "maker_side": "buy_no_from_yes_ask",
                "token": no_token,
                "outcome": "No",
                "side": OrderSide.BUY,
                "price": 1.0 - ask_price,
                "size": ask_size,
                "edge": ask_edge,
            })
        candidates = [
            candidate for candidate in candidates
            if 0 < float(candidate["price"]) < 1 and float(candidate["size"]) > 0
        ]
        if not candidates:
            return False, "maker_no_executable_side", ExecutionDelta()
        chosen = max(candidates, key=lambda item: (bool(item["is_exit"]), float(item["edge"])))
        maker_side = str(chosen["maker_side"])
        target_token = chosen["token"]
        target_outcome = str(chosen["outcome"])
        target_order_side = chosen["side"]
        target_price = float(chosen["price"])
        target_size = float(chosen["size"])
        side_edge = float(chosen["edge"])
        # The maker quote came from a fresh orderbook snapshot upstream; query
        # one more time so the limit price quantizes to the real market tick.
        target_snap = None
        if ob_analyzer is not None and hasattr(ob_analyzer, "get_snapshot"):
            try:
                target_snap = ob_analyzer.get_snapshot(target_token.token_id)
            except Exception:
                target_snap = None
        target_tick_size = float(getattr(target_snap, "tick_size", 0.01) or 0.01)
        maker_opp = ArbOpportunity(
            arb_type=ArbType.MARKET_MAKING,
            event_id=market.event_id or market.condition_id,
            event_title=market.question,
            markets=[market],
            total_cost=target_price,
            guaranteed_payout=1.0,
            gross_edge=side_edge,
            net_edge=side_edge,
            edge_pct=((side_edge / target_price) * 100.0) if target_price > 0 else 0.0,
            legs=[
                ArbLeg(
                    token_id=target_token.token_id,
                    condition_id=market.condition_id,
                    outcome=target_outcome,
                    side=target_order_side,
                    price=target_price,
                    size=target_size,
                    available_size=target_size,
                    execution_price=target_price,
                    economic_cost=target_price,
                    tick_size=target_tick_size,
                )
            ],
            max_executable_size=target_size,
            confidence=float(signal.confidence),
        )
        if target_order_side == OrderSide.BUY:
            can_trade, reason, adj_size = risk_mgr.pre_trade_check(maker_opp, target_size)
            if not can_trade:
                return False, reason, ExecutionDelta()
            balance_ok, balance_reason, _ = executor.ensure_sufficient_collateral(target_price * adj_size)
            if not balance_ok:
                return False, balance_reason, ExecutionDelta()
        else:
            adj_size = target_size
        trade = executor.submit_limit_order(
            token_id=target_token.token_id,
            condition_id=market.condition_id,
            outcome=target_outcome,
            side=target_order_side,
            price=target_price,
            size=adj_size,
            post_only=True,
            order_type_name="GTC",
            tick_size=target_tick_size,
            virtual_fill_context={
                "signal_id": signal_id,
                "execution_id": execution_id,
                "tier": signal.tier.name,
                "signal_type": signal.signal_type,
                "market_id": signal.market_id,
                "event_title": market.question,
                "maker_side": maker_side,
            },
        )
        trade.expected_edge_per_share = side_edge
        trade.event_title = market.question
        trade.signal_id = signal_id
        trade.execution_id = execution_id
        submission_success = trade.status in {TradeStatus.PENDING, TradeStatus.PARTIAL, TradeStatus.FILLED}
        if target_order_side == OrderSide.BUY:
            risk_mgr.record_execution(
                maker_opp,
                [trade],
                count_pending_as_failure=False,
            )
        orchestrator.record_execution(
            signal,
            success=submission_success,
            exposure_amount_usdc=(
                sum_trade_exposure([trade], include_simulated=False)
                if target_order_side == OrderSide.BUY
                else 0.0
            ),
        )
        if trade.fill_size:
            apply_maker_fill_to_inventory(maker_strategy, trade)
        dash_state.append_trade({
            "trade_id": trade.trade_id,
            "arb_id": trade.arb_id,
            "side": trade.side.value,
            "price": trade.price,
            "size": trade.size,
            "status": trade.status.value,
            "token_id": trade.token_id[:20],
            "timestamp": trade.timestamp,
            "simulated": trade.simulated,
            "post_only": True,
        })
        if event_recorder.is_enabled:
            event_recorder.write_event("strategy_executions", {
                "execution_id": execution_id,
                "signal_id": signal_id,
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "tier": signal.tier.name,
                "signal_type": signal.signal_type,
                "market_id": signal.market_id,
                "status": "submitted" if submission_success else "failed",
                "trade_status": trade.status.value,
                "submitted_price": trade.price,
                "submitted_size": trade.size,
                "submitted_notional": trade.price * trade.size,
                "filled_size": trade.fill_size,
                "fill_price": trade.fill_price,
                "maker_side": maker_side,
                "outcome": target_outcome,
                "side": target_order_side.value,
                "expected_edge_per_share": side_edge,
                "expected_edge_usdc": side_edge * float(adj_size),
                "fair_value": fair_value,
                "quote_bid_price": bid_price,
                "quote_ask_price": ask_price,
                "quote_bid_size": bid_size,
                "quote_ask_size": ask_size,
                "inventory_before": {
                    "yes": yes_inventory,
                    "no": no_inventory,
                },
                "book_snapshot": compact_book_snapshot(target_snap),
                "post_only": True,
                # Becker 2025 follow-up: tag every fill with our role
                # (maker/taker) and the article's category so we can
                # later estimate per-category taker-yes-share and
                # validate maker-only is actually profitable for us.
                "our_role": "maker",
                "category": (signal.payload.get("category") if isinstance(signal.payload, dict) else None) or "",
                "category_maker_taker_gap_pp": (
                    signal.payload.get("category_maker_taker_gap_pp")
                    if isinstance(signal.payload, dict) else None
                ),
                "flow_bias": (
                    signal.payload.get("flow_bias")
                    if isinstance(signal.payload, dict) else None
                ),
            })
        simulated_maker = bool(getattr(trade, "simulated", False))
        expected_edge_usdc = side_edge
        if trade.status == TradeStatus.FILLED:
            notifier.notify_trade_success(
                event_title=market.question,
                arb_type="T3_market_making",
                filled_legs=1,
                total_legs=1,
                expected_profit=expected_edge_usdc * float(trade.fill_size or adj_size),
                simulated=simulated_maker,
            )
        elif trade.status in (TradeStatus.FAILED, TradeStatus.CANCELLED) and not config.dry_run:
            notifier.notify_trade_failure(
                event_title=market.question,
                filled_legs=0,
                total_legs=1,
                simulated=simulated_maker,
                details=str(getattr(trade, "error", "") or "").strip(),
            )
        delta = ExecutionDelta()
        if submission_success:
            if trade.status == TradeStatus.FILLED:
                filled_size = float(trade.fill_size or adj_size)
                if simulated_maker:
                    delta.simulated_successes = 1
                    delta.simulated_profit_total = expected_edge_usdc * filled_size
                else:
                    delta.live_successes = 1
                    delta.live_profit_total = expected_edge_usdc * filled_size
            else:
                if simulated_maker:
                    delta.simulated_submissions = 1
                else:
                    delta.live_submissions = 1
        return submission_success, "", delta

    return False, "unsupported_strategy_tier", ExecutionDelta()


def _trade_link_payload(trade: TradeRecord) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "token_id": trade.token_id,
        "condition_id": trade.condition_id,
        "side": trade.side.value,
        "price": trade.price,
        "size": trade.size,
        "status": trade.status.value,
        "fill_price": trade.fill_price,
        "fill_size": trade.fill_size,
        "post_only": trade.post_only,
        "simulated": trade.simulated,
    }
