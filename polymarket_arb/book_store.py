"""增强订单簿存储：线程安全的 YES/NO 双侧订单簿，提供 microprice / imbalance / depth 等衍生指标.

移植自 mlmodelpoly 的 PolymarketBookStore，适配本项目的通用市场结构。
与 websocket_feed.OrderBookMirror 相比，本模块:
  - 内置 microprice（成交量加权中间价，比 mid 更精确）
  - 内置多层 imbalance（买卖压力指标，可直接输入统计模型）
  - 内置 spread_bps（基点单位的价差，方便跨市场比较）
  - 线程安全的原子快照
  - 连接状态追踪 + 数据新鲜度检查
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from polymarket_arb.utils_time import now_ms

LOG = logging.getLogger(__name__)


@dataclass
class OrderbookSide:
    """单侧订单簿（YES 或 NO token），提供丰富的衍生指标."""

    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    ts_ms: int = 0

    def update(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], ts_ms: int) -> None:
        self.bids = sorted(bids, key=lambda x: x[0], reverse=True)
        self.asks = sorted(asks, key=lambda x: x[0])
        self.ts_ms = ts_ms

    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    def best_bid_size(self) -> Optional[float]:
        return self.bids[0][1] if self.bids else None

    def best_ask_size(self) -> Optional[float]:
        return self.asks[0][1] if self.asks else None

    def mid(self) -> Optional[float]:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return None

    def spread(self) -> Optional[float]:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is not None and ask is not None:
            return ask - bid
        return None

    def spread_bps(self) -> Optional[float]:
        m, s = self.mid(), self.spread()
        if m is not None and s is not None and m > 0:
            return (s / m) * 10_000
        return None

    def bid_depth_top_n(self, n: int = 5) -> float:
        return sum(sz for _, sz in self.bids[:n])

    def ask_depth_top_n(self, n: int = 5) -> float:
        return sum(sz for _, sz in self.asks[:n])

    def imbalance(self, levels: int = 5) -> Optional[float]:
        """订单簿不平衡：(bid_depth - ask_depth) / total.

        Returns:
            -1..1 范围，>0 表示买压，<0 表示卖压。
        """
        bd = self.bid_depth_top_n(levels)
        ad = self.ask_depth_top_n(levels)
        total = bd + ad
        return (bd - ad) / total if total > 0 else None

    def microprice(self) -> Optional[float]:
        """成交量加权中间价：更精确地反映短期方向.

        Formula: (ask_px × bid_sz + bid_px × ask_sz) / (bid_sz + ask_sz)
        """
        bid_px, ask_px = self.best_bid(), self.best_ask()
        bid_sz, ask_sz = self.best_bid_size(), self.best_ask_size()
        if all(v is not None for v in (bid_px, ask_px, bid_sz, ask_sz)):
            total = bid_sz + ask_sz
            if total > 0:
                return (ask_px * bid_sz + bid_px * ask_sz) / total
        return None

    def snapshot(self) -> dict:
        return {
            "best_bid": self.best_bid(),
            "best_ask": self.best_ask(),
            "best_bid_size": self.best_bid_size(),
            "best_ask_size": self.best_ask_size(),
            "mid": self.mid(),
            "microprice": self.microprice(),
            "spread": self.spread(),
            "spread_bps": self.spread_bps(),
            "imbalance": self.imbalance(5),
            "bid_depth_top5": self.bid_depth_top_n(5),
            "ask_depth_top5": self.ask_depth_top_n(5),
            "ts_ms": self.ts_ms,
        }


class EnhancedBookStore:
    """线程安全的双侧订单簿存储.

    可以为任意市场维护 YES/NO（或 outcome_0 / outcome_1）两侧订单簿。
    下游消费者通过 snapshot() 获取原子快照。

    Args:
        market_id: 可选的市场标识，连接后由 set_market() 设置。
    """

    def __init__(self, market_id: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._yes = OrderbookSide()
        self._no = OrderbookSide()
        self._market_id = market_id
        self._yes_token_id: Optional[str] = None
        self._no_token_id: Optional[str] = None
        self._connected = False
        self._last_update_ms = 0

    def set_market(self, market_id: str, yes_token_id: str, no_token_id: str) -> None:
        with self._lock:
            self._market_id = market_id
            self._yes_token_id = yes_token_id
            self._no_token_id = no_token_id
            self._yes = OrderbookSide()
            self._no = OrderbookSide()
        LOG.info("book_store market set: %s", market_id)

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def update_yes(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], ts_ms: int) -> None:
        with self._lock:
            self._yes.update(bids, asks, ts_ms)
            self._last_update_ms = ts_ms

    def update_no(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], ts_ms: int) -> None:
        with self._lock:
            self._no.update(bids, asks, ts_ms)
            self._last_update_ms = ts_ms

    def update_by_token_id(
        self,
        token_id: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        ts_ms: int,
    ) -> bool:
        """按 token ID 路由更新到 YES 或 NO 侧。

        Returns:
            True 如果 token_id 匹配当前市场。
        """
        if token_id == self._yes_token_id:
            self.update_yes(bids, asks, ts_ms)
            return True
        if token_id == self._no_token_id:
            self.update_no(bids, asks, ts_ms)
            return True
        return False

    def snapshot(self) -> dict:
        with self._lock:
            ts = now_ms()
            age_sec = (ts - self._last_update_ms) / 1000.0 if self._last_update_ms > 0 else None
            return {
                "market_id": self._market_id,
                "yes": self._yes.snapshot(),
                "no": self._no.snapshot(),
                "connected": self._connected,
                "ts_ms": self._last_update_ms,
                "age_sec": round(age_sec, 1) if age_sec is not None else None,
            }

    def get_yes_mid(self) -> Optional[float]:
        with self._lock:
            return self._yes.mid()

    def get_no_mid(self) -> Optional[float]:
        with self._lock:
            return self._no.mid()

    def get_yes_microprice(self) -> Optional[float]:
        with self._lock:
            return self._yes.microprice()

    def get_no_microprice(self) -> Optional[float]:
        with self._lock:
            return self._no.microprice()

    def get_yes_imbalance(self, levels: int = 5) -> Optional[float]:
        with self._lock:
            return self._yes.imbalance(levels)

    def get_no_imbalance(self, levels: int = 5) -> Optional[float]:
        with self._lock:
            return self._no.imbalance(levels)

    def is_ready(self) -> bool:
        with self._lock:
            if not self._connected:
                return False
            if self._yes.ts_ms == 0 or self._no.ts_ms == 0:
                return False
            ts = now_ms()
            return (ts - self._yes.ts_ms) < 10_000 and (ts - self._no.ts_ms) < 10_000

    def get_summary(self) -> dict:
        with self._lock:
            return {
                "market_id": self._market_id,
                "connected": self._connected,
                "yes_mid": self._yes.mid(),
                "no_mid": self._no.mid(),
                "yes_spread_bps": self._yes.spread_bps(),
                "no_spread_bps": self._no.spread_bps(),
            }
