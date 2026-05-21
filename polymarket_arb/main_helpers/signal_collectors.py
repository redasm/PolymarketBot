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
import json
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
from polymarket_arb.strategies.event_calendar_model import EventCalendarModel, EventPricingInput
from polymarket_arb.strategies.logical_constraints import LogicalConstraintDetector, RelationRule
from polymarket_arb.strategies.maker_strategy import MakerStrategy
from polymarket_arb.strategies.statistical_model import StatisticalMispricingDetector
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier
from polymarket_arb.strategies.wallet_alpha import WalletAlphaScorer, WalletProfile

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


def reset_signal_collector_skip_summaries() -> None:
    """Reset collector skip telemetry carried between scan cycles."""
    collect_statistical_strategy_signals.last_skip_summary = {"total": 0, "reasons": {}, "top_markets": []}
    collect_maker_strategy_signals.last_skip_summary = {"total": 0, "reasons": {}, "top_markets": []}


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


def collect_logical_constraint_strategy_signals(
    *,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    rules: list[dict[str, Any] | RelationRule] | dict[str, Any] | str | None = None,
    input_metadata: dict[str, Any] | None = None,
) -> list[StrategySignal]:
    """Emit relationship-violation signals from explicit market rules.

    No rules means no signals. This keeps the collector safe to call from the
    main loop while operators build a vetted relation list.
    """
    parsed_rules = _parse_relation_rules(rules)
    if not parsed_rules:
        return []

    markets_by_id = {market.condition_id: market for market in candidate_markets}
    yes_prices: dict[str, float] = {}
    for market in candidate_markets:
        token = _yes_token(market)
        if token is None:
            continue
        snap = ob_analyzer.get_snapshot(token.token_id)
        if snap is not None and snap.mid is not None:
            yes_prices[market.condition_id] = float(snap.mid)
        elif token.price > 0:
            yes_prices[market.condition_id] = float(token.price)

    detector = LogicalConstraintDetector(
        rules=parsed_rules,
        default_size_usdc=config.default_order_size_usdc,
    )
    signals = detector.detect(markets=markets_by_id, yes_prices=yes_prices)
    _attach_input_metadata(signals, "logical_constraints", input_metadata)
    return signals


def collect_event_calendar_strategy_signals(
    *,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
    baselines: dict[str, dict[str, Any]] | str | None = None,
    input_metadata: dict[str, Any] | None = None,
) -> list[StrategySignal]:
    """Emit event-baseline signals from market metadata or explicit baselines."""
    baseline_map = _parse_event_baselines(baselines)
    model = EventCalendarModel(default_size_usdc=config.default_order_size_usdc)
    signals: list[StrategySignal] = []
    for market in candidate_markets:
        token = _yes_token(market)
        if token is None:
            continue
        snap = ob_analyzer.get_snapshot(token.token_id)
        market_price = float(snap.mid) if snap is not None and snap.mid is not None else float(token.price)
        if market_price <= 0:
            continue

        raw_baseline = _event_baseline_for_market(market, baseline_map)
        if raw_baseline is None:
            continue

        item = EventPricingInput(
            market=market,
            market_price=market_price,
            baseline_probability=float(raw_baseline["baseline_probability"]),
            confidence=float(raw_baseline["confidence"]),
            time_to_event_sec=float(raw_baseline["time_to_event_sec"]),
            taker_fee_rate=config.polymarket_taker_fee_rate,
        )
        signal = model.evaluate(item)
        if signal is not None:
            _attach_input_metadata([signal], "event_baselines", input_metadata)
            signals.append(signal)
    return signals


