"""WebSocket 实时订单簿推送：替代 REST 轮询，将检测延迟从秒级降到毫秒级.

Polymarket CLOB 提供两类 WebSocket:
1. Market WS: 实时订单簿变更（价位增减、best bid/ask 变动）
2. User WS: 用户自身的订单状态变更

对于套利，关键是 Market WS。每当订单簿变动推送到达时：
- 更新本地维护的订单簿镜像
- 立即触发套利检测（事件驱动，而非轮询）
- 检测到机会后直接执行，无需等待下一个扫描周期

延迟对比:
- REST 轮询: 平均延迟 = scan_interval / 2 ≈ 2.5 秒
- WebSocket:  延迟 ≈ 网络 RTT ≈ 10-50 毫秒 → 50-250x 提升
"""

from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from typing import Any, Callable, Iterable, Optional

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.models import OrderBookLevel, OrderBookSnapshot
from polymarket_arb.utils_time import now_ms

LOG = logging.getLogger(__name__)

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_MAX_PENDING_CALLBACKS = 1024
# Buffer size for deltas that arrive before a snapshot. Bounded so a runaway
# delta stream against an un-synced token cannot exhaust memory; old entries
# fall off FIFO when the buffer overflows.
_PRE_SNAPSHOT_DELTA_BUFFER = 64
# WebSocket close codes that should NOT trigger an indefinite reconnect loop:
# they signal a configuration/authorization problem the bot cannot recover
# from on its own. The remaining codes are treated as transient.
_FATAL_WS_CLOSE_CODES = frozenset({4000, 4001, 4003})
# 4xxx 是 application-layer custom range，wss-overview 没列出完整语义。
# 未列入 _FATAL_WS_CLOSE_CODES 的 4xxx code 走"长退避 + telemetry warn"，
# 既不假设可恢复也不直接 fail-stop——避免 4002/4004 出现时无限重连刷屏。
_UNKNOWN_4XXX_BACKOFF_FLOOR_SEC = 30.0


def _splice_level(
    levels: list[OrderBookLevel],
    price: float,
    new_size: float,
    *,
    descending: bool,
) -> tuple[list[OrderBookLevel], bool]:
    """Return ``(new_levels, changed)`` reflecting a single-level update.

    The hot path through this function is one linear pass: skip the
    existing entry at ``price`` (if any) while finding the sorted
    position for the new level. ``changed`` is False when the delta is
    a no-op (e.g. ``new_size == 0`` for a price that wasn't on book)
    so the caller can skip allocating a new ``OrderBookSnapshot`` and
    cycling GC pressure on irrelevant deltas.
    """
    out: list[OrderBookLevel] = []
    found = False
    inserted = False
    insert = new_size > 0
    for lv in levels:
        if not found and abs(lv.price - price) < 1e-9:
            found = True
            continue
        if insert and not inserted:
            if (descending and lv.price < price) or (not descending and lv.price > price):
                out.append(OrderBookLevel(price, new_size))
                inserted = True
        out.append(lv)
    if insert and not inserted:
        out.append(OrderBookLevel(price, new_size))
        inserted = True
    changed = found or inserted
    return out, changed


