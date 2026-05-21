"""Pure serialisation helpers used by the run-loop telemetry path.

Every function here turns a domain object (`ArbOpportunity`, `TradeRecord`,
`MarketInfo`, `StrategySignal`, …) into a plain `dict` ready for the
dashboard FastAPI layer, NDJSON event log, or backtest/reporting code.
They are deliberately side-effect free so they can be:

- snapshotted in tests without booting the loop,
- reused by other entry points (e.g. backtest reports), and
- re-ordered freely by the upcoming `main_loop` decomposition.

`time.time()` calls in `_summarize_market_catalog` /
`_build_dashboard_trade_rows` are the only "side effect" — they only stamp
an `updated_at` field and remain trivially testable with `freezegun` or by
asserting the field exists.
"""

from __future__ import annotations

import time
from typing import Any

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.config import ArbConfig
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.models import ArbOpportunity, MarketInfo
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal


def has_simulated_trades(trades: list[Any]) -> bool:
    return any(bool(getattr(trade, "simulated", False)) for trade in trades)


def reported_execution_success(*, live_execution_success: bool, trades: list[Any]) -> bool:
    """Treat a fully-filled simulated batch as 'success' for dashboard rows.

    The live executor returns False for simulated trades because no real
    orders went out, but the dashboard PnL preview wants to know
    whether the *strategy* would have worked in dry-run, so we promote
    "all legs filled" to True only when the batch is simulated.
    """
    if has_simulated_trades(trades):
        filled = [
            trade for trade in trades
            if getattr(getattr(trade, "status", None), "value", "") == "filled"
        ]
        return bool(trades) and len(filled) == len(trades)
    return live_execution_success


def estimate_trade_outcome(
    verified: ArbOpportunity,
    trades: list[Any],
    arb_success: bool,
    adj_size: float,
) -> float:
    """Estimate realised PnL for an executed opportunity.

    Used by dashboard and telemetry PnL previews. On success, PnL =
    `net_edge * min(filled_sizes)` since structural arbitrage profit
    is bounded by the smallest leg fill. On failure, we count the cash that
    actually moved (sum of `economic_cost * fill_size`); if no leg even
    partially filled, we fall back to the full theoretical entry cost as a
    pessimistic upper bound on slippage damage.
    """
    if arb_success:
        filled_sizes = [
            float(t.fill_size or t.size or 0.0)
            for t in trades
            if getattr(t, "status", None) and t.status.value == "filled"
        ]
        realized_size = min(filled_sizes) if filled_sizes else adj_size
        return verified.net_edge * realized_size

    realized_cost = 0.0
    for trade in trades:
        fill_size = float(getattr(trade, "fill_size", None) or 0.0)
        if fill_size <= 0:
            continue
        leg_cost = getattr(trade, "economic_cost", None)
        if leg_cost is None:
            leg_cost = getattr(trade, "price", 0.0)
        realized_cost += float(leg_cost) * fill_size

    if realized_cost > 0:
        return -realized_cost
    return -verified.total_cost * adj_size


def serialize_opportunity_event(opp: ArbOpportunity, *, stage: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "arb_type": opp.arb_type.value,
        "event_id": opp.event_id,
        "event_title": opp.event_title,
        "total_cost": opp.total_cost,
        "net_edge": opp.net_edge,
        "edge_pct": opp.edge_pct,
        "confidence": opp.confidence,
        "max_executable_size": opp.max_executable_size,
        "legs": [
            {
                "token_id": leg.token_id,
                "condition_id": leg.condition_id,
                "outcome": leg.outcome,
                "side": leg.side.value,
                "price": leg.price,
                "execution_price": leg.execution_price,
                "economic_cost": leg.economic_cost,
                "size": leg.size,
                "available_size": leg.available_size,
            }
            for leg in opp.legs
        ],
    }


