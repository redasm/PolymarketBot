"""Pure helpers used by the strategy-signal collection / execution path.

These functions used to live inline in `main_loop.py`. Each one is a
small, side-effect-light transformation: parsing a question for a stem
or deadline, deriving spread metrics from a snapshot, scoring whether a
T2 candidate passes its quality gates, etc.

They form a stable contract between the scan loop and the orchestrator:
isolating them here lets us test the contract without spinning up the
loop, and makes it obvious which behaviours the orchestrator depends on.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import MarketInfo, TradeRecord
from polymarket_arb.orderbook_analyzer import OrderBookAnalyzer
from polymarket_arb.strategies.maker_strategy import MakerStrategy
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal

LOG = logging.getLogger("main_loop")

_MONTHS: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_DEADLINE_RE = re.compile(
    r"\b(?:by|before)\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)


def extract_market_temporal_stem(question: str) -> str:
    """Strip leading `Will`, trailing `?`, and trailing `by/before <date>`.

    Used to group "Will BTC > $100k by Jan 1, 2026" with "Will BTC > $100k
    by Mar 1, 2026" into the same temporal-ladder bucket.
    """
    normalized = re.sub(r"\s+", " ", (question or "")).strip().rstrip("?").strip().lower()
    normalized = re.sub(r"\bwill\s+", "", normalized)
    normalized = re.sub(r"\s+(?:by|before)\s+.+$", "", normalized)
    return normalized.strip(" .!?")


def extract_market_deadline(question: str) -> datetime | None:
    """Parse `... by/before <Month> <D>, <YYYY>` into a `datetime` or None."""
    match = _DEADLINE_RE.search(question or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    day = int(match.group(2))
    year = int(match.group(3))
    if month is None:
        return None
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def spread_bps_from_snapshot(snap: Any) -> float | None:
    """Compute spread/mid in bps with explicit None on missing data."""
    if snap is None or getattr(snap, "best_bid", None) is None or getattr(snap, "best_ask", None) is None:
        return None
    mid = getattr(snap, "mid", None)
    spread = getattr(snap, "spread", None)
    if spread is None and getattr(snap, "best_bid", None) is not None and getattr(snap, "best_ask", None) is not None:
        spread = float(snap.best_ask) - float(snap.best_bid)
    if mid is None or spread is None or float(mid) <= 0:
        return None
    return (float(spread) / float(mid)) * 10_000.0


def evaluate_t2_market_quality(
    *,
    config: ArbConfig,
    snap: Any,
    no_snap: Any,
) -> dict[str, Any]:
    """Apply T2 quality gates and return a structured pass/fail report.

    A T2 directional bet is only sized when:
    - both YES and NO sides quote a tight enough spread,
    - both top-of-book asks have meaningful depth, and
    - the YES + NO mid sums to ~1.0 (no obvious dislocation).

    The dict's `reasons` list is what the orchestrator surfaces in
    telemetry so the operator can see which gate vetoed each scan.
    """
    yes_spread_bps = spread_bps_from_snapshot(snap)
    no_spread_bps = spread_bps_from_snapshot(no_snap)
    complement_error_bps = None
    if snap.mid is not None and no_snap.mid is not None:
        complement_error_bps = abs(1.0 - (float(snap.mid) + float(no_snap.mid))) * 10_000.0
    yes_top_depth = float(getattr(snap, "best_ask_size", 0.0) or 0.0)
    no_top_depth = float(getattr(no_snap, "best_ask_size", 0.0) or 0.0)

    reasons: list[str] = []
    if yes_spread_bps is None or no_spread_bps is None:
        reasons.append("missing_spread")
    elif max(float(yes_spread_bps), float(no_spread_bps)) > config.t2_max_spread_bps:
        reasons.append("spread_too_wide")
    if min(yes_top_depth, no_top_depth) < config.t2_min_top_depth:
        reasons.append("top_depth_too_low")
    if complement_error_bps is None:
        reasons.append("missing_complement_error")
    elif float(complement_error_bps) > config.t2_max_complement_error_bps:
        reasons.append("complement_error_too_high")

    return {
        "passes": not reasons,
        "reasons": reasons,
        "yes_spread_bps": round(float(yes_spread_bps), 2) if yes_spread_bps is not None else None,
        "no_spread_bps": round(float(no_spread_bps), 2) if no_spread_bps is not None else None,
        "yes_top_depth": round(yes_top_depth, 4),
        "no_top_depth": round(no_top_depth, 4),
        "complement_error_bps": round(float(complement_error_bps), 2) if complement_error_bps is not None else None,
    }


def build_t2_related_market_context(
    candidate_markets: list[MarketInfo],
    ob_analyzer: OrderBookAnalyzer,
) -> dict[str, dict[str, Any]]:
    """Build per-market related-market context for T2 fair-value modelling.

    Groups markets that share an event/stem and orders them by deadline,
    then for each market emits its immediate temporal neighbours with a
    `lower_bound` / `upper_bound` relation hint. The fair-value model
    uses these neighbour prices as soft constraints: a "by Jan 1" market
    can't price below a "by Mar 1" of the same outcome and vice versa.
    """
    yes_mid_by_market: dict[str, float] = {}
    for market in candidate_markets:
        if len(market.tokens) != 2 or market.closed or not market.active:
            continue
        yes_token = next((t for t in market.tokens if (t.outcome or "").lower() == "yes"), market.tokens[0])
        snap = ob_analyzer.get_snapshot(yes_token.token_id)
        if snap is None or snap.mid is None:
            continue
        yes_mid_by_market[market.condition_id] = float(snap.mid)

    contexts: dict[str, dict[str, Any]] = {cid: {} for cid in yes_mid_by_market}
    ladder_groups: dict[tuple[str, str], list[tuple[datetime, MarketInfo]]] = {}

    for market in candidate_markets:
        if market.condition_id not in yes_mid_by_market:
            continue
        stem = extract_market_temporal_stem(market.question)
        deadline = extract_market_deadline(market.question)
        if not stem or deadline is None:
            continue
        group_key = (market.event_id or market.event_slug or stem, stem)
        ladder_groups.setdefault(group_key, []).append((deadline, market))

    for ladder in ladder_groups.values():
        ladder.sort(key=lambda item: item[0])
        for idx, (_, market) in enumerate(ladder):
            current = contexts.setdefault(market.condition_id, {})
            if idx > 0:
                prev_market = ladder[idx - 1][1]
                prev_price = yes_mid_by_market.get(prev_market.condition_id)
                if prev_price is not None:
                    current[prev_market.condition_id] = {
                        "price": prev_price,
                        "relation": "lower_bound",
                        "weight": 1.35,
                    }
            if idx + 1 < len(ladder):
                next_market = ladder[idx + 1][1]
                next_price = yes_mid_by_market.get(next_market.condition_id)
                if next_price is not None:
                    current[next_market.condition_id] = {
                        "price": next_price,
                        "relation": "upper_bound",
                        "weight": 1.35,
                    }

    return {cid: ctx for cid, ctx in contexts.items() if ctx}


def resolve_strategy_signal_action(signal: StrategySignal) -> str:
    """Return the canonical action name (`BUY_YES` / `BUY_NO` / …) or `""`.

    The action can be encoded either explicitly in `payload["action"]` or
    implicitly via the `signal_type` string. Explicit always wins.
    """
    payload_action = str(signal.payload.get("action", "")).upper()
    if payload_action:
        return payload_action
    signal_type = signal.signal_type.upper()
    if "BUY_YES" in signal_type:
        return "BUY_YES"
    if "BUY_NO" in signal_type:
        return "BUY_NO"
    if "SELL_YES" in signal_type:
        return "SELL_YES"
    if "SELL_NO" in signal_type:
        return "SELL_NO"
    if signal_type.endswith("BUY_YES"):
        return "BUY_YES"
    if signal_type.endswith("BUY_NO"):
        return "BUY_NO"
    return ""


def find_market_for_signal(signal_market_id: str, markets: list[MarketInfo]) -> MarketInfo | None:
    """Locate the `MarketInfo` for a signal, accepting truncated condition_ids.

    Some upstream sources (logs, dashboard rows) carry only a
    truncated `condition_id`. The prefix fallback lets the executor
    reconcile signals against the live universe without strict equality.
    """
    for market in markets:
        if market.condition_id == signal_market_id:
            return market
        if (
            signal_market_id
            and len(signal_market_id) >= 8
            and (market.condition_id.startswith(signal_market_id) or signal_market_id.startswith(market.condition_id))
        ):
            return market
    return None


def set_signal_execution_check(signal: StrategySignal, *, reason: str, **fields: Any) -> None:
    """Stamp an `execution_check` payload on a signal for telemetry / dashboard.

    Centralised so every veto path uses the same payload shape: the
    dashboard's "skipped signals" panel relies on `reason` + `checked_at`
    being present on every emitted decision.
    """
    payload = {
        "reason": reason,
        "checked_at": time.time(),
    }
    payload.update(fields)
    signal.payload["execution_check"] = payload


def sum_trade_exposure(trades: list[Any], *, include_simulated: bool = True) -> float:
    """Sum economic cost × fill size across trades.

    Falls back to `price` when `economic_cost` is missing (older trade
    records) and to `size` when `fill_size` is missing (PENDING legs).
    Pass `include_simulated=False` to compute realised live exposure.
    """
    total = 0.0
    for trade in trades:
        if not include_simulated and bool(getattr(trade, "simulated", False)):
            continue
        leg_cost = getattr(trade, "economic_cost", None)
        if leg_cost is None:
            leg_cost = getattr(trade, "price", 0.0)
        fill_size = getattr(trade, "fill_size", None)
        size = float(fill_size if fill_size is not None else getattr(trade, "size", 0.0) or 0.0)
        total += float(leg_cost or 0.0) * size
    return total


def apply_maker_fill_to_inventory(maker_strategy: MakerStrategy, trade: TradeRecord) -> float:
    """Apply newly observed maker fill delta to local inventory.

    Idempotent: tracks `inventory_accounted_size` on the trade so a
    re-run of the maker reconciliation loop won't double-count an
    already-applied fill. Returns the delta size that was applied (zero
    when the trade is not a maker fill or has already been fully
    accounted for).
    """
    if not bool(getattr(trade, "post_only", False)):
        return 0.0
    filled = float(trade.fill_size or 0.0)
    accounted = float(getattr(trade, "inventory_accounted_size", 0.0) or 0.0)
    delta = max(0.0, filled - accounted)
    if delta <= 0:
        return 0.0
    maker_strategy.update_inventory(trade.token_id, trade.side.value, delta)
    trade.inventory_accounted_size = accounted + delta
    return delta
