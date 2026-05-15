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

import time
from datetime import datetime, timezone

from polymarket_arb.config import ArbConfig
from polymarket_arb.main_helpers.market_category import (
    classify_market_category,
    is_near_efficient,
)
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
from polymarket_arb.strategies.recent_exit_cooldown import RecentExitCooldownStore
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal


def _market_horizon_days(market: MarketInfo) -> float | None:
    """Days until market end_date, or None if no end_date is set.

    Mirrors the helper in `signal_collectors` so callers don't need to
    cross-import; both functions stay in sync because the logic is one-liner.
    """
    if not market.end_date:
        return None
    try:
        end_dt = datetime.fromisoformat(str(market.end_date).replace("Z", "+00:00"))
        return (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return None


def build_directional_opportunity_from_signal(
    *,
    config: ArbConfig,
    signal: StrategySignal,
    market: MarketInfo,
    ob_analyzer: OrderBookAnalyzer,
    cooldown_store: RecentExitCooldownStore | None = None,
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

    # Post-exit cooldown gate. Runs early so we don't waste depth-verify /
    # fee math on a market the operator (or the abandon path) just closed.
    if cooldown_store is not None:
        blocked, remaining_sec = cooldown_store.in_cooldown(market.condition_id)
        if blocked:
            set_signal_execution_check(
                signal,
                reason="recent_exit_cooldown",
                action=action,
                cooldown_remaining_sec=float(remaining_sec),
            )
            return None, 0.0, "recent_exit_cooldown"

    if len(market.tokens) < 2:
        set_signal_execution_check(signal, reason="non_binary_market", action=action)
        return None, 0.0, "non_binary_market"

    yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
    no_token = next((t for t in market.tokens if (t.outcome or "").lower() == "no"), market.tokens[-1])
    target_token = yes_token if action == "BUY_YES" else no_token
    outcome_label = "Yes" if action == "BUY_YES" else "No"

    if not config.dry_run and hasattr(ob_analyzer, "feed_health"):
        # Scope the staleness check to *this signal's* tokens. Without
        # scoping, a single idle hot-pool token whose mirror hasn't been
        # pushed in a few seconds would mark the entire feed unhealthy
        # and block every T2 signal in the system.
        health = ob_analyzer.feed_health(
            max_snapshot_age_sec=config.live_max_orderbook_snapshot_age_sec,
            min_ws_hit_ratio=config.live_min_ws_hit_ratio,
            token_ids=[yes_token.token_id, no_token.token_id],
        )
        if not bool(health.get("healthy", False)):
            set_signal_execution_check(
                signal,
                reason="orderbook_feed_unhealthy",
                action=action,
                feed_health_reason=health.get("reason", ""),
            )
            return None, 0.0, "orderbook_feed_unhealthy"

    snap = ob_analyzer.get_snapshot(target_token.token_id)
    if snap is None or snap.best_ask is None or snap.best_ask <= 0:
        set_signal_execution_check(
            signal,
            reason="missing_best_ask",
            action=action,
            token_id=target_token.token_id[:20],
        )
        return None, 0.0, "missing_best_ask"

    # Extreme-price gate (Becker 2025 longshot/favorite tax). Buying YES
    # below `t2_reject_price_below` averaged -41% EV on Polymarket;
    # buying NO at the symmetric high-price tail (= buying YES at low
    # complement price) is the same trade in reverse. Cut both before
    # depth + fee math — they are net-negative regardless of edge size.
    best_ask_price = float(snap.best_ask)
    reject_below = float(getattr(config, "t2_reject_price_below", 0.0) or 0.0)
    reject_above = float(getattr(config, "t2_reject_price_above", 1.0) or 1.0)
    if reject_below > 0.0 and best_ask_price < reject_below:
        set_signal_execution_check(
            signal,
            reason="price_extreme_longshot",
            action=action,
            best_ask=best_ask_price,
            reject_below=reject_below,
        )
        return None, 0.0, "price_extreme_longshot"
    if reject_above < 1.0 and best_ask_price > reject_above:
        set_signal_execution_check(
            signal,
            reason="price_extreme_favorite",
            action=action,
            best_ask=best_ask_price,
            reject_above=reject_above,
        )
        return None, 0.0, "price_extreme_favorite"

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
    snap_mid = getattr(snap, "mid", None)
    snap_spread = getattr(snap, "spread", None)
    snap_ts = float(getattr(snap, "timestamp", 0.0) or 0.0)
    snapshot_age_sec = max(0.0, time.time() - snap_ts) if snap_ts > 0 else None
    check_payload = {
        "action": action,
        "token_id": target_token.token_id[:20],
        "target_notional": target_notional,
        "target_size": target_size,
        "fillable_size": float(fillable_size),
        "best_ask": float(snap.best_ask),
        "best_bid": float(snap.best_bid) if snap.best_bid is not None else None,
        "mid": float(snap_mid) if snap_mid is not None else None,
        "spread": float(snap_spread) if snap_spread is not None else None,
        "spread_bps": (
            (float(snap_spread) / float(snap_mid)) * 10_000.0
            if snap_spread is not None and snap_mid is not None and float(snap_mid) > 0
            else None
        ),
        "snapshot_age_sec": snapshot_age_sec,
        "book_depth_top3": {
            "bids": [[float(level.price), float(level.size)] for level in snap.bids[:3]],
            "asks": [[float(level.price), float(level.size)] for level in snap.asks[:3]],
        },
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
        horizon_days = _market_horizon_days(market)
        # Long-horizon binary markets (or markets with no end_date at all)
        # are dominated by risk-premium pricing rather than mean reversion.
        # Demand a much higher edge there so a 99 bps "edge" on a 6-month
        # question can no longer slip through the live gate.
        is_long_horizon = (
            horizon_days is None
            or horizon_days > float(config.t2_long_horizon_days)
        )
        # Near-efficient categories (Finance / Crypto): Becker 2025 shows
        # the maker-taker gap is only 0.17 pp here. A 25 bps live edge
        # buffer is meaningless once 5% taker fees compound — bump to
        # the configured T2 near-efficient bar (default 300 bps).
        category = classify_market_category(market)
        near_efficient = is_near_efficient(category)
        # The active threshold is the strictest of the three:
        #   default `live_min_net_edge_bps`
        #   long-horizon override `t2_long_horizon_min_net_edge_bps`
        #   category override   `t2_near_efficient_min_net_edge_bps`
        effective_min_edge_bps = float(config.live_min_net_edge_bps)
        if is_long_horizon:
            effective_min_edge_bps = max(
                effective_min_edge_bps,
                float(config.t2_long_horizon_min_net_edge_bps),
            )
        if near_efficient:
            effective_min_edge_bps = max(
                effective_min_edge_bps,
                float(getattr(config, "t2_near_efficient_min_net_edge_bps", 0.0) or 0.0),
            )
        min_edge_by_bps = float(execution_price) * (effective_min_edge_bps / 10_000.0)
        min_live_edge = max(config.live_min_net_edge_usd, min_edge_by_bps)
        check_payload["live_min_net_edge"] = min_live_edge
        check_payload["live_min_net_edge_bps"] = effective_min_edge_bps
        check_payload["horizon_days"] = horizon_days
        check_payload["long_horizon"] = is_long_horizon
        check_payload["category"] = category
        check_payload["near_efficient"] = near_efficient
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
                tick_size=float(getattr(snap, "tick_size", 0.01) or 0.01),
            )
        ],
        max_executable_size=float(fillable_size),
        confidence=float(signal.confidence),
    )
    return opportunity, float(target_size), ""