def serialize_trade_execution(
    opp: ArbOpportunity,
    trades: list[Any],
    arb_success: bool,
    adj_size: float,
) -> dict[str, Any]:
    simulated = has_simulated_trades(trades)
    reported_success = reported_execution_success(
        live_execution_success=arb_success,
        trades=trades,
    )
    return {
        "arb_type": opp.arb_type.value,
        "event_id": opp.event_id,
        "event_title": opp.event_title,
        "arb_success": reported_success,
        "live_execution_success": arb_success,
        "simulated": simulated,
        "requested_size": adj_size,
        "expected_net_edge": opp.net_edge,
        "expected_total_cost": opp.total_cost,
        "trade_outcome_estimate": estimate_trade_outcome(opp, trades, reported_success, adj_size),
        "trades": [
            {
                "trade_id": getattr(trade, "trade_id", ""),
                "token_id": getattr(trade, "token_id", ""),
                "condition_id": getattr(trade, "condition_id", ""),
                "side": getattr(getattr(trade, "side", None), "value", ""),
                "status": getattr(getattr(trade, "status", None), "value", ""),
                "price": getattr(trade, "price", None),
                "size": getattr(trade, "size", None),
                "fill_price": getattr(trade, "fill_price", None),
                "fill_size": getattr(trade, "fill_size", None),
                "economic_cost": getattr(trade, "economic_cost", None),
                "order_id": getattr(trade, "order_id", None),
                "error": getattr(trade, "error", None),
                "rolled_back": getattr(trade, "rolled_back", False),
            }
            for trade in trades
        ],
    }


def build_dashboard_trade_rows(
    *,
    opp: ArbOpportunity,
    trades: list[Any],
    live_execution_success: bool,
    dashboard_execution_success: bool,
) -> list[dict[str, Any]]:
    """Flatten an opportunity into one dashboard row per leg.

    `expected_profit` uses the *minimum* leg size so the per-leg row
    reports the same conservative PnL estimate every time, instead of
    different numbers per leg that would confuse the UI.
    """
    mode = "simulated" if has_simulated_trades(trades) else "live"
    expected_profit = estimate_trade_outcome(
        opp,
        trades,
        dashboard_execution_success,
        min(
            [float(getattr(trade, "size", 0.0) or 0.0) for trade in trades] or [0.0]
        ),
    )
    rows: list[dict[str, Any]] = []
    for trade in trades:
        rows.append({
            "trade_id": getattr(trade, "trade_id", ""),
            "arb_id": getattr(trade, "arb_id", ""),
            "event_title": opp.event_title,
            "arb_type": opp.arb_type.value,
            "mode": mode,
            "execution_success": dashboard_execution_success,
            "live_execution_success": live_execution_success,
            "side": getattr(getattr(trade, "side", None), "value", ""),
            "price": getattr(trade, "price", None),
            "size": getattr(trade, "size", None),
            "status": getattr(getattr(trade, "status", None), "value", ""),
            "token_id": getattr(trade, "token_id", "")[:20],
            "outcome": getattr(trade, "outcome", None),
            "timestamp": getattr(trade, "timestamp", time.time()),
            "expected_profit": expected_profit,
        })
    return rows


def serialize_strategy_signal(
    signal: StrategySignal,
    *,
    submitted: bool,
    research_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "signal_id": getattr(signal, "signal_id", ""),
        "tier": signal.tier.name,
        "signal_type": signal.signal_type,
        "market_id": signal.market_id,
        "description": signal.description,
        "expected_edge": signal.expected_edge,
        "confidence": signal.confidence,
        "recommended_size_usdc": signal.recommended_size_usdc,
        "urgency": signal.urgency,
        "submitted": submitted,
        "research_overlay": dict(research_overlay or {}),
        "payload": dict(signal.payload),
        "timestamp": signal.timestamp,
    }


