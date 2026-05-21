"""Read-only scanners for opt-in quant strategy inputs."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

import requests

from polymarket_arb.models import EventInfo, MarketInfo
from polymarket_arb.strategies.wallet_alpha import WalletAlphaScorer, WalletProfile


def generate_logical_constraint_candidates(
    events: list[EventInfo],
    *,
    min_liquidity: float = 0.0,
    min_volume_24h: float = 0.0,
    max_pairs_per_event: int = 80,
) -> list[dict[str, Any]]:
    """Generate same-event binary-market pairs for human/LLM review."""
    candidates: list[dict[str, Any]] = []
    for event in events:
        markets = [
            market for market in event.markets
            if _is_binary_market(market)
            and market.active
            and not market.closed
            and market.liquidity >= min_liquidity
            and market.volume_24h >= min_volume_24h
        ]
        emitted = 0
        for subject in markets:
            for bound in markets:
                if subject.condition_id == bound.condition_id:
                    continue
                candidates.append(
                    {
                        "event_id": event.event_id,
                        "event_title": event.title,
                        "subject_market_id": subject.condition_id,
                        "subject_question": subject.question,
                        "bound_market_id": bound.condition_id,
                        "bound_question": bound.question,
                        "subject_liquidity": subject.liquidity,
                        "bound_liquidity": bound.liquidity,
                        "selector": "same_event_binary_pair",
                    }
                )
                emitted += 1
                if emitted >= max_pairs_per_event:
                    break
            if emitted >= max_pairs_per_event:
                break
    return candidates


def select_logical_constraints_with_llm(
    provider: Any,
    candidates: list[dict[str, Any]],
    *,
    min_violation_bps: float = 250.0,
    max_candidates: int = 40,
) -> list[dict[str, Any]]:
    """Use an LLM provider to keep only deterministic implication relations."""
    if not candidates:
        return []
    return asyncio.run(
        _select_logical_constraints_with_llm_async(
            provider,
            candidates[:max_candidates],
            min_violation_bps=min_violation_bps,
        )
    )


async def _select_logical_constraints_with_llm_async(
    provider: Any,
    candidates: list[dict[str, Any]],
    *,
    min_violation_bps: float,
) -> list[dict[str, Any]]:
    messages = [
        {
            "role": "system",
            "content": (
                "Return JSON only. Select only deterministic probability constraints. "
                "Use relation_type=subject_lte_bound when P(subject) must be <= P(bound). "
                "Reject thematic correlation, loose causality, and speculative relationships."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"candidates": candidates}, ensure_ascii=False),
        },
    ]
    resp = await provider.chat(messages, temperature=0.0, json_mode=True)
    try:
        payload = json.loads(resp.content)
    except json.JSONDecodeError:
        return []
    rows = payload.get("rules", []) if isinstance(payload, dict) else []
    rules: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        subject = str(row.get("subject_market_id") or "").strip()
        bound = str(row.get("bound_market_id") or "").strip()
        if not subject or not bound:
            continue
        relation_type = str(row.get("relation_type") or "subject_lte_bound")
        if relation_type != "subject_lte_bound":
            continue
        rules.append(
            {
                "subject_market_id": subject,
                "bound_market_id": bound,
                "relation_type": relation_type,
                "min_violation_bps": float(row.get("min_violation_bps") or min_violation_bps),
                "tags": ["llm_selected"],
            }
        )
    return rules


class DataApiWalletTradeClient:
    """Small read-only client for Polymarket Data API wallet trades."""

    def __init__(self, data_api_host: str, *, session: Any | None = None, timeout_sec: float = 15.0) -> None:
        self._host = data_api_host.rstrip("/")
        self._session = session or requests.Session()
        self._timeout_sec = float(timeout_sec)

    def fetch_trades(self, wallet_address: str, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        resp = self._session.get(
            f"{self._host}/trades",
            params={
                "user": wallet_address,
                "limit": int(limit),
                "offset": int(offset),
            },
            timeout=self._timeout_sec,
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, list):
            return [dict(row) for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            rows = payload.get("data") or payload.get("trades") or payload.get("results") or []
            return [dict(row) for row in rows if isinstance(row, dict)]
        return []

    def fetch_recent_trades(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        resp = self._session.get(
            f"{self._host}/trades",
            params={
                "limit": int(limit),
                "offset": int(offset),
            },
            timeout=self._timeout_sec,
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, list):
            return [dict(row) for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            rows = payload.get("data") or payload.get("trades") or payload.get("results") or []
            return [dict(row) for row in rows if isinstance(row, dict)]
        return []


def discover_wallets_from_trades(
    rows: list[dict[str, Any]],
    *,
    min_trades: int = 3,
    min_notional_usdc: float = 100.0,
    max_wallets: int = 50,
) -> list[str]:
    stats: dict[str, dict[str, float]] = defaultdict(lambda: {"trades": 0.0, "notional": 0.0})
    for row in rows:
        wallet = str(row.get("proxyWallet") or row.get("wallet_address") or row.get("user") or "").strip()
        if not wallet:
            continue
        stats[wallet]["trades"] += 1.0
        stats[wallet]["notional"] += _float(row.get("price")) * _float(row.get("size"))
    ranked = [
        (wallet, values["trades"], values["notional"])
        for wallet, values in stats.items()
        if values["trades"] >= min_trades and values["notional"] >= min_notional_usdc
    ]
    ranked.sort(key=lambda item: (item[1], item[2]), reverse=True)
    return [wallet for wallet, _, _ in ranked[:max_wallets]]


def build_wallet_observations_from_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for row in rows:
        wallet = str(row.get("proxyWallet") or row.get("wallet_address") or row.get("user") or "").strip()
        market_id = str(row.get("conditionId") or row.get("condition_id") or row.get("market_id") or "").strip()
        outcome = str(row.get("outcome") or row.get("assetOutcome") or "").strip().lower()
        side = str(row.get("side") or row.get("type") or "BUY").strip().upper()
        if not wallet or not market_id:
            continue
        if side not in {"BUY", "SELL"}:
            continue
        if outcome in {"no", "false"}:
            action = "BUY_NO" if side == "BUY" else "BUY_YES"
        else:
            action = "BUY_YES" if side == "BUY" else "BUY_NO"
        observations.append(
            {
                "wallet_address": wallet,
                "market_id": market_id,
                "category": str(row.get("marketSlug") or row.get("category") or "").strip(),
                "action": action,
                "observed_size_usdc": round(_float(row.get("price")) * _float(row.get("size")), 8),
            }
        )
    return observations


def build_wallet_profiles_from_markout_rows(
    rows: list[dict[str, Any]],
    *,
    min_trades: int = 30,
) -> dict[str, dict[str, Any]]:
    """Build wallet profiles from offline rows that already contain markout PnL."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        wallet = str(row.get("wallet_address") or row.get("proxyWallet") or "").strip()
        if wallet:
            buckets[wallet].append(row)

    profiles: dict[str, dict[str, Any]] = {}
    for wallet, wallet_rows in buckets.items():
        if len(wallet_rows) < min_trades:
            continue
        total_notional = sum(_float(row.get("notional_usdc") or row.get("notional")) for row in wallet_rows)
        if total_notional <= 0:
            continue
        realized_pnl = sum(_float(row.get("realized_pnl_usdc") or row.get("realized_pnl")) for row in wallet_rows)
        lagged_pnl = sum(_float(row.get("lagged_follow_pnl_usdc") or row.get("lagged_follow_pnl")) for row in wallet_rows)
        category_edges = _category_edges(wallet_rows)
        notionals = [_float(row.get("notional_usdc") or row.get("notional")) for row in wallet_rows]
        profiles[wallet] = {
            "wallet_address": wallet,
            "trade_count": len(wallet_rows),
            "realized_roi": round(realized_pnl / total_notional, 8),
            "lagged_follow_roi": round(lagged_pnl / total_notional, 8),
            "max_drawdown": round(_max_drawdown(wallet_rows), 8),
            "concentration_score": round((max(notionals) / total_notional) if notionals else 1.0, 8),
            "category_edges": category_edges,
        }
    return profiles


