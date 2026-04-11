"""市场上下文构建器：将分散的市场数据收集并压缩为 AI 可消费的结构化 context.

控制 token 量在 2000-4000 以内，通过精简字段名和截断列表长度来实现。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.models import MarketContext, MarketInfo, RiskState
from polymarket_arb.volatility_estimator import VolEstimator


class MarketContextBuilder:
    """从各数据源收集并压缩市场上下文."""

    def __init__(self, max_markets: int = 10) -> None:
        self._max_markets = max_markets

    def build(
        self,
        *,
        active_markets: list[MarketInfo] | None = None,
        book_store: EnhancedBookStore | None = None,
        vol_estimator: VolEstimator | None = None,
        edge_signals: list[dict] | None = None,
        recent_trades: list[dict] | None = None,
        risk_state: RiskState | None = None,
    ) -> MarketContext:
        """从各组件收集数据构建 MarketContext."""
        market_summaries = self._summarize_markets(active_markets or [])
        book_summary = book_store.get_summary() if book_store else {}
        vol_snap = vol_estimator.snapshot() if vol_estimator else {}

        risk_dict: dict[str, Any] = {}
        if risk_state:
            risk_dict = {
                "exposure": round(risk_state.total_exposure, 2),
                "positions": risk_state.open_positions,
                "daily_pnl": round(risk_state.daily_pnl, 2),
                "halted": risk_state.is_halted,
            }

        return MarketContext(
            timestamp=time.time(),
            active_markets=market_summaries,
            orderbook_summary=book_summary,
            volatility=vol_snap,
            edge_signals=(edge_signals or [])[-5:],
            recent_trades=(recent_trades or [])[-10:],
            risk_state=risk_dict,
        )

    def to_prompt_text(self, ctx: MarketContext) -> str:
        """将 MarketContext 序列化为紧凑的文本，供 LLM 消费."""
        parts: list[str] = []

        if ctx.active_markets:
            parts.append("## Active Markets")
            for m in ctx.active_markets[:self._max_markets]:
                parts.append(
                    f"- {m.get('q', '?')}: price={m.get('price', '?')}, "
                    f"vol24h=${m.get('vol24h', 0):.0f}, liq=${m.get('liq', 0):.0f}"
                )

        if ctx.orderbook_summary:
            s = ctx.orderbook_summary
            parts.append(
                f"\n## Orderbook: yes_mid={s.get('yes_mid')}, no_mid={s.get('no_mid')}, "
                f"yes_spread={s.get('yes_spread_bps')}bps, no_spread={s.get('no_spread_bps')}bps"
            )

        if ctx.volatility:
            v = ctx.volatility
            parts.append(
                f"\n## Volatility: fast_15m={v.get('sigma_fast_15m')}, "
                f"slow_15m={v.get('sigma_slow_15m')}, blend={v.get('sigma_blend_15m')}, "
                f"ready={v.get('ready')}"
            )

        if ctx.edge_signals:
            parts.append("\n## Recent Edge Signals")
            for sig in ctx.edge_signals:
                parts.append(
                    f"- dir={sig.get('direction')}, edge={sig.get('edge_bps')}bps, "
                    f"conf={sig.get('confidence')}"
                )

        if ctx.risk_state:
            r = ctx.risk_state
            parts.append(
                f"\n## Risk: exposure=${r.get('exposure', 0)}, "
                f"positions={r.get('positions', 0)}, "
                f"daily_pnl=${r.get('daily_pnl', 0)}, halted={r.get('halted', False)}"
            )

        if ctx.recent_trades:
            parts.append(f"\n## Recent Trades: {len(ctx.recent_trades)} entries")

        return "\n".join(parts)

    def _summarize_markets(self, markets: list[MarketInfo]) -> list[dict]:
        """将 MarketInfo 列表精简为轻量 dict，控制 token 用量."""
        result: list[dict] = []
        for m in markets[:self._max_markets]:
            prices = m.outcome_prices[:2] if m.outcome_prices else []
            result.append({
                "id": m.condition_id[:12],
                "q": m.question[:80],
                "price": prices[0] if prices else None,
                "vol24h": m.volume_24h,
                "liq": m.liquidity,
                "neg_risk": m.neg_risk,
            })
        return result