def lookup_market_snapshot(market_id: str, market_catalog: dict[str, dict]) -> dict[str, Any]:
    """Return a shallow copy of a catalogued market entry, or {} if missing.

    Falls back to a prefix lookup so callers can pass either a full
    `condition_id` or its truncated variant from a log line.
    """
    if not market_id:
        return {}
    if market_id in market_catalog:
        return dict(market_catalog[market_id])
    for key, value in market_catalog.items():
        if key.startswith(market_id) or market_id.startswith(key):
            return dict(value)
    return {}


def resolve_market_yes_price(market: MarketInfo) -> float | None:
    """Best-effort YES-side price extraction.

    Tries in order: an explicit YES token's live price, the corresponding
    `outcome_prices` slot, then any 0<price<1 candidate as a last resort.
    Returns None when no usable probability is available.
    """
    for idx, token in enumerate(market.tokens):
        outcome = (token.outcome or "").strip().lower()
        if outcome == "yes":
            if 0.0 < float(token.price or 0.0) < 1.0:
                return float(token.price)
            if idx < len(market.outcome_prices):
                price = float(market.outcome_prices[idx])
                if 0.0 < price < 1.0:
                    return price

    for price in market.outcome_prices:
        numeric = float(price)
        if 0.0 < numeric < 1.0:
            return numeric

    for token in market.tokens:
        numeric = float(token.price or 0.0)
        if 0.0 < numeric < 1.0:
            return numeric
    return None


def summarize_market_catalog(markets: list[MarketInfo], limit: int = 300) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for market in markets[:limit]:
        catalog[market.condition_id] = {
            "question": market.question,
            "yes_price": resolve_market_yes_price(market),
            "volume_24h": market.volume_24h,
            "liquidity": market.liquidity,
            "updated_at": time.time(),
        }
    return catalog


def is_live_execution_success(
    config: ArbConfig,
    executor: ExecutionEngine,
    opp: ArbOpportunity,
    trades: list[Any],
) -> bool:
    """Wrap the executor's success check with a hard `dry_run` guard.

    Avoids noisy "live success" telemetry rows when no real orders were
    submitted. The executor's own check is conservative enough but the
    extra guard keeps the dashboard semantics unambiguous.
    """
    if config.dry_run:
        return False
    return executor.is_successful_execution(opp, trades)


def serialize_recent_trade(trade: Any) -> dict:
    if hasattr(trade, "__dict__"):
        return {
            "trade_id": getattr(trade, "trade_id", ""),
            "token_id": getattr(trade, "token_id", ""),
            "status": getattr(getattr(trade, "status", None), "value", ""),
            "price": getattr(trade, "price", None),
            "size": getattr(trade, "size", None),
            "timestamp": getattr(trade, "timestamp", None),
        }
    return dict(trade)


def build_ws_status(
    *,
    config: ArbConfig,
    enhanced_store: EnhancedBookStore,
    ws_target_ids: list[str],
    phase_hint: str,
    scanned_orderbooks: int = 0,
    scanned_events: int = 0,
) -> dict:
    """Compute the dashboard `ws_status` block from raw WS state.

    `phase` is derived rather than set externally so a writer that forgets
    to update it on reconnect cannot leave the dashboard reporting a stale
    label. `phase_hint` is only consulted as a fallback when the store has
    no markets / no connection signal yet.
    """
    book_snapshot = enhanced_store.snapshot()
    connected = (
        bool(book_snapshot.get("connected"))
        and book_snapshot.get("ts_ms") is not None
        and book_snapshot.get("ts_ms", 0) > 0
    )
    if connected:
        phase = "connected"
    elif not config.ws_enabled:
        phase = "disabled"
    elif ws_target_ids:
        phase = "initializing"
    else:
        phase = phase_hint
    return {
        "enabled": config.ws_enabled,
        "connected": connected,
        "market_id": book_snapshot.get("market_id"),
        "subscribed_tokens": len(ws_target_ids),
        "phase": phase,
        "scanned_orderbooks": scanned_orderbooks,
        "scanned_events": scanned_events,
    }