def collect_wallet_alpha_strategy_signals(
    *,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    profiles: dict[str, dict[str, Any] | WalletProfile] | str | None = None,
    observations: list[dict[str, Any]] | str | None = None,
    scorer: WalletAlphaScorer | None = None,
    input_metadata: dict[str, Any] | None = None,
) -> list[StrategySignal]:
    """Convert vetted wallet observations into follow signals.

    This collector is intentionally data-source agnostic. A future activity
    adapter can feed `observations`; the strategy logic here only accepts
    wallets whose lagged-follow profile has already proven repeatable.
    """
    parsed_profiles = _parse_wallet_profiles(profiles)
    parsed_observations = _parse_wallet_observations(observations)
    if not parsed_observations:
        return []
    candidate_shadow_enabled = bool(
        config.dry_run and getattr(config, "wallet_alpha_candidate_shadow_enabled", False)
    )
    if not parsed_profiles and not candidate_shadow_enabled:
        return []

    markets = {market.condition_id: market for market in candidate_markets}
    alpha_scorer = scorer or WalletAlphaScorer()
    signals: list[StrategySignal] = []
    for obs in parsed_observations:
        wallet = str(obs.get("wallet_address", ""))
        market_id = str(obs.get("market_id", ""))
        action = str(obs.get("action", "BUY_YES")).upper()
        profile = parsed_profiles.get(wallet)
        market = markets.get(market_id)
        if market is None:
            continue
        category = str(obs.get("category", "") or "")
        if profile is None:
            if candidate_shadow_enabled:
                candidate_signal = _wallet_alpha_candidate_signal(config, market, obs, action, wallet, category)
                if candidate_signal is not None:
                    _attach_input_metadata(
                        [candidate_signal],
                        "wallet_alpha",
                        input_metadata,
                    )
                    signals.append(candidate_signal)
            continue
        decision = alpha_scorer.evaluate(profile, category=category or None)
        if not decision.accepted:
            continue
        if action not in {"BUY_YES", "BUY_NO"}:
            continue

        signal = StrategySignal(
            tier=StrategyTier.STATISTICAL_ARB,
            signal_type=f"wallet_alpha_{action.lower()}",
            market_id=market.condition_id,
            description=f"wallet alpha follow {wallet[:10]} on {market.question[:80]}",
            expected_edge=max(0.0, profile.lagged_follow_roi) * 10_000.0,
            confidence=decision.confidence,
            recommended_size_usdc=config.default_order_size_usdc * decision.size_multiplier,
            urgency=0.65,
            payload={
                "action": action,
                "wallet_address": wallet,
                "category": category,
                "observed_size_usdc": float(obs.get("observed_size_usdc", 0.0) or 0.0),
                "lagged_follow_roi": profile.lagged_follow_roi,
                "wallet_size_multiplier": decision.size_multiplier,
                "wallet_reasons": list(decision.reasons),
            },
        )
        _attach_input_metadata([signal], "wallet_alpha", input_metadata)
        signals.append(signal)
    return signals


def _attach_input_metadata(
    signals: list[StrategySignal],
    key: str,
    metadata: dict[str, Any] | None,
) -> None:
    if not metadata:
        return
    for signal in signals:
        signal.payload["quant_input"] = {
            "name": key,
            **dict(metadata),
        }


def _wallet_alpha_candidate_signal(
    config: ArbConfig,
    market: MarketInfo,
    obs: dict[str, Any],
    action: str,
    wallet: str,
    category: str,
) -> StrategySignal | None:
    if action not in {"BUY_YES", "BUY_NO"}:
        return None
    return StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type=f"wallet_alpha_candidate_{action.lower()}",
        market_id=market.condition_id,
        description=f"wallet alpha candidate shadow {wallet[:10]} on {market.question[:80]}",
        expected_edge=300.0,
        confidence=(config.sniper_min_confidence if config.sniper_gate_enabled else 0.10),
        recommended_size_usdc=config.default_order_size_usdc,
        urgency=0.35,
        payload={
            "action": action,
            "deviation": 0.03,
            "wallet_address": wallet,
            "category": category,
            "observed_size_usdc": float(obs.get("observed_size_usdc", 0.0) or 0.0),
            "wallet_profile_status": "candidate_unvalidated",
        },
    )


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
    skip_reasons: dict[str, int] = {}
    skip_by_market: dict[str, dict[str, Any]] = {}
    related_market_context = build_t2_related_market_context(candidate_markets, ob_analyzer)
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "non_binary_or_inactive")
            continue
        horizon_days = _market_horizon_days(market)
        if horizon_days is not None and horizon_days > 90.0:
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "horizon_gt_90d", horizon_days=horizon_days)
            continue

        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        no_token = next((t for t in market.tokens if (t.outcome or "").lower() == "no"), market.tokens[-1])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        no_snap = ob_analyzer.get_snapshot(no_token.token_id)
        if snap is None or no_snap is None or snap.mid is None or no_snap.mid is None:
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "missing_t2_snapshot")
            continue
        quality = evaluate_t2_market_quality(config=config, snap=snap, no_snap=no_snap)
        if quality["passes"] is False:
            for reason in quality.get("reasons", []) or ["quality_gate_failed"]:
                _record_skip(skip_reasons, skip_by_market, market.condition_id, f"quality_{reason}", quality=quality)
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
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "model_no_estimate")
            continue

        action = "buy_yes" if estimate.is_underpriced else "buy_no"
        if not _statistical_should_emit(
            market.condition_id, action, float(estimate.deviation)
        ):
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "collector_reemit_throttle")
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
    collect_statistical_strategy_signals.last_skip_summary = {
        "total": sum(skip_reasons.values()),
        "reasons": dict(skip_reasons),
        "top_markets": sorted(
            skip_by_market.values(),
            key=lambda item: int(item.get("count", 0)),
            reverse=True,
        )[:10],
    }
    return signals


