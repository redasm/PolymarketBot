"""多尺度波动率估算器：从 1 分钟 K 线收盘价计算 fast/slow/blend 三档 sigma.

取代原 maker_strategy.DynamicSpreadCalculator 中简陋的 _estimate_volatility。

为什么需要多尺度:
  - sigma_fast（1小时窗口）: 对波动率飙升反应迅速 → 做市策略用于风控
  - sigma_slow（6小时窗口）: 稳定基准 → FairValueModel 用于定价
  - sigma_blend: 自适应混合 → 兼顾灵敏度和稳定性

方法论:
  1. 收集 1 分钟 K 线收盘价
  2. 计算对数收益率: r_t = ln(close_t / close_{t-1})
  3. sigma_fast_1m = std(r_t) over fast 窗口
  4. sigma_slow_1m = std(r_t) over slow 窗口
  5. 缩放到 15 分钟: sigma_*_15m = sigma_*_1m × √15
  6. Blend: w × sigma_fast + (1-w) × sigma_slow

移植自 mlmodelpoly/volatility.py，去除外部配置依赖。
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Optional

from polymarket_arb.utils_time import now_ms

LOG = logging.getLogger(__name__)

DEFAULT_FAST_MINUTES = 60
DEFAULT_SLOW_MINUTES = 360
DEFAULT_MIN_BARS = 20
SQRT_15 = math.sqrt(15)


class VolEstimator:
    """多尺度波动率估算器.

    Attributes:
        fast_minutes: 快速 sigma 窗口（默认 60 分钟）
        slow_minutes: 慢速 sigma 窗口（默认 360 分钟）
        min_bars: 最少数据点要求
    """

    def __init__(
        self,
        fast_minutes: int = DEFAULT_FAST_MINUTES,
        slow_minutes: int = DEFAULT_SLOW_MINUTES,
        min_bars: int = DEFAULT_MIN_BARS,
    ) -> None:
        self.fast_minutes = fast_minutes
        self.slow_minutes = slow_minutes
        self.min_bars = min_bars

        max_size = max(slow_minutes + 10, 500)
        self._closes: deque[tuple[int, float]] = deque(maxlen=max_size)
        self._last_close: Optional[float] = None
        self._last_ts_ms: Optional[int] = None
        self._update_count = 0

    def update_1m_close(self, close_px: float, ts_ms: int) -> Optional[float]:
        """输入新的 1 分钟 K 线收盘价.

        Returns:
            本次的 log return（如果有前值），否则 None。
        """
        if close_px <= 0:
            return None

        log_return = None
        if self._last_close is not None and self._last_close > 0:
            log_return = math.log(close_px / self._last_close)
            self._update_count += 1

        self._closes.append((int(ts_ms), float(close_px)))
        self._last_close = close_px
        self._last_ts_ms = ts_ms
        return log_return

    def snapshot(self, rvol_5s: Optional[float] = None) -> dict:
        """获取当前波动率估计.

        Args:
            rvol_5s: 可选的相对成交量（>1 表示活跃，用于 blend 权重）

        Returns:
            包含 sigma_fast_15m / sigma_slow_15m / sigma_blend_15m / ready 等字段的字典。
        """
        n_bars = len(self._closes)
        result: dict = {
            "n_bars": n_bars,
            "last_close": self._last_close,
            "last_update_ms": self._last_ts_ms,
        }

        if n_bars < self.min_bars + 1:
            result.update(sigma_fast_15m=None, sigma_slow_15m=None, sigma_blend_15m=None, ready=False)
            return result

        fast_rets = self._log_returns(self.fast_minutes)
        slow_rets = self._log_returns(self.slow_minutes)

        sig_fast_1m = self._stdev(fast_rets)
        sig_slow_1m = self._stdev(slow_rets)

        if sig_fast_1m is None and sig_slow_1m is None:
            result.update(sigma_fast_15m=None, sigma_slow_15m=None, sigma_blend_15m=None, ready=False)
            return result

        if sig_slow_1m is None:
            sig_slow_1m = sig_fast_1m

        sig_fast_15m = sig_fast_1m * SQRT_15 if sig_fast_1m is not None else None
        sig_slow_15m = sig_slow_1m * SQRT_15 if sig_slow_1m is not None else None

        w = 0.5
        if rvol_5s is not None:
            w = max(0.2, min(0.9, 0.5 + (rvol_5s - 1.0) * 0.5))

        if sig_fast_15m is None:
            sig_blend_15m = sig_slow_15m
        elif sig_slow_15m is None:
            sig_blend_15m = sig_fast_15m
        else:
            sig_blend_15m = w * sig_fast_15m + (1 - w) * sig_slow_15m

        result.update(
            sigma_fast_15m=_safe_round(sig_fast_15m),
            sigma_slow_15m=_safe_round(sig_slow_15m),
            sigma_blend_15m=_safe_round(sig_blend_15m),
            ready=True,
        )
        return result

    def get_sigma_15m(self) -> Optional[float]:
        """快捷方法：返回 fast sigma scaled to 15m（兼容旧接口）."""
        snap = self.snapshot()
        return snap.get("sigma_fast_15m")

    def warmup_from_closes(self, closes: list[float], ts_ms: int) -> int:
        """从历史收盘价列表预热估算器.

        Args:
            closes: 按时间升序排列的收盘价列表
            ts_ms: 当前时间戳（毫秒）

        Returns:
            计算出的 log return 数量。
        """
        self._closes.clear()
        self._last_close = None
        self._last_ts_ms = None
        self._update_count = 0

        count = 0
        for i, px in enumerate(closes):
            if px <= 0:
                continue
            fake_ts = ts_ms - (len(closes) - i - 1) * 60_000
            self._closes.append((fake_ts, px))
            if self._last_close is not None and self._last_close > 0:
                count += 1
            self._last_close = px

        self._last_ts_ms = ts_ms
        self._update_count = count
        return count

    def is_ready(self) -> bool:
        return len(self._closes) >= self.min_bars + 1

    def _log_returns(self, n: int) -> Optional[list[float]]:
        if len(self._closes) < n + 1:
            return None
        closes = [c for _, c in list(self._closes)[-(n + 1):]]
        returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0 and closes[i] > 0:
                returns.append(math.log(closes[i] / closes[i - 1]))
        return returns or None

    @staticmethod
    def _stdev(xs: Optional[list[float]]) -> Optional[float]:
        if not xs or len(xs) < 2:
            return None
        n = len(xs)
        mean = sum(xs) / n
        variance = sum((x - mean) ** 2 for x in xs) / (n - 1)
        return math.sqrt(variance)


def _safe_round(x: Optional[float], decimals: int = 6) -> Optional[float]:
    return round(x, decimals) if x is not None else None
