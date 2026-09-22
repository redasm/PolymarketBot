"""Binance 现货价格 feed + UPDOWN 窗口参考价追踪 (T2 UPDOWN Phase 2).

为什么需要它:
  Polymarket UPDOWN 市场 (如 `btc-updown-15m-{slot}`) 问的是"15 分钟窗口结束时
  现货价是否高于窗口起点价"。给它定价 (`fair_value_model.compute_fair_updown`)
  需要三个外部输入,全部来自标的现货而非 Polymarket 盘口:
    - s_now:  当前现货价
    - ref_px: 窗口起点 (UTC 对齐的 15m 边界) 的现货价
    - sigma:  现货 15 分钟对数收益波动率

线程模型:
  与 `websocket_feed.WebSocketFeed` 一致 —— daemon 线程内跑同步 websockets
  客户端 (websockets.sync.client),自动重连。主扫描循环是同步的,通过加锁的
  getter 读最新现货价 / sigma / ref_px,绝不阻塞热路径。

数据源:
  Binance 公共 WS combined stream (无需 key):
    wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade
  sigma 冷启动用 REST /api/v3/klines 预热 (避免 ~20min 暖机)。

所有 IO 失败都吞掉并重试 —— 这是增益路径,绝不能拖垮主循环。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import requests

from polymarket_arb.volatility_estimator import VolEstimator

LOG = logging.getLogger(__name__)

# 默认 symbol -> Binance 现货交易对映射。可被 config.t2_updown_spot_pairs 覆盖。
_DEFAULT_PAIRS: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
}

_WS_HOST = "wss://stream.binance.com:9443/stream"
_REST_HOST = "https://api.binance.com"


def parse_spot_pairs(symbols: list[str], overrides: str = "") -> dict[str, str]:
    """构造 symbol(lower) -> binance pair(UPPER) 映射.

    `overrides` 是逗号分隔的 `symbol:pair`,优先于内置默认值。未知 symbol
    若无 override 则回退 `{SYM}USDT`。
    """
    mapping: dict[str, str] = {}
    override_map: dict[str, str] = {}
    for item in (overrides or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        sym, pair = item.split(":", 1)
        sym = sym.strip().lower()
        pair = pair.strip().upper()
        if sym and pair:
            override_map[sym] = pair
    for sym in symbols:
        sym = sym.strip().lower()
        if not sym:
            continue
        if sym in override_map:
            mapping[sym] = override_map[sym]
        elif sym in _DEFAULT_PAIRS:
            mapping[sym] = _DEFAULT_PAIRS[sym]
        else:
            mapping[sym] = f"{sym.upper()}USDT"
    return mapping


class UpdownRefTracker:
    """追踪每个 (symbol, window) 的窗口起点参考价 ref_px.

    UPDOWN 窗口是 UTC 对齐的固定长度区间: slot = floor(now / window_sec) * window_sec
    是窗口起始 unix 秒。ref_px 定义为窗口起点那一刻的现货价。

    `observe()` 在收到每个现货 tick 时调用: 当它发现某 (symbol, window) 进入了
    一个尚未记录的新 slot 时,就把当前现货价锁定为该 slot 的 ref_px。一旦记录
    便不再覆盖 (窗口内后续 tick 不改 ref)。

    冷启动语义: bot 启动时多半处于某个窗口的中途,该窗口的真实起点价已错过 ——
    `get_ref` 对这种"中途加入"的窗口返回 None (调用方据此跳过该窗口定价),直到
    下一个窗口起点被实时捕获。这是有意的保守取舍: 宁可不定价,也不用错误的中途
    价冒充 ref_px。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (symbol, window_sec) -> (slot, ref_px)
        self._refs: dict[tuple[str, int], tuple[int, float]] = {}
        # 进程启动时刻 (秒)。早于此刻起点的窗口视为"中途加入",不可信。
        self._start_ts: float = time.time()

    @staticmethod
    def slot_for(now_sec: float, window_sec: int) -> int:
        """返回包含 now_sec 的窗口起始 unix 秒 (UTC 对齐)."""
        w = max(1, int(window_sec))
        return int(now_sec // w) * w

    def observe(self, symbol: str, window_sec: int, spot: float, now_sec: Optional[float] = None) -> None:
        """喂入一个现货 tick;若进入新 slot 则锁定 ref_px."""
        if spot is None or spot <= 0:
            return
        if now_sec is None:
            now_sec = time.time()
        w = max(1, int(window_sec))
        slot = self.slot_for(now_sec, w)
        key = (symbol.lower(), w)
        with self._lock:
            existing = self._refs.get(key)
            if existing is None or existing[0] != slot:
                self._refs[key] = (slot, float(spot))

    def get_ref(self, symbol: str, window_sec: int, slot: int) -> Optional[float]:
        """返回指定 (symbol, window, slot) 的 ref_px;不可信/未记录则 None.

        仅当记录的 slot 精确匹配请求 slot,且该 slot 起点不早于进程启动时刻
        (排除中途加入的窗口) 时才返回。
        """
        w = max(1, int(window_sec))
        key = (symbol.lower(), w)
        with self._lock:
            rec = self._refs.get(key)
        if rec is None or rec[0] != int(slot):
            return None
        # 中途加入保护: 若窗口起点早于进程启动,说明我们没在起点观测到价格。
        if rec[0] < self._start_ts:
            return None
        return rec[1]


class BinanceSpotFeed:
    """Binance 现货价 WS feed (daemon 线程) + 每币种 VolEstimator + ref 追踪.

    与 `WebSocketFeed` 同构: `start()` 拉起 daemon 线程,内部用同步 websockets
    客户端连 combined trade stream,自动重连;`stop()` 优雅停止。主循环通过
    `get_spot` / `get_sigma_15m` / `ref_tracker` 读最新状态,全部加锁,不阻塞。

    sigma 维护: trade 流按分钟聚合成 1m 收盘价喂给 VolEstimator;启动时用 REST
    /api/v3/klines 预热,使 sigma 立即可用而非等 ~20min 暖机。
    """

    def __init__(
        self,
        *,
        pairs: dict[str, str],
        window_secs: list[int],
        fast_minutes: int = 60,
        slow_minutes: int = 360,
        min_bars: int = 20,
        warmup_klines: int = 500,
    ) -> None:
        # symbol(lower) -> binance pair(UPPER)
        self._pairs = {s.lower(): p.upper() for s, p in pairs.items()}
        self._pair_to_symbol = {p: s for s, p in self._pairs.items()}
        self._window_secs = [max(1, int(w)) for w in window_secs] or [900]
        self._warmup_klines = max(0, int(warmup_klines))

        self._lock = threading.Lock()
        self._spot: dict[str, float] = {}
        self._vol: dict[str, VolEstimator] = {
            sym: VolEstimator(fast_minutes=fast_minutes, slow_minutes=slow_minutes, min_bars=min_bars)
            for sym in self._pairs
        }
        # 当前分钟桶: symbol -> (minute_epoch, last_price)
        self._cur_minute: dict[str, tuple[int, float]] = {}

        self.ref_tracker = UpdownRefTracker()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = 1.0
        self._connected = False

    # ----- public getters (locked, hot-path safe) -----

    def get_spot(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._spot.get(symbol.lower())

    def get_sigma_15m(self, symbol: str) -> Optional[float]:
        sym = symbol.lower()
        est = self._vol.get(sym)
        if est is None:
            return None
        snap = est.snapshot()
        if not snap.get("ready"):
            return None
        # slow sigma 是定价基准 (稳定);缺失时回退 blend/fast。
        return snap.get("sigma_slow_15m") or snap.get("sigma_blend_15m") or snap.get("sigma_fast_15m")

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    # ----- lifecycle -----

    def start(self) -> None:
        if self._running:
            return
        if not self._pairs:
            LOG.info("BinanceSpotFeed: 无交易对,跳过启动")
            return
        self._running = True
        self._warmup_sigma()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="spot-feed")
        self._thread.start()
        LOG.info("BinanceSpotFeed 已启动,交易对=%s", ",".join(sorted(self._pairs.values())))

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._connected = False

    # ----- sigma warmup (REST klines) -----

    def _warmup_sigma(self) -> None:
        """用 REST /api/v3/klines 拉 1m 收盘价预热每币种 VolEstimator."""
        if self._warmup_klines <= 0:
            return
        for sym, pair in self._pairs.items():
            try:
                resp = requests.get(
                    f"{_REST_HOST}/api/v3/klines",
                    params={"symbol": pair, "interval": "1m", "limit": self._warmup_klines},
                    timeout=8,
                )
                resp.raise_for_status()
                rows = resp.json()
                # kline row: [open_time, open, high, low, close, volume, close_time, ...]
                closes = [float(r[4]) for r in rows if isinstance(r, list) and len(r) > 4]
                if len(closes) >= 2:
                    n = self._vol[sym].warmup_from_closes(closes, int(time.time() * 1000))
                    with self._lock:
                        self._spot.setdefault(sym, closes[-1])
                    LOG.info("BinanceSpotFeed: %s sigma 预热 %d 根 K 线 (%d 收益率)", pair, len(closes), n)
            except (requests.RequestException, ValueError, KeyError, IndexError) as e:
                LOG.warning("BinanceSpotFeed: %s sigma 预热失败: %s", pair, e)

    # ----- per-trade update -----

    def _on_trade(self, pair: str, price: float, trade_ms: int) -> None:
        sym = self._pair_to_symbol.get(pair)
        if sym is None or price <= 0:
            return
        with self._lock:
            self._spot[sym] = price
        # 喂 ref tracker (所有配置窗口)
        now_sec = trade_ms / 1000.0
        for w in self._window_secs:
            self.ref_tracker.observe(sym, w, price, now_sec=now_sec)
        # 按分钟聚合成 1m 收盘价喂 VolEstimator
        minute = int(trade_ms // 60_000)
        prev = self._cur_minute.get(sym)
        if prev is None:
            self._cur_minute[sym] = (minute, price)
            return
        prev_minute, prev_price = prev
        if minute > prev_minute:
            # 上一分钟收盘 = 上一分钟最后一个价
            est = self._vol.get(sym)
            if est is not None:
                est.update_1m_close(prev_price, prev_minute * 60_000)
            self._cur_minute[sym] = (minute, price)
        else:
            self._cur_minute[sym] = (minute, price)

    # ----- WS loop -----

    def _stream_path(self) -> str:
        streams = "/".join(f"{p.lower()}@trade" for p in sorted(self._pairs.values()))
        return f"{_WS_HOST}?streams={streams}"

    def _run_loop(self) -> None:
        import websockets.sync.client as ws_sync
        from websockets.exceptions import ConnectionClosed

        url = self._stream_path()
        while self._running:
            try:
                with ws_sync.connect(url, close_timeout=5, max_size=4 * 1024 * 1024) as ws:
                    with self._lock:
                        self._connected = True
                    self._reconnect_delay = 1.0
                    LOG.info("BinanceSpotFeed 已连接: %s", url)
                    while self._running:
                        try:
                            raw = ws.recv(timeout=15)
                        except TimeoutError:
                            continue
                        self._handle_message(raw)
            except ConnectionClosed:
                LOG.warning("BinanceSpotFeed 连接关闭,准备重连")
            except Exception as e:  # noqa: BLE001 - feed 绝不能拖垮主进程
                LOG.warning("BinanceSpotFeed 循环异常: %s", e)
            finally:
                with self._lock:
                    self._connected = False
            if self._running:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(30.0, self._reconnect_delay * 2)

    def _handle_message(self, raw: object) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        # combined stream 包了一层 {"stream": "...", "data": {...}}
        data = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(data, dict):
            return
        if data.get("e") != "trade":
            return
        pair = str(data.get("s") or "").upper()
        try:
            price = float(data.get("p"))
            trade_ms = int(data.get("T") or data.get("E") or time.time() * 1000)
        except (TypeError, ValueError):
            return
        self._on_trade(pair, price, trade_ms)