def _yes_token(market: MarketInfo):
    if not market.tokens:
        return None
    return next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])


def _parse_relation_rules(raw_rules: list[dict[str, Any] | RelationRule] | dict[str, Any] | str | None) -> list[RelationRule]:
    if not raw_rules:
        return []
    rows: Any = raw_rules
    if isinstance(raw_rules, str):
        try:
            rows = json.loads(raw_rules)
        except json.JSONDecodeError:
            LOG.warning("LOGICAL_CONSTRAINTS_JSON 解析失败，忽略逻辑约束策略")
            return []
    if isinstance(rows, dict):
        expires_at = _first_float(rows, ("expires_at",))
        if expires_at is not None and expires_at < time.time():
            return []
        rows = rows.get("rules", [])
    parsed: list[RelationRule] = []
    for row in rows or []:
        if isinstance(row, RelationRule):
            parsed.append(row)
            continue
        if not isinstance(row, dict):
            continue
        try:
            parsed.append(
                RelationRule(
                    subject_market_id=str(row["subject_market_id"]),
                    bound_market_id=str(row["bound_market_id"]),
                    relation_type=str(row.get("relation_type", "subject_lte_bound")),
                    min_violation_bps=float(row.get("min_violation_bps", 200.0)),
                    max_size_usdc=(
                        float(row["max_size_usdc"])
                        if row.get("max_size_usdc") not in (None, "")
                        else None
                    ),
                    tags=tuple(str(tag) for tag in row.get("tags", []) or []),
                )
            )
        except (KeyError, TypeError, ValueError):
            LOG.debug("跳过无效逻辑约束规则: %s", row)
    return parsed


