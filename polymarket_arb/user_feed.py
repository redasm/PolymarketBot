"""Polymarket CLOB **user 频道** WebSocket：订单与成交的即时推送.

和 `websocket_feed.py`（market 频道，推订单簿）互补：user 频道推的是
**自己的**订单状态和成交。

为什么需要它:

在只有 market 频道的时候，成交要靠 `ExecutionEngine.sync_pending_trade_statuses`
每个扫描周期对每个挂单做一次 REST `get_order`。这有三个后果:

1. 延迟等于一个扫描周期（默认 5s）加上 N 次串行 REST 往返；
2. 挂单越多越慢，正好在做市铺得最开、最需要及时知道成交的时候最慢；
3. 敞口和持仓在这段窗口里是过期的 —— `RISK_MAX_OPEN_POSITIONS=1`
   的 canary 配置下，一次没被及时观测到的成交就能把机器人卡住。

**并发契约（重要）**：本模块的后台线程**只做解析和入队**，绝不碰
RiskManager / ExitManager / MakerStrategy 的任何状态。主循环每个周期
`drain()` 出事件，在主线程上走和轮询完全相同的对账路径。这样既拿到了
推送的低延迟，又不引入跨线程的风险状态竞争。收到事件时顺带 set 一下
`wake_event`，主循环就能立刻醒来而不是睡满一个周期。
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

LOG = logging.getLogger(__name__)

POLYMARKET_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
_PING_INTERVAL_SEC = 10.0
_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_MAX_SEC = 60.0
_DEFAULT_QUEUE_SIZE = 2000
# 去重环的容量。user 频道会在重连后重放，(order_id, trade_id) 组合
# 用来保证增量成交只被计一次。
_DEDUP_RING_SIZE = 4096
# 订阅集合变化后最短的重连间隔，避免市场池抖动时反复重连。
_RESUBSCRIBE_MIN_INTERVAL_SEC = 15.0
_MARKET_CHECK_INTERVAL_SEC = 5.0

_CANCEL_STATUSES = {"CANCELED", "CANCELLED", "UNMATCHED", "EXPIRED"}
_FAILED_STATUSES = {"FAILED", "REJECTED"}


@dataclass(frozen=True)
class UserOrderEvent:
    """user 频道事件的归一化形态.

    `cumulative_matched` 来自 order 消息的 `size_matched`（该订单累计
    成交量，权威值）；`incremental_matched` 来自 trade 消息里
    `maker_orders[].matched_amount`（**本次**成交量）。两者语义不同，
    刻意分开存 —— 混用会在重放时重复计数。
    """

    order_id: str
    event_type: str  # PLACEMENT / UPDATE / CANCELLATION / TRADE
    token_id: str = ""
    condition_id: str = ""
    side: str = ""
    price: float = 0.0
    original_size: float = 0.0
    cumulative_matched: Optional[float] = None
    incremental_matched: Optional[float] = None
    status: str = ""
    dedup_key: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def is_cancel(self) -> bool:
        return self.event_type == "CANCELLATION" or self.status.upper() in _CANCEL_STATUSES

    @property
    def is_failure(self) -> bool:
        return self.status.upper() in _FAILED_STATUSES


def _f(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out:  # NaN
        return 0.0
    return out


def _ts(value: Any) -> float:
    raw = _f(value)
    if raw <= 0:
        return time.time()
    # user 频道混用秒和毫秒。
    return raw / 1000.0 if raw > 1e11 else raw


def parse_user_messages(raw: str) -> list[dict]:
    """一帧可能是单个对象，也可能是对象数组."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def normalize_user_message(msg: dict) -> list[UserOrderEvent]:
    """把一条 user 频道消息拆成 0..N 个归一化事件."""
    event_type = str(msg.get("event_type") or "").lower()
    top_type = str(msg.get("type") or "").upper()

    if event_type == "trade" or top_type == "TRADE":
        return _normalize_trade(msg)
    if event_type == "order" or top_type in ("PLACEMENT", "UPDATE", "CANCELLATION"):
        return _normalize_order(msg)
    return []


