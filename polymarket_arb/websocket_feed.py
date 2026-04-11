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
import threading
import time
from typing import Any, Callable, Optional

from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.models import OrderBookLevel, OrderBookSnapshot
from polymarket_arb.utils_time import now_ms

LOG = logging.getLogger(__name__)

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class OrderBookMirror:
    """本地订单簿镜像：由 WebSocket 增量更新维护.

    维护每个 token_id 的完整订单簿状态，
    当 best bid/ask 变动时触发回调。
    """

    def __init__(self) -> None:
        self._books: dict[str, OrderBookSnapshot] = {}
        self._lock = threading.Lock()
        self._on_change_callbacks: list[Callable[[str, OrderBookSnapshot], None]] = []

    def register_callback(self, cb: Callable[[str, OrderBookSnapshot], None]) -> None:
        self._on_change_callbacks.append(cb)

    def get(self, token_id: str) -> Optional[OrderBookSnapshot]:
        with self._lock:
            return self._books.get(token_id)

    def get_all(self) -> dict[str, OrderBookSnapshot]:
        with self._lock:
            return dict(self._books)

    def apply_snapshot(self, token_id: str, bids: list[dict], asks: list[dict], tick_size: float = 0.01) -> None:
        """应用完整订单簿快照（初始连接或重连后）."""
        parsed_bids = sorted(
            [OrderBookLevel(float(b["price"]), float(b["size"])) for b in bids if float(b.get("size", 0)) > 0],
            key=lambda x: x.price,
            reverse=True,
        )
        parsed_asks = sorted(
            [OrderBookLevel(float(a["price"]), float(a["size"])) for a in asks if float(a.get("size", 0)) > 0],
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

        self._fire_callbacks(token_id, snap)

    def apply_delta(self, token_id: str, side: str, price: float, new_size: float) -> None:
        """应用单个价位的增量更新.

        side: "buy" 或 "sell"
        new_size: 0 表示该价位已消失
        """
        with self._lock:
            snap = self._books.get(token_id)
            if snap is None:
                return

            if side == "buy":
                levels = list(snap.bids)
                levels = [lv for lv in levels if abs(lv.price - price) > 1e-9]
                if new_size > 0:
                    levels.append(OrderBookLevel(price, new_size))
                levels.sort(key=lambda x: x.price, reverse=True)
                new_snap = OrderBookSnapshot(
                    token_id=token_id,
                    best_bid=levels[0].price if levels else None,
                    best_ask=snap.best_ask,
                    tick_size=snap.tick_size,
                    bids=levels,
                    asks=list(snap.asks),
                    timestamp=time.time(),
                )
            else:
                levels = list(snap.asks)
                levels = [lv for lv in levels if abs(lv.price - price) > 1e-9]
                if new_size > 0:
                    levels.append(OrderBookLevel(price, new_size))
                levels.sort(key=lambda x: x.price)
                new_snap = OrderBookSnapshot(
                    token_id=token_id,
                    best_bid=snap.best_bid,
                    best_ask=levels[0].price if levels else None,
                    tick_size=snap.tick_size,
                    bids=list(snap.bids),
                    asks=levels,
                    timestamp=time.time(),
                )

            old_bb = snap.best_bid
            old_ba = snap.best_ask
            self._books[token_id] = new_snap

        if new_snap.best_bid != old_bb or new_snap.best_ask != old_ba:
            self._fire_callbacks(token_id, new_snap)

    def _fire_callbacks(self, token_id: str, snap: OrderBookSnapshot) -> None:
        for cb in self._on_change_callbacks:
            try:
                cb(token_id, snap)
            except Exception as e:
                LOG.error("订单簿回调异常: %s", e)


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
    ):
        self._mirror = mirror
        self._ws_url = ws_url
        self._enhanced_store = enhanced_store
        self._subscribed_tokens: set[str] = set()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = 1.0

    def subscribe(self, token_ids: list[str]) -> None:
        self._subscribed_tokens.update(token_ids)

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

    def _run_loop(self) -> None:
        """WebSocket 主循环：连接 → 订阅 → 接收 → 重连."""
        import websockets.sync.client as ws_sync

        while self._running:
            try:
                with ws_sync.connect(self._ws_url, close_timeout=5) as ws:
                    LOG.info("WebSocket 已连接: %s", self._ws_url)
                    self._reconnect_delay = 1.0

                    for token_id in self._subscribed_tokens:
                        sub_msg = json.dumps({
                            "type": "subscribe",
                            "channel": "market",
                            "assets_ids": [token_id],
                        })
                        ws.send(sub_msg)

                    while self._running:
                        try:
                            raw = ws.recv(timeout=30)
                        except TimeoutError:
                            ws.send(json.dumps({"type": "ping"}))
                            continue

                        self._handle_message(raw)

            except Exception as e:
                if not self._running:
                    break
                LOG.warning(
                    "WebSocket 断开: %s，%.1f 秒后重连",
                    e,
                    self._reconnect_delay,
                )
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def _handle_message(self, raw: str | bytes) -> None:
        """解析 WebSocket 消息并更新镜像 + EnhancedBookStore."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type") or msg.get("event_type") or ""

        if msg_type == "book":
            token_id = msg.get("asset_id") or ""
            if not token_id:
                return
            bids = msg.get("bids") or []
            asks = msg.get("asks") or []
            self._mirror.apply_snapshot(token_id, bids, asks)
            self._sync_to_enhanced_store(token_id, bids, asks)

        elif msg_type == "price_change":
            changes = msg.get("changes") or [msg]
            for change in changes:
                token_id = change.get("asset_id") or ""
                if not token_id:
                    continue
                side = change.get("side", "").lower()
                price = float(change.get("price", 0))
                size = float(change.get("size", 0))
                if side in ("buy", "sell") and price > 0:
                    self._mirror.apply_delta(token_id, side, price, size)

        elif msg_type in ("pong", "subscribed", "heartbeat"):
            pass

    def _sync_to_enhanced_store(self, token_id: str, bids_raw: list, asks_raw: list) -> None:
        """将 WS 推送的订单簿数据同步写入 EnhancedBookStore."""
        if self._enhanced_store is None:
            return
        parsed_bids = _parse_orders(bids_raw)
        parsed_asks = _parse_orders(asks_raw)
        ts = now_ms()
        matched = self._enhanced_store.update_by_token_id(token_id, parsed_bids, parsed_asks, ts)
        if matched and not self._enhanced_store._connected:
            self._enhanced_store.set_connected(True)


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