def _parse_event_baselines(raw: dict[str, dict[str, Any]] | str | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOG.warning("EVENT_BASELINES_JSON 解析失败，忽略事件基线策略")
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def _event_baseline_for_market(
    market: MarketInfo,
    baseline_map: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    raw = baseline_map.get(market.condition_id) or baseline_map.get(market.slug)
    if raw is None:
        return None
    baseline = _first_float(raw, ("event_baseline_probability", "baseline_probability"))
    confidence = _first_float(raw, ("event_confidence", "confidence"))
    time_to_event = _time_to_event_sec(raw, now_ts=time.time())
    if baseline is None or confidence is None or time_to_event is None:
        return None
    return {
        "baseline_probability": baseline,
        "confidence": confidence,
        "time_to_event_sec": time_to_event,
    }


def _first_float(raw: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in raw or raw[key] in (None, ""):
            continue
        try:
            return float(raw[key])
        except (TypeError, ValueError):
            continue
    return None


def _time_to_event_sec(raw: dict[str, Any], *, now_ts: float) -> float | None:
    absolute = _first_timestamp(
        raw,
        (
            "resolution_ts",
            "resolution_time_ts",
            "resolution_at_ts",
            "event_ts",
            "event_time_ts",
            "deadline_ts",
        ),
    )
    if absolute is None:
        absolute = _first_iso_timestamp(
            raw,
            (
                "resolution_at",
                "resolution_time",
                "event_time",
                "event_date",
                "deadline",
                "end_date",
            ),
        )
    if absolute is not None:
        remaining = absolute - now_ts
        return remaining if remaining >= 0 else None
    static_remaining = _first_float(raw, ("time_to_event_sec", "seconds_to_event"))
    if static_remaining is None:
        return None
    generated_at = _first_timestamp(raw, ("generated_at", "source_generated_at", "created_at_ts"))
    if generated_at is None:
        generated_at = _first_iso_timestamp(raw, ("generated_at", "source_generated_at", "created_at"))
    if generated_at is None:
        return None
    remaining = static_remaining - max(0.0, now_ts - generated_at)
    return remaining if remaining >= 0 else None


def _first_timestamp(raw: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _first_float(raw, (key,))
        if value is not None:
            return value
    return None


def _first_iso_timestamp(raw: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _parse_wallet_profiles(raw: dict[str, dict[str, Any] | WalletProfile] | str | None) -> dict[str, WalletProfile]:
    if not raw:
        return {}
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            LOG.warning("WALLET_ALPHA_PROFILES_JSON 解析失败，忽略钱包 alpha 策略")
            return {}
    if not isinstance(payload, dict):
        return {}
    if "schema_version" in payload and "wallets" in payload:
        expires_at = _first_float(payload, ("expires_at",))
        if expires_at is not None and expires_at < time.time():
            return {}
        wallets_payload = payload.get("wallets")
        if not isinstance(wallets_payload, dict):
            return {}
        payload = wallets_payload

    parsed: dict[str, WalletProfile] = {}
    for wallet, row in payload.items():
        if isinstance(row, WalletProfile):
            parsed[str(wallet)] = row
            continue
        if not isinstance(row, dict):
            continue
        try:
            parsed[str(wallet)] = WalletProfile(
                wallet_address=str(row.get("wallet_address") or wallet),
                trade_count=int(row.get("trade_count", 0)),
                realized_roi=float(row.get("realized_roi", 0.0)),
                lagged_follow_roi=float(row.get("lagged_follow_roi", 0.0)),
                max_drawdown=float(row.get("max_drawdown", 1.0)),
                concentration_score=float(row.get("concentration_score", 1.0)),
                category_edges={
                    str(key): float(value)
                    for key, value in dict(row.get("category_edges", {}) or {}).items()
                },
            )
        except (TypeError, ValueError):
            LOG.debug("跳过无效钱包画像: %s", row)
    return parsed


def _parse_wallet_observations(raw: list[dict[str, Any]] | str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOG.warning("WALLET_ALPHA_OBSERVATIONS_JSON 解析失败，忽略钱包观察")
        return []
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, dict)]


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
    event_baselines: dict[str, dict[str, Any]] | str | None = None,
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
    skip_reasons: dict[str, int] = {}
    skip_by_market: dict[str, dict[str, Any]] = {}
    event_baseline_map = _parse_event_baselines(event_baselines)
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "non_binary_or_inactive")
            continue

        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        if snap is None or snap.mid is None:
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "missing_maker_snapshot")
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
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "missing_fair_value")
            continue

        # Pull the per-market flow snapshot once and reuse it for both
        # quote steering and payload telemetry. We only feed
        # `flow_bias_yes_share` into `compute_quote` when the sample is
        # *stable* — letting an under-sampled window steer the quote
        # would just amplify noise (and contradict the `is_stable`
        # gating semantics defined on FlowBias).
        flow_snapshot = (
            flow_aggregator.get_bias(market.condition_id)
            if flow_aggregator is not None
            else None
        )
        flow_share: float | None = (
            float(flow_snapshot.taker_yes_share)
            if flow_snapshot is not None and flow_snapshot.is_stable
            else None
        )
        event_toxicity = _maker_event_time_toxicity(market, event_baseline_map)
        quote = maker_strategy.compute_quote(
            token_id=yes_token.token_id,
            condition_id=market.condition_id,
            fair_value=float(fair_value),
            tick_size=max(float(getattr(snap, "tick_size", 0.01) or 0.01), 0.01),
            mid_price=float(snap.mid),
            flow_bias_yes_share=flow_share,
            spread_multiplier=float(event_toxicity["spread_multiplier"]),
        )
        if quote is None or (quote.bid_price is None and quote.ask_price is None):
            _record_skip(skip_reasons, skip_by_market, market.condition_id, "maker_no_quote")
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

        flow_bias_payload: dict | None = (
            flow_snapshot.to_dict() if flow_snapshot is not None else None
        )
        if flow_bias_payload is not None:
            # Annotate whether the snapshot actually influenced the
            # quote this cycle. Useful for shadow-mode A/B reads.
            flow_bias_payload["applied_to_quote"] = flow_share is not None

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
            "queue_position": _maker_queue_position_payload(snap, quote),
        }
        if event_toxicity["applied"]:
            payload["event_time_toxicity"] = event_toxicity
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
                recommended_size_usdc=max(quote.bid_size, quote.ask_size) * float(event_toxicity["size_multiplier"]),
                urgency=urgency,
                payload=payload,
            )
        )
    collect_maker_strategy_signals.last_skip_summary = {
        "total": sum(skip_reasons.values()),
        "reasons": dict(skip_reasons),
        "top_markets": sorted(
            skip_by_market.values(),
            key=lambda item: int(item.get("count", 0)),
            reverse=True,
        )[:10],
    }
    return signals