def _normalize_order(msg: dict) -> list[UserOrderEvent]:
    order_id = str(msg.get("id") or msg.get("order_id") or msg.get("orderID") or "")
    if not order_id:
        return []
    event_type = str(msg.get("type") or "UPDATE").upper()
    size_matched = msg.get("size_matched")
    ts = _ts(msg.get("timestamp") or msg.get("created_at"))
    return [
        UserOrderEvent(
            order_id=order_id,
            event_type=event_type,
            token_id=str(msg.get("asset_id") or ""),
            condition_id=str(msg.get("market") or msg.get("condition_id") or ""),
            side=str(msg.get("side") or "").upper(),
            price=_f(msg.get("price")),
            original_size=_f(msg.get("original_size") or msg.get("size")),
            cumulative_matched=_f(size_matched) if size_matched is not None else None,
            status=str(msg.get("status") or "").upper(),
            # order 消息带累计量，天然幂等，用 (order, 累计量, 类型) 去重。
            dedup_key=f"order:{order_id}:{event_type}:{_f(size_matched)}",
            ts=ts,
        )
    ]


def _normalize_trade(msg: dict) -> list[UserOrderEvent]:
    """一条 trade 消息可能同时命中我方多个挂单（maker_orders）."""
    trade_id = str(msg.get("id") or msg.get("trade_id") or "")
    status = str(msg.get("status") or "").upper()
    ts = _ts(msg.get("timestamp") or msg.get("match_time") or msg.get("last_update"))
    asset_id = str(msg.get("asset_id") or "")
    condition_id = str(msg.get("market") or msg.get("condition_id") or "")
    side = str(msg.get("side") or "").upper()

    events: list[UserOrderEvent] = []
    for maker in msg.get("maker_orders") or []:
        if not isinstance(maker, dict):
            continue
        maker_id = str(maker.get("order_id") or maker.get("id") or "")
        if not maker_id:
            continue
        events.append(
            UserOrderEvent(
                order_id=maker_id,
                event_type="TRADE",
                token_id=str(maker.get("asset_id") or asset_id),
                condition_id=condition_id,
                side=str(maker.get("side") or side).upper(),
                price=_f(maker.get("price") or msg.get("price")),
                original_size=_f(maker.get("original_size")),
                incremental_matched=_f(maker.get("matched_amount")),
                status=status,
                dedup_key=f"trade:{trade_id}:{maker_id}",
                ts=ts,
            )
        )

    taker_id = str(msg.get("taker_order_id") or "")
    if taker_id:
        events.append(
            UserOrderEvent(
                order_id=taker_id,
                event_type="TRADE",
                token_id=asset_id,
                condition_id=condition_id,
                side=side,
                price=_f(msg.get("price")),
                incremental_matched=_f(msg.get("size")),
                status=status,
                dedup_key=f"trade:{trade_id}:taker:{taker_id}",
                ts=ts,
            )
        )
    return events


