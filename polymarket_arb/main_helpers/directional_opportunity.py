"""Build a DIRECTIONAL `ArbOpportunity` from a strategy signal.

Extracted from `main_loop.py` because the function is pure
(no module-level state, no side effects beyond writing diagnostic
metadata onto the signal itself via `set_signal_execution_check`)
and reasonably long (~140 lines of gating + math + payload assembly).

The function returns a 3-tuple `(opportunity, target_size, reason)`:

- On success: `(ArbOpportunity, target_size, "")`.
- On rejection: `(None, 0.0, reason_string)` where `reason_string`
  matches the value also written to `signal.payload["execution_check"]`
  for downstream telemetry.

Rejection reasons (stable identifiers — telemetry consumers count them):

- `unsupported_direction` — signal action is not BUY_YES/BUY_NO
- `orderbook_feed_unhealthy` — live mode and the WS/feed health gate failed
- `non_binary_market` — market has fewer than two outcomes
- `missing_best_ask` — no usable ask quote for the target token
- `non_positive_notional` — `recommended_size_usdc <= 0`
- `insufficient_depth` — book depth doesn't cover `target_size`
- `zero_fillable_size` — depth scan returned a size <= 0
- `edge_below_fee` — gross edge does not cover taker fee
- `live_edge_below_buffer` — live mode and net edge below the
  configured buffer (`live_min_net_edge_usd` / `_bps`)
"""

from __future__ import annotations

from polymarket_arb.config import ArbConfig
from polymarket_arb.main_helpers.signal_helpers import (
    resolve_strategy_signal_action,
    set_signal_execution_check,
)
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    FeeStructure,
    MarketInfo,
    OrderSide,
)
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal


def build_directional_opportunity_from_signal(
    *,
    config: ArbConfig,
    signal: StrategySignal,
    market: MarketInfo,
    ob_analyzer: OrderBookAnalyzer,
) -> tuple[ArbOpportunity | None, float, str]:
    """Convert a directional T2/T3 signal into an executable `ArbOpportunity`.

    Walks the gating chain (direction → feed health → market shape →
    quote → notional → depth → edge vs fees → live-mode buffer) and
    bails out on the first failure with a stable reason string. The
    full diagnostic payload is mirrored onto `signal.payload` via
    `set_signal_execution_check` so telemetry / dashboard can show
    *why* a signal was filtered without re-running the math.
    """
    action = resolve_strategy_signal_action(signal)
    if action not in {"BUY_YES", "BUY_NO"}:
        set_signal_execution_check(signal, reason="unsupported_direction", action=action)
        return None, 0.0, "unsupported_direction"

    if not config.dry_run and hasattr(ob_analyzer, "feed_health"):
        health = ob_analyzer.feed_health(
            max_snapshot_age_sec=config.live_max_orderbook_snapshot_age_sec,
            min_ws_hit_ratio=config.live_min_ws_hit_ratio,
        )
        if not bool(health.get("healthy", False)):
            set_signal_execution_check(
                signal,
                reason="orderbook_feed_unhealthy",
                action=action,
                feed_health_reason=health.get("reason", ""),
            )
            return None, 0.0, "orderbook_feed_unhealthy"

    if len(market.tokens) < 2:
        set_signal_execution_check(signal, reason="non_binary_market", action=action)
        return None, 0.0, "non_binary_market"

    yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
    no_token = next((t for t in market.tokens if (t.outcome or "").lower() == "no"), market.tokens[-1])
    target_token = yes_token if action == "BUY_YES" else no_token
    outcome_label = "Yes" if action == "BUY_YES" else "No"

    snap = ob_analyzer.get_snapshot(target_token.token_id)
    if snap is None or snap.best_ask is None or snap.best_ask <= 0:
        set_signal_execution_check(
            signal,
            reason="missing_best_ask",
            action=action,
            token_id=target_token.token_id[:20],
        )
        return None, 0.0, "missing_best_ask"

    target_notional = max(0.0, float(signal.recommended_size_usdc))
    if target_notional <= 0:
        set_signal_execution_check(signal, reason="non_positive_notional", action=action)
        return None, 0.0, "non_positive_notional"
    target_size = target_notional / float(snap.best_ask)
    executable = ob_analyzer.get_executable_ask_price(target_token.token_id, target_size)
    if executable is None:
        set_signal_execution_check(
            signal,
            reason="insufficient_depth",
            action=action,
            best_ask=float(snap.best_ask),
            target_notional=target_notional,
            target_size=target_size,
        )
        return None, 0.0, "insufficient_depth"
    execution_price, fillable_size = executable
    if fillable_size <= 0:
        set_signal_execution_check(
            signal,
            reason="zero_fillable_size",
            action=action,
            best_ask=float(snap.best_ask),
            execution_price=float(execution_price),
            target_size=target_size,
        )
        return None, 0.0, "zero_fillable_size"

    gross_edge = abs(float(signal.payload.get("deviation", 0.0) or (signal.expected_edge / 10_000.0)))
    fee_estimate = FeeStructure.for_market(config.polymarket_taker_fee_rate, market).estimate_price_fee(
        float(execution_price)
    )
    net_edge = gross_edge - fee_estimate
    check_payload = {
        "action": action,
        "token_id": target_token.token_id[:20],
        "target_notional": target_notional,
        "target_size": target_size,
        "fillable_size": float(fillable_size),
        "best_ask": float(snap.best_ask),
        "execution_price": float(execution_price),
        "gross_edge": gross_edge,
        "fee_estimate": fee_estimate,
        "net_edge": net_edge,
        "gross_edge_bps": gross_edge * 10_000.0,
        "fee_estimate_bps": fee_estimate * 10_000.0,
        "net_edge_bps": net_edge * 10_000.0,
        "model_prob": signal.payload.get("model_prob"),
        "market_prob": signal.payload.get("market_prob"),
        "fee_model": "clob_binary_fee_rate_x_price_x_1_minus_price",
        "fee_rate": FeeStructure.for_market(config.polymarket_taker_fee_rate, market).taker_fee_rate,
    }
    if net_edge <= 0:
        set_signal_execution_check(signal, reason="edge_below_fee", **check_payload)
        return None, 0.0, "edge_below_fee"
    if not config.dry_run:
        min_edge_by_bps = float(execution_price) * (config.live_min_net_edge_bps / 10_000.0)
        min_live_edge = max(config.live_min_net_edge_usd, min_edge_by_bps)
        check_payload["live_min_net_edge"] = min_live_edge
        check_payload["live_min_net_edge_bps"] = config.live_min_net_edge_bps
        if net_edge < min_live_edge:
            set_signal_execution_check(signal, reason="live_edge_below_buffer", **check_payload)
            return None, 0.0, "live_edge_below_buffer"
    set_signal_execution_check(signal, reason="", **check_payload)

    opportunity = ArbOpportunity(
        arb_type=ArbType.DIRECTIONAL,
        event_id=market.event_id or market.condition_id,
        event_title=market.question,
        markets=[market],
        total_cost=float(execution_price),
        guaranteed_payout=1.0,
        gross_edge=gross_edge,
        net_edge=net_edge,
        edge_pct=(net_edge / float(execution_price)) * 100.0 if execution_price > 0 else 0.0,
        legs=[
            ArbLeg(
                token_id=target_token.token_id,
                condition_id=market.condition_id,
                outcome=outcome_label,
                side=OrderSide.BUY,
                price=float(execution_price),
                size=float(target_size),
                available_size=float(fillable_size),
                execution_price=float(execution_price),
                economic_cost=float(execution_price),
            )
        ],
        max_executable_size=float(fillable_size),
        confidence=float(signal.confidence),
    )
    return opportunity, float(target_size), ""
