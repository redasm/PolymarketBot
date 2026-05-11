"""Per-tier strategy signal collectors.

Each `collect_*_strategy_signals` function takes the relevant scanner /
detector / strategy as an explicit argument and returns a flat list of
`StrategySignal`s for the orchestrator to schedule. They are pure
transforms (`MarketInfo` + scanner state -> signals) and side-effect
free, so the orchestrator can call them in any order without coordinating
shared state.

Why a separate module:
- The original implementations lived inline in `main_loop.py` mixed
  with run-loop wiring, which made it impossible to exercise the
  per-tier output shape without booting the whole bot.
- Splitting them this way also makes it obvious which subsystem each
  tier consumes (T1 = `CrossPlatformScanner`; T2 = `StatisticalMispricingDetector`
  + related-market context; T3 = `MakerStrategy` + fair-value map).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.main_helpers.flow_aggregator import FlowAggregator
from polymarket_arb.main_helpers.market_category import (
    CATEGORY_MAKER_TAKER_GAP_PP,
    classify_market_category,
    is_high_gap,
)
from polymarket_arb.main_helpers.signal_helpers import (
    build_t2_related_market_context,
    evaluate_t2_market_quality,
)
from polymarket_arb.models import MarketInfo
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.strategies.maker_strategy import MakerStrategy
from polymarket_arb.strategies.statistical_model import StatisticalMispricingDetector
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier

LOG = logging.getLogger("main_loop")

# Per-market T2 emission throttle. The orchestrator's rate cap was hitting
# ~342 skips per scan cycle on a 2-market universe because the collector
# re-emitted the same statistical signal every cycle on stable orderbooks.
# We dedupe at the source: same market + same direction + tiny deviation
# delta within the window → skip. Material moves (direction flip or
# |Δdeviation| ≥ STATISTICAL_REEMIT_DEVIATION_DELTA) always re-emit.
_STATISTICAL_LAST_EMIT: dict[str, tuple[float, str, float]] = {}
STATISTICAL_REEMIT_WINDOW_SEC = 60.0
STATISTICAL_REEMIT_DEVIATION_DELTA = 0.005


def _statistical_should_emit(
    market_id: str,
    action: str,
    deviation: float,
    *,
    now: float | None = None,
) -> bool:
    """Throttle re-emission of the same (market, action) on tiny moves.

    Exposed so tests can drive the cache; module-level state is fine here
    because the bot has a single collector instance per run.
    """
    now = now if now is not None else time.time()
    cached = _STATISTICAL_LAST_EMIT.get(market_id)
    if cached is None:
        _STATISTICAL_LAST_EMIT[market_id] = (now, action, float(deviation))
        return True
    cached_ts, cached_action, cached_dev = cached
    age = now - cached_ts
    if cached_action != action:
        _STATISTICAL_LAST_EMIT[market_id] = (now, action, float(deviation))
        return True
    if abs(float(deviation) - cached_dev) >= STATISTICAL_REEMIT_DEVIATION_DELTA:
        _STATISTICAL_LAST_EMIT[market_id] = (now, action, float(deviation))
        return True
    if age >= STATISTICAL_REEMIT_WINDOW_SEC:
        _STATISTICAL_LAST_EMIT[market_id] = (now, action, float(deviation))
        return True
    return False


def reset_statistical_signal_throttle() -> None:
    """Clear the per-market emission cache. Tests use this between cases."""
    _STATISTICAL_LAST_EMIT.clear()


def collect_cross_platform_strategy_signals(
    *,
    config: ArbConfig,
    scanner: Any | None,
) -> list[StrategySignal]:
    """Convert raw cross-platform opportunities (Polymarket↔Kalshi) into T1 signals.

    Returns an empty list when the scanner is disabled (no pairs
    configured), so the orchestrator can call this unconditionally.
    """
    if scanner is None:
        return []

    signals: list[StrategySignal] = []
    for opp in scanner.scan():
        signals.append(
            StrategySignal(
                tier=StrategyTier.CROSS_PLATFORM,
                signal_type=f"cross_platform_{opp.direction}",
                market_id=opp.pair.polymarket_condition_id,
                description=opp.pair.event_description[:120],
                expected_edge=opp.edge_pct * 100.0,
                confidence=opp.confidence,
                recommended_size_usdc=config.default_order_size_usdc,
                urgency=0.9,
                payload={
                    "direction": opp.direction,
                    "pair_id": opp.pair.pair_id,
                    "event_description": opp.pair.event_description,
                    "poly_cost": opp.poly_cost,
                    "kalshi_cost": opp.kalshi_cost,
                    "total_cost": opp.total_cost,
                    "net_edge": opp.net_edge,
                    "edge_pct": opp.edge_pct,
                },
            )
        )
    return signals


def collect_statistical_strategy_signals(
    *,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    detector: StatisticalMispricingDetector,
) -> list[StrategySignal]:
    """T2 directional signals from statistical mispricing detector.

    Quality-gates each market (spread / depth / complement-error) before
    asking the detector for an estimate; markets that fail any gate are
    dropped silently here and surfaced later via `execution_check`
    payloads on signals that *do* survive.
    """
    signals: list[StrategySignal] = []
    related_market_context = build_t2_related_market_context(candidate_markets, ob_analyzer)
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            continue
        horizon_days = _market_horizon_days(market)
        if horizon_days is not None and horizon_days > 90.0:
            continue

        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        no_token = next((t for t in market.tokens if (t.outcome or "").lower() == "no"), market.tokens[-1])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        no_snap = ob_analyzer.get_snapshot(no_token.token_id)
        if snap is None or no_snap is None or snap.mid is None or no_snap.mid is None:
            continue
        quality = evaluate_t2_market_quality(config=config, snap=snap, no_snap=no_snap)
        if quality["passes"] is False:
            continue

        bids_total_size = sum(level.size for level in snap.bids[:5])
        asks_total_size = sum(level.size for level in snap.asks[:5])
        estimate = detector.analyze(
            market_id=market.condition_id,
            outcome="YES",
            market_price=float(snap.mid),
            bids_total_size=bids_total_size,
            asks_total_size=asks_total_size,
            mid_price=float(snap.mid),
            related_market_prices=related_market_context.get(market.condition_id),
        )
        if estimate is None:
            continue

        action = "buy_yes" if estimate.is_underpriced else "buy_no"
        if not _statistical_should_emit(
            market.condition_id, action, float(estimate.deviation)
        ):
            continue
        signals.append(
            StrategySignal(
                tier=StrategyTier.STATISTICAL_ARB,
                signal_type=f"statistical_{action}",
                market_id=market.condition_id,
                description=f"{market.question[:80]} | deviation={estimate.deviation:+.4f}",
                expected_edge=estimate.abs_edge * 10_000.0,
                confidence=estimate.confidence,
                recommended_size_usdc=config.default_order_size_usdc,
                urgency=min(1.0, 0.5 + estimate.confidence * 0.4),
                payload={
                    "outcome": estimate.outcome,
                    "model_prob": estimate.model_prob,
                    "market_prob": estimate.market_prob,
                    "deviation": estimate.deviation,
                    "deviation_pct": estimate.deviation_pct,
                    "signals": dict(estimate.signals),
                    "quality": quality,
                    "related_context_count": len(related_market_context.get(market.condition_id, {})),
                },
            )
        )
    return signals


def _market_horizon_days(market: MarketInfo) -> float | None:
    if not market.end_date:
        return None
    try:
        end_dt = datetime.fromisoformat(str(market.end_date).replace("Z", "+00:00"))
        return (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return None


def collect_maker_strategy_signals(
    *,
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    maker_strategy: MakerStrategy,
    fair_values_by_market: dict[str, float],
    detector: StatisticalMispricingDetector | None = None,
    flow_aggregator: FlowAggregator | None = None,
) -> list[StrategySignal]:
    """T3 maker quote signals around model fair value.

    Falls back to the statistical detector's own probability estimate
    when no pre-computed fair value is supplied for a market — this lets
    the maker tier still post quotes during cycles where the T2 path
    didn't run (e.g. T2 disabled by config).

    When `flow_aggregator` is provided each signal also carries a
    ``flow_bias`` payload (taker_yes_share over the active window).
    For the current "最小落地" phase this is telemetry-only — it lets
    us validate the dataset before letting it drive quote-side
    selection.
    """
    signals: list[StrategySignal] = []
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            continue

        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        if snap is None or snap.mid is None:
            continue
        fair_value = fair_values_by_market.get(market.condition_id)
        if fair_value is None and detector is not None:
            bids_total_size = sum(level.size for level in snap.bids[:5])
            asks_total_size = sum(level.size for level in snap.asks[:5])
            estimate = detector.estimate_market_probability(
                market_id=market.condition_id,
                outcome="YES",
                market_price=float(snap.mid),
                bids_total_size=bids_total_size,
                asks_total_size=asks_total_size,
                mid_price=float(snap.mid),
            )
            fair_value = estimate.model_prob
        if fair_value is None:
            continue

        quote = maker_strategy.compute_quote(
            token_id=yes_token.token_id,
            condition_id=market.condition_id,
            fair_value=float(fair_value),
            tick_size=max(float(getattr(snap, "tick_size", 0.01) or 0.01), 0.01),
            mid_price=float(snap.mid),
        )
        if quote is None or (quote.bid_price is None and quote.ask_price is None):
            continue

        bid_edge = (
            max(0.0, float(fair_value) - float(quote.bid_price))
            if quote.bid_price is not None
            else 0.0
        )
        ask_edge = (
            max(0.0, float(quote.ask_price) - float(fair_value))
            if quote.ask_price is not None
            else 0.0
        )
        active_sides = (1 if quote.bid_price is not None else 0) + (
            1 if quote.ask_price is not None else 0
        )
        per_fill_edge = (bid_edge + ask_edge) / active_sides if active_sides > 0 else 0.0

        # Boost T3 urgency in categories the article identifies as the
        # maker's structural sweet spot. Same quote, but ranked higher
        # in the orchestrator queue against same-tier competition.
        category = classify_market_category(market)
        gap_pp = CATEGORY_MAKER_TAKER_GAP_PP.get(category, 1.5)
        # 0.2 baseline; +0.3 in high-gap categories so a World Events
        # quote with gap=7.32 pp dominates a Finance quote with gap=0.17.
        urgency = 0.2 + (0.3 if is_high_gap(category) else 0.0)

        flow_bias_payload: dict | None = None
        if flow_aggregator is not None:
            bias = flow_aggregator.get_bias(market.condition_id)
            if bias is not None:
                flow_bias_payload = bias.to_dict()

        payload: dict = {
            "quote": {
                "bid_price": quote.bid_price,
                "ask_price": quote.ask_price,
                "bid_size": quote.bid_size,
                "ask_size": quote.ask_size,
                "spread": quote.spread,
                "fair_value": quote.fair_value,
                "bid_edge": bid_edge,
                "ask_edge": ask_edge,
            },
            "category": category,
            "category_maker_taker_gap_pp": gap_pp,
        }
        if flow_bias_payload is not None:
            payload["flow_bias"] = flow_bias_payload

        signals.append(
            StrategySignal(
                tier=StrategyTier.MARKET_MAKING,
                signal_type="maker_quote",
                market_id=market.condition_id,
                description=f"{market.question[:80]} | maker fair={fair_value:.4f} spread={quote.spread:.4f} cat={category}",
                expected_edge=per_fill_edge * 10_000.0,
                confidence=0.5,
                recommended_size_usdc=max(quote.bid_size, quote.ask_size),
                urgency=urgency,
                payload=payload,
            )
        )
    return signals
