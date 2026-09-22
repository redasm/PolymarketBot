"""Polymarket RTDS 现货价 feed（结算口径参考源）+ 与 Binance 的双源合成.

为什么需要它:

UPDOWN 市场（`btc-updown-15m-{slot}`）问的是"窗口结束时现货价是否高于
窗口起点价"，而**结算用的是 Polymarket 自己的价格源**，不是 Binance。
`spot_feed.BinanceSpotFeed` 用 Binance 的 trade 流定价，等于在用一个和
结算口径不同的参考价给合约定价 —— 两者的价差（basis）在平静时段可以
忽略，但正是在剧烈波动、UPDOWN 最容易出现边际定价机会的时候最大。

数据源: ``wss://ws-live-data.polymarket.com``，topic ``crypto_prices``
（以及 ``crypto_prices_chainlink``）。

**关于 payload 结构的不确定性（重要）**：订阅协议是确定的
（``{"action": "subscribe", "subscriptions": [{"topic", "type"}]}``，
消息形如 ``{"topic", "type", "timestamp", "payload"}``），但
``crypto_prices`` 的 payload 字段名没有拿到权威样本。因此:

- `parse_rtds_crypto_payload` 对字段名做**宽容匹配**（symbol/pair/asset,
  value/price/close…），认不出来就静默丢弃，绝不猜测性地喂给定价；
- `CompositeSpotFeed` 默认运行在 **shadow 模式**：RTDS 只记录与
  Binance 的 basis telemetry，定价仍走 Binance。等 telemetry 证明
  RTDS 侧的数据连续、可信，再把 `T2_UPDOWN_RTDS_MODE` 切到 `primary`。

这和 `doc/zh/pending-validations.md` 里其它影子特性的处理方式一致：先观测，
后接管。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from polymarket_arb.spot_feed import UpdownRefTracker
from polymarket_arb.volatility_estimator import VolEstimator

LOG = logging.getLogger(__name__)

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
RTDS_CRYPTO_TOPIC = "crypto_prices"
RTDS_CRYPTO_CHAINLINK_TOPIC = "crypto_prices_chainlink"

_PING_INTERVAL_SEC = 5.0
_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_MAX_SEC = 60.0
# 主源多久没更新就算失效，回落到备源。UPDOWN 窗口最短 15 分钟，
# 30 秒的容忍度既能吸收正常抖动，又不会让定价用上明显过期的价格。
DEFAULT_STALENESS_SEC = 30.0

_KNOWN_SYMBOLS = ("btc", "eth", "sol", "xrp", "doge", "matic", "link", "ada")
_QUOTE_SUFFIXES = ("usdt", "usdc", "usd", "perp")


def normalize_rtds_symbol(raw: Any) -> str:
    """把 "BTC/USD" / "BTCUSDT" / "btc-usd" / "BTC" 统一成 "btc".

    认不出来返回空串 —— 调用方据此丢弃该条，而不是猜一个 symbol。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    for sep in ("/", "-", "_", ":"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    for suffix in _QUOTE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    text = text.strip()
    if not text:
        return ""
    # 只接受纯字母的短代号（2-8 字符），挡掉 condition_id、以及切分
    # 垃圾串后剩下的单字母残片 —— 没有任何真实币种代号只有一个字母。
    if not text.isalpha() or not (2 <= len(text) <= 8):
        return ""
    return text


def _as_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out <= 0:
        return None
    return out


def _as_ts(value: Any) -> float:
    raw = _as_float(value)
    if raw is None:
        return time.time()
    return raw / 1000.0 if raw > 1e11 else raw


def parse_rtds_crypto_payload(msg: dict) -> list[tuple[str, float, float]]:
    """从一条 RTDS 消息里抽出 (symbol, price, ts) 三元组.

    payload 可能是单个对象，也可能是对象数组。字段名做宽容匹配；任何
    抽不出 symbol 或正价格的条目直接丢弃。
    """
    topic = str(msg.get("topic") or "").strip().lower()
    if topic and topic not in (RTDS_CRYPTO_TOPIC, RTDS_CRYPTO_CHAINLINK_TOPIC):
        return []

    payload = msg.get("payload", msg)
    rows: list[dict]
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        return []

    msg_ts = msg.get("timestamp")
    out: list[tuple[str, float, float]] = []
    for row in rows:
        symbol = normalize_rtds_symbol(
            row.get("symbol")
            or row.get("pair")
            or row.get("asset")
            or row.get("ticker")
            or row.get("market")
        )
        if not symbol:
            continue
        price = None
        for key in ("value", "price", "close", "last", "p"):
            price = _as_float(row.get(key))
            if price is not None:
                break
        if price is None:
            continue
        ts = _as_ts(row.get("timestamp") or row.get("time") or row.get("t") or msg_ts)
        out.append((symbol, price, ts))
    return out


class RtdsSpotFeed:
    """RTDS 现货价 daemon 线程 feed.

    对外协议与 `BinanceSpotFeed` 完全一致（get_spot / get_sigma_15m /
    ref_tracker），因此可以被 `UpdownPricer` 直接消费，也可以塞进
    `CompositeSpotFeed` 做主源。
    """

    def __init__(
        self,
        *,
        symbols: list[str],
        window_secs: list[int],
        ws_url: str = RTDS_WS_URL,
        topics: list[str] | None = None,
        fast_minutes: int = 60,
        slow_minutes: int = 360,
        min_bars: int = 20,
    ) -> None:
        self._symbols = {s.strip().lower() for s in symbols if s and s.strip()}
        self._window_secs = [max(1, int(w)) for w in window_secs] or [900]
        self._ws_url = ws_url
        self._topics = list(topics or [RTDS_CRYPTO_TOPIC])

        self._lock = threading.Lock()
        self._spot: dict[str, float] = {}
        self._spot_ts: dict[str, float] = {}
        self._vol: dict[str, VolEstimator] = {
            sym: VolEstimator(
                fast_minutes=fast_minutes, slow_minutes=slow_minutes, min_bars=min_bars
            )
            for sym in self._symbols
        }
        self._cur_minute: dict[str, tuple[int, float]] = {}
        self._stats = {"messages": 0, "ticks": 0, "unknown_symbols": 0, "reconnects": 0}

        self.ref_tracker = UpdownRefTracker()

        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None

    # ----- getters (locked, hot-path safe) -----

    def get_spot(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._spot.get(symbol.lower())

    def get_spot_age(self, symbol: str, *, now: float | None = None) -> Optional[float]:
        """距离该 symbol 最近一次更新的秒数；从未更新返回 None."""
        now = time.time() if now is None else float(now)
        with self._lock:
            ts = self._spot_ts.get(symbol.lower())
        return None if ts is None else max(0.0, now - ts)

    def get_sigma_15m(self, symbol: str) -> Optional[float]:
        est = self._vol.get(symbol.lower())
        if est is None:
            return None
        snap = est.snapshot()
        if not snap.get("ready"):
            return None
        return (
            snap.get("sigma_slow_15m")
            or snap.get("sigma_blend_15m")
            or snap.get("sigma_fast_15m")
        )

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def stats(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._stats)
        out["connected"] = self.is_connected()
        out["symbols"] = sorted(self._symbols)
        return out

    # ----- lifecycle -----

    def start(self) -> None:
        if self._running:
            return
        if not self._symbols:
            LOG.info("RtdsSpotFeed: 无 symbol，跳过启动")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="rtds-spot-feed"
        )
        self._thread.start()
        LOG.info(
            "RtdsSpotFeed 已启动，symbol=%s topic=%s",
            ",".join(sorted(self._symbols)),
            ",".join(self._topics),
        )

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._connected = False

    # ----- ingest -----

    def _subscription_payload(self) -> dict[str, Any]:
        return {
            "action": "subscribe",
            "subscriptions": [
                {"topic": topic, "type": "*"} for topic in self._topics
            ],
        }

    def handle_message(self, msg: dict) -> int:
        """处理一条已解析的 RTDS 消息，返回落地的 tick 数（测试直接调用）."""
        applied = 0
        with self._lock:
            self._stats["messages"] += 1
        for symbol, price, ts in parse_rtds_crypto_payload(msg):
            if symbol not in self._symbols:
                with self._lock:
                    self._stats["unknown_symbols"] += 1
                continue
            self._apply_tick(symbol, price, ts)
            applied += 1
        return applied

    def _apply_tick(self, symbol: str, price: float, ts: float) -> None:
        with self._lock:
            self._spot[symbol] = price
            self._spot_ts[symbol] = ts
            self._stats["ticks"] += 1
            minute = int(ts // 60)
            current = self._cur_minute.get(symbol)
            closed_price: Optional[float] = None
            closed_minute: Optional[int] = None
            if current is not None and minute > current[0]:
                # 分钟切换：上一分钟的最后一个价就是它的收盘价。
                closed_minute, closed_price = current[0], current[1]
            self._cur_minute[symbol] = (minute, price)
        if closed_price is not None and closed_minute is not None:
            est = self._vol.get(symbol)
            if est is not None:
                try:
                    est.update_1m_close(closed_price, closed_minute * 60_000)
                except Exception as exc:  # noqa: BLE001 - 波动率是增益路径
                    LOG.debug("RTDS sigma 更新失败 %s: %s", symbol, exc)
        for window in self._window_secs:
            self.ref_tracker.observe(symbol, window, price, now_sec=ts)

    def _handle_frame(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            return
        stripped = raw.strip()
        if not stripped or stripped.upper() in ("PING", "PONG"):
            return
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return
        messages = data if isinstance(data, list) else [data]
        for msg in messages:
            if isinstance(msg, dict):
                self.handle_message(msg)

    def _run_loop(self) -> None:
        import websockets.sync.client as ws_sync

        backoff = _BACKOFF_INITIAL_SEC
        while self._running:
            try:
                with ws_sync.connect(
                    self._ws_url, close_timeout=5, max_size=4 * 1024 * 1024
                ) as ws:
                    ws.send(json.dumps(self._subscription_payload()))
                    with self._lock:
                        self._connected = True
                    backoff = _BACKOFF_INITIAL_SEC
                    LOG.info("RTDS 已连接并订阅")
                    last_ping = time.time()
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
            except Exception as exc:  # noqa: BLE001 - 断线重连是常态
                if self._running:
                    with self._lock:
                        self._stats["reconnects"] += 1
                    LOG.warning("RTDS 断开: %s，%.1f 秒后重连", exc, backoff)
            finally:
                with self._lock:
                    self._connected = False
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(_BACKOFF_MAX_SEC, backoff * 1.5)


class _CompositeRefTracker:
    """按当前生效的源返回窗口起点参考价."""

    def __init__(self, composite: "CompositeSpotFeed") -> None:
        self._composite = composite

    def get_ref(self, symbol: str, window_sec: int, slot: int) -> Optional[float]:
        for feed in self._composite.active_feeds(symbol):
            tracker = getattr(feed, "ref_tracker", None)
            if tracker is None:
                continue
            ref = tracker.get_ref(symbol, window_sec, slot)
            if ref is not None:
                return ref
        return None


class CompositeSpotFeed:
    """RTDS（结算口径）+ Binance（流动性口径）双源.

    三种模式:
      - ``off``：只用 Binance，等价于接入前的行为；
      - ``shadow``（默认）：定价仍走 Binance，RTDS 只产出 basis telemetry；
      - ``primary``：定价用 RTDS，RTDS 数据过期（超过 staleness）时自动
        回落到 Binance。

    `basis_report()` 给出每个 symbol 两源价格及其 bps 偏离，是决定能否
    从 shadow 切到 primary 的依据。
    """

    MODES = ("off", "shadow", "primary")

    def __init__(
        self,
        *,
        primary: Any,
        fallback: Any,
        mode: str = "shadow",
        staleness_sec: float = DEFAULT_STALENESS_SEC,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._mode = mode if mode in self.MODES else "shadow"
        self._staleness_sec = max(0.0, float(staleness_sec))
        self.ref_tracker = _CompositeRefTracker(self)

    @property
    def mode(self) -> str:
        return self._mode

    def _primary_is_usable(self, symbol: str, *, now: float | None = None) -> bool:
        if self._mode != "primary" or self._primary is None:
            return False
        if self._primary.get_spot(symbol) is None:
            return False
        if self._staleness_sec <= 0:
            return True
        age = self._primary.get_spot_age(symbol, now=now)
        return age is not None and age <= self._staleness_sec

    def active_feeds(self, symbol: str) -> list[Any]:
        """按优先级返回本次取数应该问的 feed."""
        if self._primary_is_usable(symbol):
            return [self._primary, self._fallback]
        return [self._fallback]

    def get_spot(self, symbol: str) -> Optional[float]:
        for feed in self.active_feeds(symbol):
            if feed is None:
                continue
            value = feed.get_spot(symbol)
            if value is not None:
                return value
        return None

    def get_sigma_15m(self, symbol: str) -> Optional[float]:
        for feed in self.active_feeds(symbol):
            if feed is None:
                continue
            value = feed.get_sigma_15m(symbol)
            if value is not None:
                return value
        return None

    def is_connected(self) -> bool:
        for feed in (self._fallback, self._primary):
            if feed is not None and feed.is_connected():
                return True
        return False

    def start(self) -> None:
        if self._fallback is not None:
            self._fallback.start()
        if self._primary is not None and self._mode != "off":
            self._primary.start()

    def stop(self) -> None:
        if self._primary is not None:
            self._primary.stop()
        if self._fallback is not None:
            self._fallback.stop()

    def basis_report(self, symbols: list[str], *, now: float | None = None) -> dict:
        """两源价差报告。两边都有价才算 basis，否则只记状态."""
        rows: dict[str, dict] = {}
        max_abs_bps = 0.0
        for raw in symbols:
            symbol = str(raw or "").strip().lower()
            if not symbol:
                continue
            primary_px = (
                self._primary.get_spot(symbol) if self._primary is not None else None
            )
            fallback_px = (
                self._fallback.get_spot(symbol) if self._fallback is not None else None
            )
            row: dict[str, Any] = {
                "rtds": primary_px,
                "binance": fallback_px,
                "rtds_age_sec": (
                    self._primary.get_spot_age(symbol, now=now)
                    if self._primary is not None
                    else None
                ),
                "source": (
                    "rtds" if self._primary_is_usable(symbol, now=now) else "binance"
                ),
            }
            if primary_px and fallback_px:
                basis_bps = (primary_px - fallback_px) / fallback_px * 10_000.0
                row["basis_bps"] = round(basis_bps, 3)
                max_abs_bps = max(max_abs_bps, abs(basis_bps))
            rows[symbol] = row
        return {
            "mode": self._mode,
            "max_abs_basis_bps": round(max_abs_bps, 3) if rows else None,
            "symbols": rows,
        }
