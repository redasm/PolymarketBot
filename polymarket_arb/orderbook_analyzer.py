"""订单簿分析器：读取 CLOB 订单簿并提取可执行价格/深度信息."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def _live_mirror_snapshots(live_mirror: Any) -> dict[str, OrderBookSnapshot]:
    """Return all live mirror snapshots across supported mirror interfaces."""
    if hasattr(live_mirror, "get_all"):
        snapshots = live_mirror.get_all()
    elif hasattr(live_mirror, "all"):
        snapshots = live_mirror.all()
    elif isinstance(live_mirror, dict):
        snapshots = live_mirror
    else:
        return {}
    return snapshots if isinstance(snapshots, dict) else {}


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
        feed_health_cache_ttl_sec: float = 0.2,
        batch_concurrency: int = 16,
        ws_liveness_sec: float = 20.0,
    ):
        self._client = clob_client
        self._live_mirror = live_mirror
        self._snapshot_ttl_sec = max(0.0, snapshot_ttl_sec)
        self._ws_snapshot_max_age_sec = max(0.0, float(ws_snapshot_max_age_sec))
        self._retry_count = max(0, int(retry_count))
        self._retry_delay_sec = max(0.0, float(retry_delay_sec))
        self._missing_orderbook_cooldown_sec = max(0.0, float(missing_orderbook_cooldown_sec))
        self._batch_concurrency = max(1, int(batch_concurrency))
        self._ws_liveness_sec = max(0.0, float(ws_liveness_sec))
        self._snapshot_cache: dict[str, OrderBookSnapshot] = {}
        self._snapshot_cache_source: dict[str, str] = {}
        self._missing_orderbook_until: dict[str, float] = {}
        self._missing_orderbook_failures: dict[str, int] = {}
        self._stats_lock = threading.Lock()
        self._stats: dict[str, int] = {key: 0 for key in _ORDERBOOK_STAT_KEYS}
        # P2-feedhealth: TTL on global-path feed_health results.
        # The orchestrator + signal paths can call feed_health
        # multiple times per cycle (T1 cross-platform check + T2
        # statistical + T3 maker) — caching the global verdict for
        # 200ms eliminates redundant ``get_all`` + dict scans without
        # hiding genuine staleness (one scan cycle is typically ≥3s).
        self._feed_health_cache_ttl_sec = max(0.0, float(feed_health_cache_ttl_sec))
        self._feed_health_cache: tuple[tuple[float, float], float, dict[str, Any]] | None = None

    def set_live_mirror(self, live_mirror: Any | None) -> None:
        self._live_mirror = live_mirror

    def snapshot_stats(self, *, reset: bool = False) -> dict[str, int]:
        with self._stats_lock:
            snap = dict(self._stats)
            if reset:
                self._stats = {key: 0 for key in _ORDERBOOK_STAT_KEYS}
            return snap

    def feed_health(
        self,
        *,
        max_snapshot_age_sec: float,
        min_ws_hit_ratio: float,
        token_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return live-trading orderbook health from recent source stats.

        Pass ``token_ids`` to scope the staleness check to the markets a
        specific signal actually cares about — without that scoping a
        single idle hot-pool token whose mirror hasn't been pushed in N
        seconds will fail the *entire* check and block every T2 signal
        in the system. Token ids that aren't in the live mirror are
        skipped (they'll fall through to REST / cache via
        ``get_snapshot``, which is the right behaviour — REST has its
        own freshness contract).

        When ``token_ids`` is None the check sweeps every mirror entry
        (legacy behaviour, kept for callers that want a global view).
        Global-path results are cached for ``_feed_health_cache_ttl_sec``
        so per-cycle hot loops calling ``feed_health()`` repeatedly
        don't pay the ``get_all`` lock contention + dict copy cost on
        every call. The token-scoped path bypasses cache because the
        scope changes between callers.
        """
        if self._live_mirror is None:
            return {"healthy": False, "reason": "ws_mirror_unavailable"}

        now = time.time()
        if (
            token_ids is None
            and self._feed_health_cache_ttl_sec > 0
            and self._feed_health_cache is not None
        ):
            cache_key, cache_ts, cache_result = self._feed_health_cache
            if (
                cache_key == (max_snapshot_age_sec, min_ws_hit_ratio)
                and (now - cache_ts) < self._feed_health_cache_ttl_sec
            ):
                return cache_result

        stats = self.snapshot_stats(reset=False)
        requests = max(0, int(stats.get("requests", 0)))
        ws_hits = max(0, int(stats.get("ws_hit", 0)))
        rest_errors = max(0, int(stats.get("rest_error", 0)))
        missing = max(0, int(stats.get("missing_orderbook", 0)))
        ws_hit_ratio = (ws_hits / requests) if requests > 0 else 0.0
        if requests > 0 and ws_hit_ratio < min_ws_hit_ratio:
            result = {
                "healthy": False,
                "reason": "ws_hit_ratio_low",
                "ws_hit_ratio": ws_hit_ratio,
                "stats": stats,
            }
            return self._maybe_cache_feed_health(token_ids, max_snapshot_age_sec, min_ws_hit_ratio, now, result)
        if rest_errors > 0:
            result = {"healthy": False, "reason": "rest_errors_present", "stats": stats}
            return self._maybe_cache_feed_health(token_ids, max_snapshot_age_sec, min_ws_hit_ratio, now, result)
        if missing > 0:
            result = {"healthy": False, "reason": "missing_orderbooks_present", "stats": stats}
            return self._maybe_cache_feed_health(token_ids, max_snapshot_age_sec, min_ws_hit_ratio, now, result)

        snapshots = _live_mirror_snapshots(self._live_mirror)
        if token_ids is not None:
            relevant = (snapshots.get(tid) for tid in token_ids)
            scoped_iter = [s for s in relevant if s is not None]
        else:
            scoped_iter = list(snapshots.values())
        for snap in scoped_iter:
            snap_ts = float(getattr(snap, "timestamp", 0.0) or 0.0)
            if snap_ts > 0 and (now - snap_ts) > max_snapshot_age_sec:
                result = {
                    "healthy": False,
                    "reason": "stale_ws_snapshot",
                    "snapshot_age_sec": now - snap_ts,
                }
                return self._maybe_cache_feed_health(token_ids, max_snapshot_age_sec, min_ws_hit_ratio, now, result)
        result = {"healthy": True, "reason": "", "ws_hit_ratio": ws_hit_ratio, "stats": stats}
        return self._maybe_cache_feed_health(token_ids, max_snapshot_age_sec, min_ws_hit_ratio, now, result)

    def _maybe_cache_feed_health(
        self,
        token_ids: list[str] | None,
        max_snapshot_age_sec: float,
        min_ws_hit_ratio: float,
        now: float,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if token_ids is None and self._feed_health_cache_ttl_sec > 0:
            self._feed_health_cache = (
                (max_snapshot_age_sec, min_ws_hit_ratio),
                now,
                result,
            )
        return result

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
                    failures = self._missing_orderbook_failures.get(token_id, 0) + 1
                    self._missing_orderbook_failures[token_id] = failures
                    cooldown = self._missing_orderbook_backoff_sec(failures)
                    self._missing_orderbook_until[token_id] = time.time() + cooldown
                    self._record_stat("missing_orderbook")
                    LOG.warning(
                        "[cid=%s] token=%s… 暂无 orderbook，进入 %.0fs 冷却",
                        correlation_id,
                        token_id[:20],
                        cooldown,
                    )
                    return None
                if attempt < self._retry_count:
                    LOG.debug(
                        "[cid=%s] get_order_book 失败，准备重试 (%d/%d) token=%s…: %s",
                        correlation_id,
                        attempt + 1,
                        self._retry_count + 1,
                        token_id[:20],
                        e,
                    )
                    continue
                self._record_stat("rest_error")
                LOG.error(
                    "[cid=%s] get_order_book 失败 token=%s… 重试 %d 次全部失败: %s",
                    correlation_id,
                    token_id[:20],
                    self._retry_count + 1,
                    e,
                )
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
        self._missing_orderbook_failures.pop(token_id, None)
        self._missing_orderbook_until.pop(token_id, None)
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
        if (
            self._ws_snapshot_max_age_sec > 0
            and snap_ts > 0
            and (now - snap_ts) > self._ws_snapshot_max_age_sec
            and not self._mirror_trusts(snap_ts, now)
        ):
            return None
        # Hot-path micro-opt: scan_cycle calls get_snapshot ≥2× per
        # binary market every cycle. The WS mirror typically returns
        # the same snapshot reference until the next delta lands, so
        # re-writing the cache dicts (two writes per call) is pure
        # overhead. Compare by identity *and* timestamp to also avoid
        # stamping a re-rebuilt snapshot that happens to share the
        # epoch — cheaper than a deep eq.
        cached = self._snapshot_cache.get(token_id)
        if (
            cached is None
            or cached is not snap
            or float(getattr(cached, "timestamp", 0.0) or 0.0) != snap_ts
            or self._snapshot_cache_source.get(token_id) != "ws"
        ):
            self._set_cached_snapshot(token_id, snap, source="ws")
        self._record_stat("ws_hit")
        return snap

    def _mirror_trusts(self, snap_ts: float, now: float) -> bool:
        is_trusted = getattr(self._live_mirror, "is_trusted", None)
        if is_trusted is None or self._ws_liveness_sec <= 0:
            return False
        try:
            return bool(is_trusted(snap_ts, now, self._ws_liveness_sec))
        except Exception as e:
            LOG.debug("读取 WS 镜像存活状态失败: %s", e)
            return False

    def _set_cached_snapshot(self, token_id: str, snapshot: OrderBookSnapshot, *, source: str) -> None:
        self._snapshot_cache[token_id] = snapshot
        self._snapshot_cache_source[token_id] = source

    def _evict_cached_snapshot(self, token_id: str) -> None:
        self._snapshot_cache.pop(token_id, None)
        self._snapshot_cache_source.pop(token_id, None)

    def _missing_orderbook_backoff_sec(self, failures: int) -> float:
        base = self._missing_orderbook_cooldown_sec
        if base <= 0:
            return 0.0
        exponent = min(max(0, int(failures) - 1), 6)
        return min(base * (2 ** exponent), 6 * 3600.0)

    def _record_stat(self, key: str, amount: int = 1) -> None:
        if key not in _ORDERBOOK_STAT_KEYS:
            return
        with self._stats_lock:
            self._stats[key] += int(amount)

    def _record_stats_bulk(self, increments: dict[str, int]) -> None:
        """Batch variant of :meth:`_record_stat` — one lock acquisition
        for all keys in ``increments``. The scan/orchestrator path can
        aggregate per-call counters locally and call this once per
        cycle so the WS callback thread isn't pre-empting on
        ``_stats_lock`` hundreds of times per scan cycle.
        """
        if not increments:
            return
        with self._stats_lock:
            for key, amount in increments.items():
                if key in _ORDERBOOK_STAT_KEYS and amount:
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
        delay: float = 0.0,
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

        if delay <= 0 and len(missing) > 1 and self._batch_concurrency > 1:
            # Cold-start / low-WS-coverage cycles can leave hundreds of
            # tokens with no live or cached snapshot, each needing its own
            # REST round trip. Fetching them one at a time here turns a
            # ~5s scan cycle into minutes (each token's individual
            # get_snapshot() writes to a distinct cache/cooldown dict key,
            # so concurrent calls don't need extra locking beyond the
            # existing _stats_lock).
            with ThreadPoolExecutor(max_workers=min(self._batch_concurrency, len(missing))) as pool:
                for tid, snap in zip(missing, pool.map(
                    lambda t: self.get_snapshot(t, allow_rest_fallback=True, count_request=False),
                    missing,
                )):
                    if snap is not None:
                        result[tid] = snap
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
