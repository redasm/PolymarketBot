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
from typing import Any

from polymarket_arb.config import ArbConfig
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


def collect_maker_strategy_signals(
    *,
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    maker_strategy: MakerStrategy,
    fair_values_by_market: dict[str, float],
    detector: StatisticalMispricingDetector | None = None,
) -> list[StrategySignal]:
    """T3 maker quote signals around model fair value.

    Falls back to the statistical detector's own probability estimate
    when no pre-computed fair value is supplied for a market — this lets
    the maker tier still post quotes during cycles where the T2 path
    didn't run (e.g. T2 disabled by config).
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

        signals.append(
            StrategySignal(
                tier=StrategyTier.MARKET_MAKING,
                signal_type="maker_quote",
                market_id=market.condition_id,
                description=f"{market.question[:80]} | maker fair={fair_value:.4f} spread={quote.spread:.4f}",
                expected_edge=per_fill_edge * 10_000.0,
                confidence=0.5,
                recommended_size_usdc=max(quote.bid_size, quote.ask_size),
                urgency=0.2,
                payload={
                    "quote": {
                        "bid_price": quote.bid_price,
                        "ask_price": quote.ask_price,
                        "bid_size": quote.bid_size,
                        "ask_size": quote.ask_size,
                        "spread": quote.spread,
                        "fair_value": quote.fair_value,
                        "bid_edge": bid_edge,
                        "ask_edge": ask_edge,
                    }
                },
            )
        )
    return signals
