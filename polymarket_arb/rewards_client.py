"""CLOB 流动性奖励元数据（只读）.

Polymarket 对挂在中间价附近的限价单发放流动性奖励。奖励带的半宽由
市场自身的 ``rewards_max_spread`` 决定（单位是 **cent**），落在
``[mid - δ, mid + δ]`` 内且规模 ≥ ``rewards_min_size`` 的挂单才计分:

    δ(price space) = rewards_max_spread(cents) * 0.01

数据源: ``GET {clob_host}/rewards/markets/{condition_id}``，返回
``{"data": [{... "rewards_max_spread": 3.0, "rewards_min_size": 50, ...}]}``。

为什么单独成模块而不是塞进 market_scanner:

1. 奖励参数变化频率远低于订单簿（按天计），适合独立 TTL 缓存；
2. T3 报价路径需要在**每个扫描周期**读到它，但绝不能因为这个接口
   超时/5xx 就阻塞报价 —— 所有失败都降级成"该市场无奖励带"，
   即 δ=0，maker_strategy 退回纯 fair-value 报价（历史行为）;
3. 负缓存（失败/无数据）独立 TTL，避免对无奖励市场每周期重试。

本模块**不做任何写操作**，也不参与下单。
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 5.0
DEFAULT_TTL_SEC = 900.0
DEFAULT_NEGATIVE_TTL_SEC = 300.0

# rewards_max_spread 以 cent 计价，价格空间是 0-1，故 1 cent = 0.01。
_CENTS_TO_PRICE = 0.01

# 防御性上限：δ 超过半个价格区间说明上游返回了异常量纲（例如把
# bps 当 cent 返回），此时按"无奖励带"处理而不是给出荒谬的报价约束。
_MAX_SANE_DELTA = 0.25


@dataclass(frozen=True)
class RewardsConfig:
    """单个市场的流动性奖励参数快照."""

    condition_id: str
    rewards_max_spread: float = 0.0  # cents
    rewards_min_size: float = 0.0  # shares
    daily_rate_usdc: float = 0.0
    fetched_at: float = field(default_factory=time.time)

    @property
    def reward_delta(self) -> float:
        """奖励带半宽（价格空间 0-1）。异常量纲一律返回 0."""
        delta = max(0.0, float(self.rewards_max_spread)) * _CENTS_TO_PRICE
        if delta > _MAX_SANE_DELTA:
            return 0.0
        return delta

    @property
    def is_incentivized(self) -> bool:
        return self.reward_delta > 0.0


def parse_rewards_payload(condition_id: str, payload: Any) -> Optional[RewardsConfig]:
    """把 /rewards/markets 响应规整成 RewardsConfig.

    上游返回过 ``{"data": [...]}`` / 裸 list / 裸 dict 三种形态，这里
    统一处理。解析不出有效行返回 ``None``（调用方按无奖励带处理）。
    """
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            rows = [row for row in data if isinstance(row, dict)]
        elif isinstance(data, dict):
            rows = [data]
        elif "rewards_max_spread" in payload or "rewards_min_size" in payload:
            rows = [payload]
    elif isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]

    if not rows:
        return None

    row = rows[0]
    return RewardsConfig(
        condition_id=condition_id,
        rewards_max_spread=_as_float(row.get("rewards_max_spread")),
        rewards_min_size=_as_float(row.get("rewards_min_size")),
        daily_rate_usdc=_sum_daily_rates(row.get("rates")),
        fetched_at=time.time(),
    )


def _as_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return 0.0
    return out


def _sum_daily_rates(rates: Any) -> float:
    """``rates`` 是 [{asset_address, rewards_daily_rate}, ...]，求和."""
    if not isinstance(rates, list):
        return 0.0
    total = 0.0
    for entry in rates:
        if not isinstance(entry, dict):
            continue
        total += _as_float(
            entry.get("rewards_daily_rate")
            if entry.get("rewards_daily_rate") is not None
            else entry.get("daily_rate")
        )
    return total


class RewardsClient:
    """带 TTL 缓存的只读奖励参数客户端。**任何异常都不外抛**."""

    def __init__(
        self,
        clob_host: str,
        *,
        session: Any | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        ttl_sec: float = DEFAULT_TTL_SEC,
        negative_ttl_sec: float = DEFAULT_NEGATIVE_TTL_SEC,
        fetch_interval_sec: float = 0.1,
        enabled: bool = True,
    ) -> None:
        self._host = (clob_host or "").rstrip("/")
        self._session = session or requests.Session()
        self._timeout_sec = max(0.1, float(timeout_sec))
        self._ttl_sec = max(0.0, float(ttl_sec))
        self._negative_ttl_sec = max(0.0, float(negative_ttl_sec))
        self._enabled = bool(enabled) and bool(self._host)
        self._lock = threading.Lock()
        # condition_id -> (expires_at, RewardsConfig | None)
        self._cache: dict[str, tuple[float, Optional[RewardsConfig]]] = {}
        self._stats = {"hits": 0, "misses": 0, "errors": 0, "incentivized": 0}
        # 后台预热：扫描热路径只读缓存，网络调用全部丢给 worker 线程。
        self._pending: collections.deque[str] = collections.deque()
        self._pending_set: set[str] = set()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fetch_interval_sec = max(0.0, float(fetch_interval_sec))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get(self, condition_id: str) -> Optional[RewardsConfig]:
        """返回市场的奖励参数；未知/失败/未启用一律 ``None``."""
        cid = (condition_id or "").strip()
        if not self._enabled or not cid:
            return None

        now = time.time()
        with self._lock:
            cached = self._cache.get(cid)
            if cached is not None and cached[0] > now:
                self._stats["hits"] += 1
                return cached[1]

        config = self._fetch(cid)
        ttl = self._ttl_sec if config is not None else self._negative_ttl_sec
        with self._lock:
            self._cache[cid] = (time.time() + ttl, config)
            self._stats["misses"] += 1
            if config is not None and config.is_incentivized:
                self._stats["incentivized"] += 1
        return config

    def reward_delta(self, condition_id: str) -> float:
        """T3 报价路径的主入口：拿不到就返回 0（等价于旧行为）."""
        config = self.get(condition_id)
        return config.reward_delta if config is not None else 0.0

    def cached(self, condition_id: str) -> Optional[RewardsConfig]:
        """只读缓存，**绝不发起网络请求** —— 扫描热路径专用.

        未命中返回 ``None``，调用方按"无奖励带"处理。配合
        :meth:`request` 在后台预热，热路径就永远不会因为奖励元数据
        而阻塞。
        """
        cid = (condition_id or "").strip()
        if not self._enabled or not cid:
            return None
        now = time.time()
        with self._lock:
            cached = self._cache.get(cid)
        if cached is None or cached[0] <= now:
            return None
        return cached[1]

    def cached_reward_delta(self, condition_id: str) -> float:
        config = self.cached(condition_id)
        return config.reward_delta if config is not None else 0.0

    def request(self, condition_ids: list[str], *, max_pending: int = 200) -> int:
        """把缓存缺失的市场排进后台预热队列，返回本次新入队数量.

        入队而不是直接拉取：单周期可能有上百个市场，串行 HTTP 会把
        扫描周期拖垮。worker 线程按 ``fetch_interval_sec`` 节流消费。
        """
        if not self._enabled:
            return 0
        now = time.time()
        queued = 0
        with self._lock:
            for raw in condition_ids:
                cid = (raw or "").strip()
                if not cid or cid in self._pending_set:
                    continue
                cached = self._cache.get(cid)
                if cached is not None and cached[0] > now:
                    continue
                if len(self._pending) >= max(1, int(max_pending)):
                    break
                self._pending.append(cid)
                self._pending_set.add(cid)
                queued += 1
        if queued:
            self._ensure_worker()
        return queued

    def prefetch(self, condition_ids: list[str], *, max_fetches: int = 20) -> int:
        """同步预热（阻塞）。仅供离线脚本 / 测试使用，主循环用 request()."""
        if not self._enabled:
            return 0
        fetched = 0
        now = time.time()
        for raw in condition_ids:
            if fetched >= max(0, int(max_fetches)):
                break
            cid = (raw or "").strip()
            if not cid:
                continue
            with self._lock:
                cached = self._cache.get(cid)
            if cached is not None and cached[0] > now:
                continue
            self.get(cid)
            fetched += 1
        return fetched

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self._stop.is_set():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, name="rewards-prefetch", daemon=True
            )
            worker = self._worker
        worker.start()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if not self._pending:
                    # 队列空就退出线程；下次 request() 再拉起，避免
                    # 常驻一个什么都不做的线程。
                    self._worker = None
                    return
                cid = self._pending.popleft()
                self._pending_set.discard(cid)
            try:
                self.get(cid)
            except Exception as exc:  # noqa: BLE001 - worker 不允许崩
                LOG.debug("rewards 预热失败 %s: %s", cid[:12], exc)
            if self._fetch_interval_sec > 0:
                self._stop.wait(self._fetch_interval_sec)

    def close(self) -> None:
        """停止后台预热线程（进程退出前调用；不调用也不会泄漏，daemon）."""
        self._stop.set()
        with self._lock:
            self._pending.clear()
            self._pending_set.clear()
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

    def stats(self) -> dict[str, int]:
        with self._lock:
            out = dict(self._stats)
            out["cached_markets"] = len(self._cache)
            out["pending"] = len(self._pending)
        return out

    def _fetch(self, condition_id: str) -> Optional[RewardsConfig]:
        url = f"{self._host}/rewards/markets/{condition_id}"
        try:
            resp = self._session.get(url, timeout=self._timeout_sec)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 奖励元数据绝不能拖垮报价
            with self._lock:
                self._stats["errors"] += 1
            LOG.debug("rewards 元数据拉取失败 %s: %s", condition_id[:12], exc)
            return None
        try:
            return parse_rewards_payload(condition_id, payload)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._stats["errors"] += 1
            LOG.debug("rewards 响应解析失败 %s: %s", condition_id[:12], exc)
            return None