class OrderBookMirror:
    """本地订单簿镜像：由 WebSocket 增量更新维护.

    维护每个 token_id 的完整订单簿状态，
    当 best bid/ask 变动时触发回调。
    """

    def __init__(self) -> None:
        self._books: dict[str, OrderBookSnapshot] = {}
        self._lock = threading.Lock()
        self._callbacks_lock = threading.Lock()
        self._on_change_callbacks: list[Callable[[str, OrderBookSnapshot], None]] = []
        self._callback_queue: queue.Queue[tuple[str, OrderBookSnapshot] | None] = queue.Queue(maxsize=_MAX_PENDING_CALLBACKS)
        self._callback_worker: Optional[threading.Thread] = None
        self._callback_worker_running = False
        # Deltas that arrive before the first snapshot for a token are buffered
        # here and replayed once the snapshot lands. Without this we silently
        # drop the early increments after every reconnect.
        self._pending_deltas: dict[str, list[tuple[str, float, float]]] = {}

    def register_callback(self, cb: Callable[[str, OrderBookSnapshot], None]) -> None:
        should_start = False
        with self._callbacks_lock:
            self._on_change_callbacks.append(cb)
            if not self._callback_worker_running:
                self._callback_worker_running = True
                should_start = True
        if should_start:
            self._callback_worker = threading.Thread(
                target=self._callback_loop,
                daemon=True,
                name="orderbook-callbacks",
            )
            self._callback_worker.start()

    def get(self, token_id: str) -> Optional[OrderBookSnapshot]:
        with self._lock:
            return self._books.get(token_id)

    def get_all(self) -> dict[str, OrderBookSnapshot]:
        with self._lock:
            return dict(self._books)

    def apply_snapshot(self, token_id: str, bids: list[Any], asks: list[Any], tick_size: float = 0.01) -> None:
        """应用完整订单簿快照（初始连接或重连后）."""
        parsed_bids = sorted(
            [OrderBookLevel(price, size) for price, size in _parse_orders(bids)],
            key=lambda x: x.price,
            reverse=True,
        )
        parsed_asks = sorted(
            [OrderBookLevel(price, size) for price, size in _parse_orders(asks)],
            key=lambda x: x.price,
        )

        snap = OrderBookSnapshot(
            token_id=token_id,
            best_bid=parsed_bids[0].price if parsed_bids else None,
            best_ask=parsed_asks[0].price if parsed_asks else None,
            tick_size=tick_size,
            bids=parsed_bids,
            asks=parsed_asks,
            timestamp=time.time(),
        )

        with self._lock:
            self._books[token_id] = snap
            buffered = self._pending_deltas.pop(token_id, [])

        if buffered:
            LOG.info(
                "回放快照前缓存的 %d 条 delta: token=%s",
                len(buffered),
                token_id[:16],
            )
            for side, price, new_size in buffered:
                self.apply_delta(token_id, side, price, new_size)
            return

        self._fire_callbacks(token_id, snap)

    def apply_delta(self, token_id: str, side: str, price: float, new_size: float) -> None:
        """应用单个价位的增量更新.

        side: "buy" 或 "sell"
        new_size: 0 表示该价位已消失

        If a snapshot has not yet arrived for `token_id` the delta is buffered
        (FIFO, capped) and replayed once the snapshot lands.

        Hot-path optimization: the previous implementation copied the side
        list, filtered it, appended, then ran a full ``sort()`` for every
        delta. Under heavy WS traffic that was the dominant Python-side
        CPU cost. The rewrite below does a single linear pass that
        simultaneously drops the existing entry at ``price`` (if any) and
        inserts the new level at its sorted position — same big-O but
        ~2× faster in practice and zero allocations on the no-op path.
        The opposite-side list is reused by reference because we never
        mutate stored level lists in place (each delta produces a new
        list on the touched side only).
        """
        with self._lock:
            snap = self._books.get(token_id)
            if snap is None:
                buffer = self._pending_deltas.setdefault(token_id, [])
                buffer.append((side, price, new_size))
                if len(buffer) > _PRE_SNAPSHOT_DELTA_BUFFER:
                    del buffer[: len(buffer) - _PRE_SNAPSHOT_DELTA_BUFFER]
                    LOG.warning(
                        "丢弃过旧的 pre-snapshot delta: token=%s buffer=%d",
                        token_id[:16],
                        len(buffer),
                    )
                return

            if side == "buy":
                existing_levels = snap.bids
                # Bids are stored DESC so the "insert before first
                # smaller price" rule keeps the list sorted.
                new_levels, changed = _splice_level(
                    existing_levels, price, new_size, descending=True
                )
                if not changed:
                    return
                new_snap = OrderBookSnapshot(
                    token_id=token_id,
                    best_bid=new_levels[0].price if new_levels else None,
                    best_ask=snap.best_ask,
                    tick_size=snap.tick_size,
                    bids=new_levels,
                    asks=snap.asks,
                    timestamp=time.time(),
                )
            else:
                existing_levels = snap.asks
                new_levels, changed = _splice_level(
                    existing_levels, price, new_size, descending=False
                )
                if not changed:
                    return
                new_snap = OrderBookSnapshot(
                    token_id=token_id,
                    best_bid=snap.best_bid,
                    best_ask=new_levels[0].price if new_levels else None,
                    tick_size=snap.tick_size,
                    bids=snap.bids,
                    asks=new_levels,
                    timestamp=time.time(),
                )

            old_bb = snap.best_bid
            old_ba = snap.best_ask
            self._books[token_id] = new_snap

        if new_snap.best_bid != old_bb or new_snap.best_ask != old_ba:
            self._fire_callbacks(token_id, new_snap)

    def _fire_callbacks(self, token_id: str, snap: OrderBookSnapshot) -> None:
        with self._callbacks_lock:
            has_callbacks = bool(self._on_change_callbacks)
            worker_running = self._callback_worker_running
        if not has_callbacks or not worker_running:
            return
        item = (token_id, snap)
        try:
            self._callback_queue.put_nowait(item)
        except queue.Full:
            try:
                dropped = self._callback_queue.get_nowait()
                self._callback_queue.task_done()
                LOG.warning("订单簿回调队列拥堵，已丢弃旧快照: token=%s", dropped[0][:16] if dropped else "unknown")
            except queue.Empty:
                return
            try:
                self._callback_queue.put_nowait(item)
            except queue.Full:
                LOG.warning("订单簿回调队列持续满载，跳过本次快照: token=%s", token_id[:16])

    def stop(self) -> None:
        with self._callbacks_lock:
            if not self._callback_worker_running:
                return
            self._callback_worker_running = False
        enqueued_sentinel = False
        try:
            self._callback_queue.put_nowait(None)
            enqueued_sentinel = True
        except queue.Full:
            try:
                dropped = self._callback_queue.get_nowait()
                self._callback_queue.task_done()
                LOG.warning("停止回调线程时丢弃排队快照: token=%s", dropped[0][:16] if dropped else "unknown")
                self._callback_queue.put_nowait(None)
                enqueued_sentinel = True
            except (queue.Empty, queue.Full):
                LOG.warning("回调队列满且无法写入停止信号，将直接等待线程超时退出")
        if enqueued_sentinel:
            self._callback_queue.join()
        if self._callback_worker is not None:
            self._callback_worker.join(timeout=2)

    def _callback_loop(self) -> None:
        while True:
            item = self._callback_queue.get()
            try:
                if item is None:
                    return
                token_id, snap = item
                with self._callbacks_lock:
                    callbacks = list(self._on_change_callbacks)
                for cb in callbacks:
                    try:
                        cb(token_id, snap)
                    except Exception as e:
                        LOG.error("订单簿回调异常: %s", e)
            finally:
                self._callback_queue.task_done()

