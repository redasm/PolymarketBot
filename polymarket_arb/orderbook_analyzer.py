"""订单簿分析器：读取 CLOB 订单簿并提取可执行价格/深度信息."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from polymarket_arb.models import OrderBookLevel, OrderBookSnapshot

LOG = logging.getLogger(__name__)
_ORDERBOOK_STAT_KEYS = (
    "requests",
    "ws_hit",
    "cache_hit",
    "rest_fallback",
    "rest_success",
    "rest_error",
    "missing_orderbook",
    "cooldown_skip",
)


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
        live_mirror: Any | None = None,
        ws_snapshot_max_age_sec: float = 10.0,
        retry_count: int = 2,
        retry_delay_sec: float = 0.15,
        missing_orderbook_cooldown_sec: float = 300.0,
    ):
        self._client = clob_client
        self._live_mirror = live_mirror
        self._snapshot_ttl_sec = max(0.0, snapshot_ttl_sec)
        self._ws_snapshot_max_age_sec = max(0.0, float(ws_snapshot_max_age_sec))
        self._retry_count = max(0, int(retry_count))
        self._retry_delay_sec = max(0.0, float(retry_delay_sec))
        self._missing_orderbook_cooldown_sec = max(0.0, float(missing_orderbook_cooldown_sec))
        self._snapshot_cache: dict[str, OrderBookSnapshot] = {}
        self._snapshot_cache_source: dict[str, str] = {}
        self._missing_orderbook_until: dict[str, float] = {}
        self._stats_lock = threading.Lock()
        self._stats: dict[str, int] = {key: 0 for key in _ORDERBOOK_STAT_KEYS}

    def set_live_mirror(self, live_mirror: Any | None) -> None:
        self._live_mirror = live_mirror

    def snapshot_stats(self, *, reset: bool = False) -> dict[str, int]:
        with self._stats_lock:
            snap = dict(self._stats)
            if reset:
                self._stats = {key: 0 for key in _ORDERBOOK_STAT_KEYS}
            return snap

    def get_snapshot(
        self,
        token_id: str,
        *,
        allow_rest_fallback: bool = True,
        count_request: bool = True,
    ) -> Optional[OrderBookSnapshot]:
        """获取单个 token 的订单簿快照."""
        if count_request:
            self._record_stat("requests")
        now = time.time()
        live = self._get_live_snapshot(token_id, now=now)
        if live is not None:
            return live

        cached = self._snapshot_cache.get(token_id)
        if cached is not None:
            cache_source = self._snapshot_cache_source.get(token_id, "rest")
            max_age = self._ws_snapshot_max_age_sec if cache_source == "ws" else self._snapshot_ttl_sec
            if max_age > 0 and (now - cached.timestamp) <= max_age:
                self._record_stat("cache_hit")
                return cached
            self._evict_cached_snapshot(token_id)
        if not allow_rest_fallback:
            return None
        self._record_stat("rest_fallback")
        missing_until = self._missing_orderbook_until.get(token_id)
        if missing_until is not None:
            if now < missing_until:
                self._record_stat("cooldown_skip")
                return None
            self._missing_orderbook_until.pop(token_id, None)

        book = None
        correlation_id = f"book-{token_id[:12]}-{int(now * 1000)}"
        for attempt in range(self._retry_count + 1):
            try:
                book = self._client.get_order_book(token_id)
                break
            except Exception as e:
                if _is_missing_orderbook_error(e):
                    self._missing_orderbook_until[token_id] = time.time() + self._missing_orderbook_cooldown_sec
                    self._record_stat("missing_orderbook")
                    LOG.warning(
                        "[cid=%s] token=%s… 暂无 orderbook，进入 %.0fs 冷却",
                        correlation_id,
                        token_id[:20],
                        self._missing_orderbook_cooldown_sec,
                    )
                    return None
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
                self._record_stat("rest_error")
                LOG.error("[cid=%s] get_order_book 失败 token=%s…: %s", correlation_id, token_id[:20], e)
                return None

        if book is None:
            self._record_stat("rest_error")
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
        self._set_cached_snapshot(token_id, snapshot, source="rest")
        self._record_stat("rest_success")
        return snapshot

    def _get_live_snapshot(self, token_id: str, *, now: float) -> Optional[OrderBookSnapshot]:
        if self._live_mirror is None:
            return None
        try:
            snap = self._live_mirror.get(token_id)
        except Exception as e:
            LOG.debug("读取 WS 订单簿镜像失败 token=%s…: %s", token_id[:20], e)
            return None
        if snap is None:
            return None
        snap_ts = float(getattr(snap, "timestamp", 0.0) or 0.0)
        if self._ws_snapshot_max_age_sec > 0 and snap_ts > 0 and (now - snap_ts) > self._ws_snapshot_max_age_sec:
            return None
        self._set_cached_snapshot(token_id, snap, source="ws")
        self._record_stat("ws_hit")
        return snap

    def _set_cached_snapshot(self, token_id: str, snapshot: OrderBookSnapshot, *, source: str) -> None:
        self._snapshot_cache[token_id] = snapshot
        self._snapshot_cache_source[token_id] = source

    def _evict_cached_snapshot(self, token_id: str) -> None:
        self._snapshot_cache.pop(token_id, None)
        self._snapshot_cache_source.pop(token_id, None)

    def _record_stat(self, key: str, amount: int = 1) -> None:
        if key not in _ORDERBOOK_STAT_KEYS:
            return
        with self._stats_lock:
            self._stats[key] += int(amount)

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
        self,
        token_ids: list[str],
        delay: float = 0.05,
        *,
        allow_rest_fallback: bool = True,
    ) -> dict[str, OrderBookSnapshot]:
        """批量获取多个 token 的订单簿快照."""
        result: dict[str, OrderBookSnapshot] = {}
        missing: list[str] = []
        deduped = list(dict.fromkeys(token_ids))
        self._record_stat("requests", len(deduped))

        for tid in deduped:
            snap = self.get_snapshot(tid, allow_rest_fallback=False, count_request=False)
            if snap is not None:
                result[tid] = snap
            else:
                missing.append(tid)
        if not allow_rest_fallback:
            return result

        for idx, tid in enumerate(missing):
            snap = self.get_snapshot(tid, allow_rest_fallback=True, count_request=False)
            if snap is not None:
                result[tid] = snap
            if delay > 0 and idx != len(missing) - 1:
                time.sleep(delay)
        return result


def _is_missing_orderbook_error(error: Exception) -> bool:
    message = str(error).lower()
    return "no orderbook exists" in message
