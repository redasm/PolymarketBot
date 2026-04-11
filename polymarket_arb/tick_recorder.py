"""Tick 录制器：将 WS 推送的订单簿快照写入 NDJSON 文件，供回测和回归测试使用.

设计原则:
  - 零侵入：通过 OrderBookMirror.register_callback() 接入，不改动 WS 逻辑
  - 低开销：异步写入、自动按日期滚动文件
  - NDJSON 格式：每行一个 JSON 对象，可用 pandas / duckdb / jq 直接查询

录制格式（每行）:
{
  "ts_ms": 1712345678000,
  "token_id": "0xabc...def",
  "event_type": "book",
  "best_bid": 0.52,
  "best_ask": 0.54,
  "bid_depth_5": 12500.0,
  "ask_depth_5": 8700.0,
  "imbalance_5": 0.18,
  "microprice": 0.5285,
  "spread_bps": 377.4,
  "bids_top3": [[0.52, 5000], [0.51, 4500], [0.50, 3000]],
  "asks_top3": [[0.54, 3200], [0.55, 2800], [0.56, 2700]]
}

使用方式:

  from polymarket_arb.tick_recorder import TickRecorder

  recorder = TickRecorder(output_dir="data/ticks", enabled=True)
  mirror.register_callback(recorder.on_book_update)
  # ... 运行 WS ...
  recorder.close()

回测时读取:

  import json
  with open("data/ticks/2026-04-11.ndjson") as f:
      for line in f:
          tick = json.loads(line)
          # feed to replay engine

环境变量:
  TICK_RECORD_ENABLED=true   开启录制
  TICK_RECORD_DIR=data/ticks 输出目录
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from polymarket_arb.models import OrderBookSnapshot

LOG = logging.getLogger(__name__)


class TickRecorder:
    """将订单簿快照写入 NDJSON 文件.

    Args:
        output_dir: 输出目录（自动创建）
        enabled: 是否启用录制
        max_file_size_mb: 单文件大小上限，超过后滚动（默认 200MB）
    """

    def __init__(
        self,
        output_dir: str = "data/ticks",
        enabled: bool = False,
        max_file_size_mb: float = 200.0,
    ) -> None:
        self._enabled = enabled
        self._output_dir = Path(output_dir)
        self._max_bytes = int(max_file_size_mb * 1024 * 1024)
        self._lock = threading.Lock()
        self._current_date: Optional[str] = None
        self._file = None
        self._bytes_written = 0
        self._tick_count = 0

        if self._enabled:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            LOG.info("tick_recorder enabled, output_dir=%s", self._output_dir)

    def on_book_update(self, token_id: str, snap: OrderBookSnapshot) -> None:
        """注册为 OrderBookMirror 的回调，每次订单簿更新时调用."""
        if not self._enabled:
            return

        ts_ms = int(snap.timestamp * 1000)
        bids_top3 = [[lv.price, lv.size] for lv in snap.bids[:3]]
        asks_top3 = [[lv.price, lv.size] for lv in snap.asks[:3]]

        bid_depth_5 = sum(lv.size for lv in snap.bids[:5])
        ask_depth_5 = sum(lv.size for lv in snap.asks[:5])
        total_depth = bid_depth_5 + ask_depth_5
        imbalance = (bid_depth_5 - ask_depth_5) / total_depth if total_depth > 0 else 0

        microprice = None
        if snap.best_bid is not None and snap.best_ask is not None:
            bid_sz = snap.bids[0].size if snap.bids else 0
            ask_sz = snap.asks[0].size if snap.asks else 0
            total_sz = bid_sz + ask_sz
            if total_sz > 0:
                microprice = round(
                    (snap.best_ask * bid_sz + snap.best_bid * ask_sz) / total_sz, 6
                )

        mid = snap.mid
        spread_bps = None
        if mid is not None and snap.spread is not None and mid > 0:
            spread_bps = round((snap.spread / mid) * 10_000, 1)

        record = {
            "ts_ms": ts_ms,
            "token_id": token_id,
            "event_type": "book",
            "best_bid": snap.best_bid,
            "best_ask": snap.best_ask,
            "bid_depth_5": round(bid_depth_5, 2),
            "ask_depth_5": round(ask_depth_5, 2),
            "imbalance_5": round(imbalance, 4),
            "microprice": microprice,
            "spread_bps": spread_bps,
            "bids_top3": bids_top3,
            "asks_top3": asks_top3,
        }

        self._write(record)

    def _write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        line_bytes = line.encode("utf-8")

        with self._lock:
            self._ensure_file()
            if self._file is not None:
                self._file.write(line_bytes)
                self._bytes_written += len(line_bytes)
                self._tick_count += 1

                if self._bytes_written >= self._max_bytes:
                    self._rotate()

    def _ensure_file(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._current_date != today or self._file is None:
            self._close_file()
            self._current_date = today
            path = self._output_dir / f"{today}.ndjson"
            self._file = open(path, "ab")
            self._bytes_written = path.stat().st_size if path.exists() else 0
            LOG.info("tick_recorder opened %s", path)

    def _rotate(self) -> None:
        self._close_file()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        seq = 1
        while True:
            path = self._output_dir / f"{today}.{seq}.ndjson"
            if not path.exists():
                break
            seq += 1
        self._file = open(path, "ab")
        self._bytes_written = 0
        self._current_date = today
        LOG.info("tick_recorder rotated to %s", path)

    def _close_file(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None

    def close(self) -> None:
        with self._lock:
            self._close_file()
        LOG.info("tick_recorder closed, total ticks recorded: %d", self._tick_count)

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def is_enabled(self) -> bool:
        return self._enabled
