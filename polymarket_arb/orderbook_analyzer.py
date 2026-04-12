"""订单簿分析器：读取 CLOB 订单簿并提取可执行价格/深度信息."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from polymarket_arb.models import OrderBookLevel, OrderBookSnapshot

LOG = logging.getLogger(__name__)


def _parse_level(raw: Any) -> Optional[OrderBookLevel]:
    """将 CLOB 返回的价位对象转为 OrderBookLevel."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        price = raw.get("price")
        size = raw.get("size")
    else:
        price = getattr(raw, "price", None)
        size = getattr(raw, "size", None)
    if price is None or size is None:
        return None
    try:
        return OrderBookLevel(price=float(price), size=float(size))
    except (ValueError, TypeError):
        return None


def _merge_levels(levels: list[OrderBookLevel], *, reverse: bool) -> list[OrderBookLevel]:
    merged: dict[float, float] = {}
    for level in levels:
        merged[level.price] = merged.get(level.price, 0.0) + level.size
    return [
        OrderBookLevel(price=price, size=size)
        for price, size in sorted(merged.items(), key=lambda item: item[0], reverse=reverse)
    ]


class OrderBookAnalyzer:
    """从 CLOB 客户端拉取订单簿并构建结构化快照."""

    def __init__(
        self,
        clob_client: Any,
        snapshot_ttl_sec: float = 0.5,
        *,
        retry_count: int = 2,
        retry_delay_sec: float = 0.15,
    ):
        self._client = clob_client
        self._snapshot_ttl_sec = max(0.0, snapshot_ttl_sec)
        self._retry_count = max(0, int(retry_count))
        self._retry_delay_sec = max(0.0, float(retry_delay_sec))
        self._snapshot_cache: dict[str, OrderBookSnapshot] = {}

    def get_snapshot(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """获取单个 token 的订单簿快照."""
        cached = self._snapshot_cache.get(token_id)
        now = time.time()
        if cached is not None and self._snapshot_ttl_sec > 0 and (now - cached.timestamp) <= self._snapshot_ttl_sec:
            return cached

        book = None
        correlation_id = f"book-{token_id[:12]}-{int(now * 1000)}"
        for attempt in range(self._retry_count + 1):
            try:
                book = self._client.get_order_book(token_id)
                break
            except Exception as e:
                if attempt < self._retry_count:
                    LOG.warning(
                        "[cid=%s] get_order_book 失败，准备重试 (%d/%d) token=%s…: %s",
                        correlation_id,
                        attempt + 1,
                        self._retry_count + 1,
                        token_id[:20],
                        e,
                    )
                    if self._retry_delay_sec > 0:
                        time.sleep(self._retry_delay_sec)
                    continue
                LOG.error("[cid=%s] get_order_book 失败 token=%s…: %s", correlation_id, token_id[:20], e)
                return None

        if book is None:
            return None

        raw_bids = getattr(book, "bids", None) or []
        raw_asks = getattr(book, "asks", None) or []
        tick = float(getattr(book, "tick_size", None) or "0.01")

        bids = _merge_levels(
            [lv for raw in raw_bids if (lv := _parse_level(raw)) is not None],
            reverse=True,
        )
        asks = _merge_levels(
            [lv for raw in raw_asks if (lv := _parse_level(raw)) is not None],
            reverse=False,
        )

        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None

        snapshot = OrderBookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=tick,
            bids=bids,
            asks=asks,
            timestamp=now,
        )
        self._snapshot_cache[token_id] = snapshot
        return snapshot

    def get_best_ask_with_depth(
        self, token_id: str, min_size: float = 0.0
    ) -> Optional[tuple[float, float]]:
        """获取满足最小深度要求的最优卖价和可用数量.

        Returns:
            (price, available_size) 或 None（无足够深度时）
        """
        snap = self.get_snapshot(token_id)
        if snap is None or not snap.asks:
            return None

        if min_size <= 0:
            return (snap.asks[0].price, snap.asks[0].size)

        cumulative_size = 0.0
        for level in snap.asks:
            cumulative_size += level.size
            if cumulative_size >= min_size:
                return (level.price, cumulative_size)

        if snap.asks:
            return (snap.asks[-1].price, cumulative_size)
        return None

    def get_executable_ask_price(
        self, token_id: str, target_size: float
    ) -> Optional[tuple[float, float]]:
        """计算以 target_size 吃单时的加权平均成交价.

        Returns:
            (vwap, filled_size) — 加权平均价和实际可填充的数量
        """
        snap = self.get_snapshot(token_id)
        if snap is None or not snap.asks:
            return None

        total_cost = 0.0
        filled = 0.0
        for level in snap.asks:
            take = min(level.size, target_size - filled)
            total_cost += take * level.price
            filled += take
            if filled >= target_size - 1e-9:
                break

        if filled <= 0:
            return None
        vwap = total_cost / filled
        return (vwap, filled)

    def get_executable_bid_price(
        self, token_id: str, target_size: float
    ) -> Optional[tuple[float, float]]:
        """计算以 target_size 打 bid 时的加权平均成交价."""
        snap = self.get_snapshot(token_id)
        if snap is None or not snap.bids:
            return None

        total_value = 0.0
        filled = 0.0
        for level in snap.bids:
            take = min(level.size, target_size - filled)
            total_value += take * level.price
            filled += take
            if filled >= target_size - 1e-9:
                break

        if filled <= 0:
            return None
        vwap = total_value / filled
        return (vwap, filled)

    def batch_get_snapshots(
        self, token_ids: list[str], delay: float = 0.05
    ) -> dict[str, OrderBookSnapshot]:
        """批量获取多个 token 的订单簿快照."""
        result: dict[str, OrderBookSnapshot] = {}
        for tid in token_ids:
            snap = self.get_snapshot(tid)
            if snap is not None:
                result[tid] = snap
            if delay > 0 and tid != token_ids[-1]:
                time.sleep(delay)
        return result