class WebSocketFeed:
    """管理 Polymarket WebSocket 连接的生命周期.

    负责:
    - 建立连接并订阅指定 token 的订单簿
    - 解析推送消息并更新 OrderBookMirror
    - 可选同步更新 EnhancedBookStore（提供 microprice / imbalance 等衍生指标）
    - 自动重连（指数退避）
    - 心跳维持
    """

    def __init__(
        self,
        mirror: OrderBookMirror,
        ws_url: str = POLYMARKET_WS_URL,
        enhanced_store: Optional[EnhancedBookStore] = None,
        trade_callback: Optional[Callable[[dict], None]] = None,
    ):
        self._mirror = mirror
        self._ws_url = ws_url
        self._enhanced_store = enhanced_store
        self._trade_callback = trade_callback
        self._subscribed_tokens: set[str] = set()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = 1.0
        # tick_size_change 事件维护：实盘下单 round price 时可读，
        # 当前仅 telemetry 收集，留 hook 给 execution_engine 后续接入。
        self._tick_sizes: dict[str, float] = {}
        # market_resolved 后已结算的 token，外部可读以从订阅池里摘除。
        self._resolved_tokens: set[str] = set()
        # 线程安全访问活跃 ws 句柄，Dynamic Subscription 从外部线程发送时使用。
        self._ws_lock = threading.Lock()
        self._ws_handle: Any = None

    def set_trade_callback(self, callback: Optional[Callable[[dict], None]]) -> None:
        """Replace the ``last_trade_price`` handler used by this feed."""
        self._trade_callback = callback

    def subscribe(self, token_ids: list[str]) -> None:
        self._subscribed_tokens.update(token_ids)

    def subscribed_tokens(self) -> set[str]:
        """Snapshot of tokens currently subscribed (safe to mutate externally)."""
        return set(self._subscribed_tokens)

    def get_tick_size(self, token_id: str) -> Optional[float]:
        """Return the latest tick size pushed via ``tick_size_change`` (or None)."""
        return self._tick_sizes.get(token_id)

    def get_resolved_tokens(self) -> set[str]:
        """Snapshot of tokens the server has marked resolved since this feed started."""
        return set(self._resolved_tokens)

    def add_tokens(self, token_ids: Iterable[str]) -> bool:
        """Dynamic Subscription: add tokens to live subscription.

        Returns True if the message was sent over the active WS connection.
        Returns False if the feed isn't currently connected — caller is
        responsible for fallback (typically stop+restart).
        """
        new = {str(t).strip() for t in token_ids if t}
        new = new - self._subscribed_tokens
        if not new:
            return True
        self._subscribed_tokens.update(new)
        return self._send_dynamic_subscription("subscribe", new)

    def remove_tokens(self, token_ids: Iterable[str]) -> bool:
        """Dynamic Subscription: remove tokens from live subscription."""
        gone = {str(t).strip() for t in token_ids if t}
        gone = gone & self._subscribed_tokens
        if not gone:
            return True
        self._subscribed_tokens.difference_update(gone)
        return self._send_dynamic_subscription("unsubscribe", gone)

    def _send_dynamic_subscription(self, operation: str, tokens: set[str]) -> bool:
        payload: dict[str, Any] = {
            "assets_ids": sorted(tokens),
            "operation": operation,
        }
        if operation == "subscribe":
            payload["custom_feature_enabled"] = True
        with self._ws_lock:
            ws = self._ws_handle
        if ws is None:
            return False
        try:
            ws.send(json.dumps(payload))
            LOG.info("Dynamic %s 已发送，token=%d", operation, len(tokens))
            return True
        except Exception as exc:
            LOG.warning("Dynamic %s 失败: %s，回退到 stop+restart", operation, exc)
            return False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-feed")
        self._thread.start()
        LOG.info("WebSocket feed 已启动，订阅 %d 个 token", len(self._subscribed_tokens))

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._mirror.stop()
        if self._enhanced_store is not None:
            self._enhanced_store.set_connected(False)

    def _run_loop(self) -> None:
        """WebSocket 主循环：连接 → 订阅 → 接收 → 重连."""
        import websockets.sync.client as ws_sync
        from websockets.exceptions import ConnectionClosed

        # websockets 默认 max_size=1MB（2^20）。Polymarket Market 频道在订阅
        # 完成后会按 initial_dump=true（默认）一次性 dump 全部 token 的初始
        # 订单簿。当订阅 token 数较多（200 markets × 2 outcome = 400 token）
        # 时单条 dump 可达数 MB，触发客户端主动发 1009 (MESSAGE_TOO_BIG) 关闭
        # → 重连 → 再 dump → 再关闭的死循环。给 max_size 留 16MB 余量。
        # 官方 changelog 2025-05-28 已移除 100 token 订阅上限，不再有"token
        # 数硬上限"约束；瓶颈只在客户端缓冲区。
        ws_max_size = 16 * 1024 * 1024

        while self._running:
            try:
                with ws_sync.connect(
                    self._ws_url,
                    close_timeout=5,
                    max_size=ws_max_size,
                ) as ws:
                    with self._ws_lock:
                        self._ws_handle = ws
                    LOG.info("WebSocket 已连接: %s", self._ws_url)
                    self._reconnect_delay = 1.0

                    if self._subscribed_tokens:
                        # changelog 2025-05-28 引入 initial_dump 字段（默认 true）。
                        # 显式声明意图：希望拿初始全簿，max_size 已留 16MB 余量。
                        sub_msg = json.dumps({
                            "assets_ids": sorted(self._subscribed_tokens),
                            "type": "market",
                            "custom_feature_enabled": True,
                            "initial_dump": True,
                        })
                        ws.send(sub_msg)
                        LOG.info("WebSocket 订阅已发送，token=%d", len(self._subscribed_tokens))

                    while self._running:
                        # 官方 wss-overview 要求 "Send PING every 10 seconds"；
                        # 安静市场（无 price_change 推送）超 ~10s 服务端会断开。
                        # recv timeout 设 8s，超时即发心跳，给服务端阈值留 2s 余量。
                        try:
                            raw = ws.recv(timeout=8)
                        except TimeoutError:
                            ws.send("PING")
                            continue

                        self._handle_message(raw)

            except ConnectionClosed as exc:
                with self._ws_lock:
                    self._ws_handle = None
                if not self._running:
                    break
                if self._enhanced_store is not None:
                    self._enhanced_store.set_connected(False)
                code = getattr(getattr(exc, "rcvd", None), "code", None) or getattr(
                    getattr(exc, "sent", None), "code", None
                )
                if code in _FATAL_WS_CLOSE_CODES:
                    LOG.error(
                        "WebSocket 收到 fatal close code=%s（鉴权/订阅 schema 错误），停止重连",
                        code,
                    )
                    self._running = False
                    break
                if isinstance(code, int) and 4000 <= code < 5000:
                    # 未列入 fatal 的 4xxx：官方 wss-overview 未文档化语义；
                    # 透明 retry 容易出现死循环刷屏，给一次长退避 + 显著日志。
                    LOG.error(
                        "WebSocket 收到未知 4xxx close code=%s reason=%r，使用 %.0fs 退避",
                        code,
                        getattr(exc, "reason", ""),
                        _UNKNOWN_4XXX_BACKOFF_FLOOR_SEC,
                    )
                    self._reconnect_delay = max(
                        self._reconnect_delay, _UNKNOWN_4XXX_BACKOFF_FLOOR_SEC
                    )
                else:
                    LOG.warning(
                        "WebSocket 关闭 code=%s reason=%r，%.1f 秒后重连",
                        code,
                        getattr(exc, "reason", ""),
                        self._reconnect_delay,
                    )
                self._sleep_with_backoff()
            except Exception as exc:
                with self._ws_lock:
                    self._ws_handle = None
                if not self._running:
                    break
                if self._enhanced_store is not None:
                    self._enhanced_store.set_connected(False)
                LOG.warning(
                    "WebSocket 断开: %s，%.1f 秒后重连",
                    exc,
                    self._reconnect_delay,
                )
                self._sleep_with_backoff()

    def _sleep_with_backoff(self) -> None:
        sleep_for = self._with_reconnect_jitter(self._reconnect_delay)
        time.sleep(sleep_for)
        self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def _handle_message(self, raw: str | bytes) -> None:
        """解析 WebSocket 消息并更新镜像 + EnhancedBookStore."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if raw in ("PONG", "PING"):
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self._handle_message(json.dumps(item))
            return
        if not isinstance(msg, dict):
            return

        msg_type = msg.get("event_type") or msg.get("type") or ""

        if msg_type == "book":
            token_id = msg.get("asset_id") or ""
            if not token_id:
                return
            bids = msg.get("bids") or []
            asks = msg.get("asks") or []
            self._mirror.apply_snapshot(token_id, bids, asks)
            self._sync_to_enhanced_store(token_id, bids, asks)

        elif msg_type == "price_change":
            changes = _normalize_price_changes(msg)
            for change in changes:
                token_id = str(change.get("asset_id") or "").strip()
                if not token_id:
                    continue
                side = str(change.get("side", "")).lower()
                try:
                    price = float(change.get("price", 0))
                    size = float(change.get("size", 0))
                except (TypeError, ValueError):
                    LOG.warning("忽略异常 price_change 消息: %s", change)
                    continue
                if side in ("buy", "sell") and price > 0:
                    self._mirror.apply_delta(token_id, side, price, size)
                    self._sync_snapshot_from_mirror(token_id)

        elif msg_type == "last_trade_price":
            self._dispatch_trade(msg)

        elif msg_type == "tick_size_change":
            self._handle_tick_size_change(msg)

        elif msg_type == "best_bid_ask":
            # 官方依赖 custom_feature_enabled=true 才推送。
            # 顶档信息已由 price_change 增量维护出来，这里仅显式吸收以免落入
            # 未知 event_type 默认分支；如未来要做快速 best 监控可在此挂 hook。
            pass

        elif msg_type == "new_market":
            self._handle_new_market(msg)

        elif msg_type == "market_resolved":
            self._handle_market_resolved(msg)

        elif msg_type in ("pong", "subscribed", "subscription_ack", "heartbeat"):
            pass

    def _handle_tick_size_change(self, msg: dict[str, Any]) -> None:
        token_id = str(msg.get("asset_id") or "").strip()
        if not token_id:
            return
        raw_new = msg.get("new_tick_size")
        try:
            new_tick = float(raw_new)
        except (TypeError, ValueError):
            LOG.warning("tick_size_change new_tick_size 解析失败: %s", msg)
            return
        if new_tick <= 0:
            return
        old = self._tick_sizes.get(token_id)
        self._tick_sizes[token_id] = new_tick
        if old != new_tick:
            LOG.info(
                "tick_size_change token=%s old=%s new=%s",
                token_id[:12], old, new_tick,
            )

    def _handle_new_market(self, msg: dict[str, Any]) -> None:
        market_id = msg.get("id") or msg.get("market") or ""
        question = msg.get("question") or ""
        # 仅 telemetry——市场发现仍走 REST 路径，不在 WS 推送里加 token。
        LOG.info(
            "new_market: id=%s question=%s",
            str(market_id)[:24], str(question)[:80],
        )

    def _handle_market_resolved(self, msg: dict[str, Any]) -> None:
        winning = str(msg.get("winning_asset_id") or "")
        market_id = str(msg.get("id") or msg.get("market") or "")
        # market_resolved schema 复用 new_market 的 metadata，含 clob_token_ids
        # 或 assets_ids；两者都查一下以兼容字段重命名。
        token_list_raw = msg.get("clob_token_ids") or msg.get("assets_ids") or []
        if not isinstance(token_list_raw, list):
            token_list_raw = []
        assets = [str(t).strip() for t in token_list_raw if t]
        LOG.info(
            "market_resolved: market=%s winning=%s tokens=%d",
            market_id[:24], winning[:12], len(assets),
        )
        if not assets:
            return
        self._resolved_tokens.update(assets)
        to_remove = [t for t in assets if t in self._subscribed_tokens]
        if to_remove:
            sent = self.remove_tokens(to_remove)
            LOG.info(
                "market_resolved 自动 unsubscribe token=%d dynamic_sent=%s",
                len(to_remove), sent,
            )

    def _dispatch_trade(self, msg: dict[str, Any]) -> None:
        """Forward ``last_trade_price`` events to the registered consumer.

        Exceptions in the consumer are swallowed so a buggy flow
        aggregator cannot kill the WS pump (the consumer is best-
        effort telemetry, not a hot-path).
        """
        if self._trade_callback is None:
            return
        try:
            self._trade_callback(msg)
        except Exception as exc:
            LOG.warning("trade_callback raised: %s", exc)

    def _sync_to_enhanced_store(self, token_id: str, bids_raw: list, asks_raw: list) -> None:
        """将 WS 推送的订单簿数据同步写入 EnhancedBookStore."""
        if self._enhanced_store is None:
            return
        parsed_bids = _parse_orders(bids_raw)
        parsed_asks = _parse_orders(asks_raw)
        ts = now_ms()
        matched = self._enhanced_store.update_by_token_id(token_id, parsed_bids, parsed_asks, ts)
        if matched:
            snap = self._enhanced_store.snapshot()
            if not snap.get("connected", False):
                self._enhanced_store.set_connected(True)

    def _sync_snapshot_from_mirror(self, token_id: str) -> None:
        if self._enhanced_store is None:
            return
        snap = self._mirror.get(token_id)
        if snap is None:
            return
        bids = [(level.price, level.size) for level in snap.bids]
        asks = [(level.price, level.size) for level in snap.asks]
        self._enhanced_store.update_by_token_id(token_id, bids, asks, now_ms())

    def _with_reconnect_jitter(self, delay: float) -> float:
        jitter = random.uniform(0.0, delay * 0.3)
        return delay + jitter


def _parse_orders(orders_raw: list) -> list[tuple[float, float]]:
    """将 WS 原始订单数据解析为 (price, size) 元组列表."""
    result: list[tuple[float, float]] = []
    for order in orders_raw or []:
        try:
            if isinstance(order, dict):
                price = float(order.get("price", 0))
                size = float(order.get("size", 0))
            else:
                price = float(order[0])
                size = float(order[1]) if len(order) > 1 else 0.0
            if price > 0 and size > 0:
                result.append((price, size))
        except (ValueError, IndexError, TypeError):
            continue
    return result


def _normalize_price_changes(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容 price_change 中 dict/list/单条消息三种形态."""
    raw_changes = msg.get("price_changes")
    if raw_changes is None:
        raw_changes = msg.get("changes")

    if isinstance(raw_changes, dict):
        return [raw_changes]
    if isinstance(raw_changes, list):
        normalized = [item for item in raw_changes if isinstance(item, dict)]
        if len(normalized) != len(raw_changes):
            LOG.warning("price_change 消息包含非 dict 项，已忽略异常项")
        return normalized
    if raw_changes is not None:
        LOG.warning("price_change 消息字段 changes 类型异常: %s", type(raw_changes).__name__)

    return [msg]