def promote_wallet_profiles_from_markout_rows(
    rows: list[dict[str, Any]],
    *,
    min_trades: int = 30,
    min_lagged_roi: float = 0.04,
    max_concentration: float = 0.35,
    max_drawdown: float = 0.35,
) -> dict[str, dict[str, Any]]:
    """Return only wallet profiles that pass the live wallet-alpha gate."""
    profiles = build_wallet_profiles_from_markout_rows(rows, min_trades=min_trades)
    scorer = WalletAlphaScorer(
        min_trades=min_trades,
        min_lagged_roi=min_lagged_roi,
        max_concentration=max_concentration,
        max_drawdown=max_drawdown,
    )
    promoted: dict[str, dict[str, Any]] = {}
    for wallet, row in profiles.items():
        profile = WalletProfile(
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
        decision = scorer.evaluate(profile)
        if decision.accepted:
            promoted[wallet] = {
                **row,
                "promotion_reasons": list(decision.reasons),
                "promotion_confidence": decision.confidence,
            }
    return promoted


def build_wallet_markouts_from_shadow_rows(
    *,
    virtual_fill_rows: list[dict[str, Any]],
    lifecycle_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entry_by_trade_id: dict[str, dict[str, Any]] = {}
    for row in virtual_fill_rows:
        context = row.get("decision_context") if isinstance(row.get("decision_context"), dict) else {}
        wallet = str(context.get("wallet_address") or "").strip()
        signal_type = str(context.get("signal_type") or "")
        if not wallet or "wallet_alpha" not in signal_type:
            continue
        trade_id = str(row.get("trade_id") or "")
        if trade_id:
            entry_by_trade_id[trade_id] = row

    markouts: list[dict[str, Any]] = []
    for row in lifecycle_rows:
        if row.get("event") not in {"position_closed", "position_partially_closed"}:
            continue
        entry = entry_by_trade_id.get(str(row.get("open_trade_id") or ""))
        if entry is None:
            continue
        context = entry.get("decision_context") if isinstance(entry.get("decision_context"), dict) else {}
        close_size = _float(row.get("close_size"))
        open_price = _float(row.get("open_price") or (entry.get("result") or {}).get("avg_fill_price") or entry.get("price"))
        notional = open_price * close_size
        if notional <= 0:
            continue
        realized = _float(row.get("realized_pnl"))
        markouts.append(
            {
                "wallet_address": str(context.get("wallet_address") or ""),
                "market_id": str(row.get("market_id") or entry.get("market_id") or ""),
                "category": str(context.get("category") or ""),
                "notional_usdc": round(notional, 8),
                "realized_pnl_usdc": round(realized, 8),
                "lagged_follow_pnl_usdc": round(realized, 8),
            }
        )
    return markouts


def _category_edges(rows: list[dict[str, Any]]) -> dict[str, float]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        category = str(row.get("category") or "").strip()
        if category:
            by_category[category].append(row)
    out: dict[str, float] = {}
    for category, category_rows in by_category.items():
        notional = sum(_float(row.get("notional_usdc") or row.get("notional")) for row in category_rows)
        if notional <= 0:
            continue
        pnl = sum(_float(row.get("lagged_follow_pnl_usdc") or row.get("lagged_follow_pnl")) for row in category_rows)
        out[category] = round(pnl / notional, 8)
    return out


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    total_notional = 0.0
    for row in rows:
        equity += _float(row.get("lagged_follow_pnl_usdc") or row.get("lagged_follow_pnl"))
        total_notional += _float(row.get("notional_usdc") or row.get("notional"))
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown / total_notional if total_notional > 0 else 1.0


def _is_binary_market(market: MarketInfo) -> bool:
    outcomes = {(token.outcome or "").strip().lower() for token in market.tokens}
    return {"yes", "no"}.issubset(outcomes) or len(market.tokens) == 2


def _float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