class UserChannelFeed:
    """user 频道的后台线程 + 有界事件队列.

    线程只负责「连接 → 订阅 → 解析 → 入队」，不做任何业务状态变更。
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        market_provider: Callable[[], Iterable[str]] | None = None,
        ws_url: str = POLYMARKET_USER_WS_URL,
        wake_event: threading.Event | None = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""
        self._api_passphrase = api_passphrase or ""
        self._market_provider = market_provider
        self._ws_url = ws_url
        self._wake_event = wake_event
        self._queue: collections.deque[UserOrderEvent] = collections.deque(
            maxlen=max(1, int(queue_size))
        )
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._ws_handle: Any = None
        self._stats = {
            "events": 0,
            "dropped": 0,
            "duplicates": 0,
            "reconnects": 0,
            "parse_errors": 0,
        }

    # ------- 凭证 -------

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret and self._api_passphrase)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------- 生命周期 -------

    def start(self) -> bool:
        if self._running:
            return True
        if not self.has_credentials:
            LOG.warning("user 频道缺少 API 凭证（key/secret/passphrase），未启动")
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-user-feed"
        )
        self._thread.start()
        LOG.info("user 频道 WebSocket 已启动")
        return True

    def stop(self) -> None:
        self._running = False
        with self._lock:
            ws = self._ws_handle
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._connected = False

    # ------- 消费端（主线程） -------

    def drain(self, max_items: int = 500) -> list[UserOrderEvent]:
        """取出待处理事件（FIFO）。只应由主循环调用."""
        out: list[UserOrderEvent] = []
        limit = max(1, int(max_items))
        with self._lock:
            while self._queue and len(out) < limit:
                out.append(self._queue.popleft())
        return out

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._stats)
        out["connected"] = self._connected
        out["pending"] = self.pending_count()
        return out

    # ------- 生产端（后台线程） -------

    def _offer(self, event: UserOrderEvent) -> None:
        with self._lock:
            if event.dedup_key:
                if event.dedup_key in self._seen:
                    self._stats["duplicates"] += 1
                    return
                self._seen[event.dedup_key] = None
                while len(self._seen) > _DEDUP_RING_SIZE:
                    self._seen.popitem(last=False)
            if len(self._queue) == self._queue.maxlen:
                # 有界队列：丢最老的。丢弃是可观测的（stats.dropped），
                # 而且轮询对账仍然兜底，所以不会丢失最终一致性。
                self._stats["dropped"] += 1
            self._queue.append(event)
            self._stats["events"] += 1
        if self._wake_event is not None:
            self._wake_event.set()

    def _current_markets(self) -> list[str]:
        if self._market_provider is None:
            return []
        try:
            return sorted(
                {str(m).strip() for m in self._market_provider() if str(m).strip()}
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("user 频道 market_provider 失败: %s", exc)
            return []

    def _subscription_payload(self, markets: list[str]) -> dict[str, Any]:
        return {
            "type": "user",
            "auth": {
                "apiKey": self._api_key,
                "secret": self._api_secret,
                "passphrase": self._api_passphrase,
            },
            "markets": markets,
        }

    def _handle_frame(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            return
        stripped = raw.strip()
        if not stripped or stripped.upper() in ("PING", "PONG"):
            return
        for msg in parse_user_messages(stripped):
            try:
                for event in normalize_user_message(msg):
                    self._offer(event)
            except Exception as exc:  # noqa: BLE001 - 单条坏消息不能断流
                with self._lock:
                    self._stats["parse_errors"] += 1
                LOG.debug("user 频道消息解析失败: %s", exc)

    def _run_loop(self) -> None:
        import websockets.sync.client as ws_sync

        backoff = _BACKOFF_INITIAL_SEC
        while self._running:
            try:
                markets = self._current_markets()
                with ws_sync.connect(
                    self._ws_url, close_timeout=5, max_size=4 * 1024 * 1024
                ) as ws:
                    with self._lock:
                        self._ws_handle = ws
                    ws.send(json.dumps(self._subscription_payload(markets)))
                    self._connected = True
                    connected_at = time.time()
                    backoff = _BACKOFF_INITIAL_SEC
                    LOG.info("user 频道已连接并订阅，market=%d", len(markets))
                    subscribed = markets
                    last_ping = time.time()
                    last_market_check = time.time()
                    while self._running:
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            raw = None
                        if raw is not None:
                            self._handle_frame(raw)
                        now = time.time()
                        if now - last_ping >= _PING_INTERVAL_SEC:
                            ws.send("PING")
                            last_ping = now
                        # 订阅集合是连接时固定的，市场池换了就必须重连，
                        # 否则新市场上的成交永远推不过来。
                        if now - last_market_check >= _MARKET_CHECK_INTERVAL_SEC:
                            last_market_check = now
                            if (
                                now - connected_at >= _RESUBSCRIBE_MIN_INTERVAL_SEC
                                and self._current_markets() != subscribed
                            ):
                                LOG.info("user 频道订阅集合变化，重连以重新订阅")
                                break
            except Exception as exc:  # noqa: BLE001 - 断线是常态，重连即可
                if self._running:
                    with self._lock:
                        self._stats["reconnects"] += 1
                    LOG.warning(
                        "user 频道断开: %s，%.1f 秒后重连", exc, backoff
                    )
            finally:
                self._connected = False
                with self._lock:
                    self._ws_handle = None
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(_BACKOFF_MAX_SEC, backoff * 1.5)