def _maker_queue_position_payload(snap: OrderBookSnapshot, quote: Any) -> dict[str, Any]:
    bid_price = getattr(quote, "bid_price", None)
    ask_price = getattr(quote, "ask_price", None)
    return {
        "telemetry_only": True,
        "bid_price": bid_price,
        "ask_price": ask_price,
        "bid_ahead_size": round(_depth_at_price(getattr(snap, "bids", []), bid_price), 8),
        "ask_ahead_size": round(_depth_at_price(getattr(snap, "asks", []), ask_price), 8),
    }


def _depth_at_price(levels: list[Any], price: Any) -> float:
    if price is None:
        return 0.0
    target = float(price)
    return sum(
        float(getattr(level, "size", 0.0) or 0.0)
        for level in levels
        if abs(float(getattr(level, "price", 0.0) or 0.0) - target) <= 1e-9
    )


def _maker_event_time_toxicity(
    market: MarketInfo,
    baseline_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    raw = baseline_map.get(market.condition_id) or baseline_map.get(market.slug)
    remaining = _time_to_event_sec(raw, now_ts=time.time()) if raw else None
    if remaining is None:
        return {"applied": False, "spread_multiplier": 1.0, "size_multiplier": 1.0}
    if remaining <= 5 * 60:
        return {
            "applied": True,
            "time_to_event_sec": round(float(remaining), 3),
            "spread_multiplier": 2.0,
            "size_multiplier": 0.50,
            "reason": "event_within_5m",
        }
    if remaining <= 30 * 60:
        return {
            "applied": True,
            "time_to_event_sec": round(float(remaining), 3),
            "spread_multiplier": 1.5,
            "size_multiplier": 0.75,
            "reason": "event_within_30m",
        }
    return {
        "applied": False,
        "time_to_event_sec": round(float(remaining), 3),
        "spread_multiplier": 1.0,
        "size_multiplier": 1.0,
    }


reset_signal_collector_skip_summaries()


def _record_skip(
    reasons: dict[str, int],
    by_market: dict[str, dict[str, Any]],
    market_id: str,
    reason: str,
    **context: Any,
) -> None:
    reasons[reason] = reasons.get(reason, 0) + 1
    if not market_id:
        return
    item = by_market.setdefault(market_id, {"market_id": market_id, "count": 0, "reasons": {}})
    item["count"] = int(item.get("count", 0)) + 1
    item_reasons = item.setdefault("reasons", {})
    item_reasons[reason] = int(item_reasons.get(reason, 0)) + 1
    if context and "sample_context" not in item:
        item["sample_context"] = context
