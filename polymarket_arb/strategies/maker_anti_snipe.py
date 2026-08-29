"""T3 抗狙击保护：中间价跳变暂停 / 稳定确认 / 滤波 / 成交冷却 / 追价上限.

做市的成本结构里，逆向选择（adverse selection）通常比 spread 收益更重要：
知情交易者总是在报价的不利方向吃单。最典型的三个被吃场景:

1. **跳变瞬间被扫**。中间价刚跳，我方还在按旧 fair value 报价，挂单
   立刻变成对手方的免费期权。
2. **被单个异常 tick 牵着走**。一笔异常成交把 mid 打歪，报价跟着追过去，
   下一 tick 又回来 —— 追价本身就是亏损。
3. **成交后立刻重挂**。刚被吃说明对手方有信息优势，此时马上在同一价位
   补挂等于把同一张牌再打一次。

对应五道保护:

- `mid_jump_pause`：单次 mid 跳变超过阈值 → 暂停该 token 报价一段时间；
- `stable_confirmation`：暂停结束后，要求连续若干次观测都落在窄带内才
  恢复报价，避免在震荡中途就回来；
- **滤波中间价**：用中位数（抗单点异常）叠加 EMA（抗抖动）产出报价锚点，
  而不是直接用原始 mid；
- `post_fill_cooldown`：成交后一段时间内不在该 token 重新报价；
- `max_chase_ticks`：单次报价移动上限，把"追价"这个动作本身限住。

**状态与线程**：Guard 是纯内存状态机，只被主循环的信号采集调用，不加锁
（和 `MakerStrategy` 的库存状态同一线程模型）。所有判断只依赖传入的
`now`，方便测试注入时间。
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Optional

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AntiSnipeConfig:
    enabled: bool = True
    # 中间价历史长度：中位数滤波的窗口。太长会让报价迟钝，5-9 比较合适。
    mid_history_size: int = 7
    # 阈值一律用 **tick** 而不是 bps：预测市场的价格跨越 0.01-0.99，
    # 同样 1 个 tick 在 0.50 是 200 bps、在 0.05 是 2000 bps。用 bps 设
    # 阈值会让低价市场永久暂停、高价市场形同虚设。
    # 单次 mid 相对上一次**原始** mid 跳变超过此值（tick）视为跳变。
    jump_pause_ticks: float = 3.0
    jump_pause_sec: float = 20.0
    # 暂停解除后要求连续 N 次观测的移动都在 stable_band_ticks 内。
    stable_ticks_required: int = 2
    stable_band_ticks: float = 1.0
    # EMA 平滑系数，越小越迟钝。0 表示关闭 EMA（只用中位数）。
    ema_alpha: float = 0.3
    use_median: bool = True
    post_fill_cooldown_sec: float = 15.0
    # 单次报价移动上限（tick）。0 表示不限制。
    max_chase_ticks: float = 2.0


@dataclass
class _TokenState:
    mids: collections.deque = field(default_factory=lambda: collections.deque(maxlen=7))
    filtered_mid: Optional[float] = None
    # 跳变检测必须对比**上一次原始 mid**，不能对比滤波值 —— 滤波值在
    # 跳变后会滞后很久，用它算跳幅会让 token 永久停在暂停态。
    last_raw_mid: Optional[float] = None
    paused_until: float = 0.0
    stable_streak: int = 0
    cooldown_until: float = 0.0
    last_bid: Optional[float] = None
    last_ask: Optional[float] = None


@dataclass(frozen=True)
class AntiSnipeDecision:
    allow: bool
    reason: str = ""
    filtered_mid: Optional[float] = None
    raw_mid: Optional[float] = None
    jump_ticks: float = 0.0

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "reason": self.reason,
            "filtered_mid": (
                round(self.filtered_mid, 6) if self.filtered_mid is not None else None
            ),
            "raw_mid": round(self.raw_mid, 6) if self.raw_mid is not None else None,
            "jump_ticks": round(self.jump_ticks, 3),
        }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


class AntiSnipeGuard:
    """按 token 维护抗狙击状态。`enabled=False` 时所有判断一律放行."""

    def __init__(self, config: AntiSnipeConfig | None = None) -> None:
        self._config = config or AntiSnipeConfig()
        self._states: dict[str, _TokenState] = {}
        self._stats = {
            "evaluated": 0,
            "blocked_jump": 0,
            "blocked_unstable": 0,
            "blocked_cooldown": 0,
            "chase_clamped": 0,
        }

    @property
    def config(self) -> AntiSnipeConfig:
        return self._config

    def _state(self, token_id: str) -> _TokenState:
        state = self._states.get(token_id)
        if state is None:
            state = _TokenState(
                mids=collections.deque(maxlen=max(1, self._config.mid_history_size))
            )
            self._states[token_id] = state
        return state

    def register_fill(self, token_id: str, now: float) -> None:
        """成交后开启冷却。刚被吃说明对手方可能有信息优势."""
        if not self._config.enabled or not token_id:
            return
        state = self._state(token_id)
        state.cooldown_until = max(
            state.cooldown_until, now + max(0.0, self._config.post_fill_cooldown_sec)
        )

    def evaluate(
        self, token_id: str, mid: float, now: float, *, tick_size: float = 0.01
    ) -> AntiSnipeDecision:
        """喂入一次 mid 观测，返回是否允许报价与滤波后的锚点.

        **有副作用**：每次调用都推进该 token 的状态机（历史、暂停窗口、
        稳定计数），所以每个 token 每个扫描周期只应调用一次。
        """
        cfg = self._config
        if not cfg.enabled:
            return AntiSnipeDecision(allow=True, filtered_mid=mid, raw_mid=mid)
        if mid is None or mid <= 0:
            return AntiSnipeDecision(allow=False, reason="invalid_mid")

        tick = max(1e-6, float(tick_size or 0.01))
        self._stats["evaluated"] += 1
        state = self._state(token_id)

        jump_ticks = 0.0
        if state.last_raw_mid is not None:
            jump_ticks = abs(float(mid) - state.last_raw_mid) / tick
        state.last_raw_mid = float(mid)

        state.mids.append(float(mid))
        filtered = self._filter(state, float(mid))
        state.filtered_mid = filtered

        # 1) 跳变 → 暂停
        if cfg.jump_pause_ticks > 0 and jump_ticks >= cfg.jump_pause_ticks:
            state.paused_until = max(
                now + max(0.0, cfg.jump_pause_sec), state.paused_until
            )
            state.stable_streak = 0
            self._stats["blocked_jump"] += 1
            LOG.debug(
                "抗狙击暂停: token=%s 跳变 %.1f tick", str(token_id)[:12], jump_ticks
            )
            return AntiSnipeDecision(
                allow=False,
                reason="mid_jump",
                filtered_mid=filtered,
                raw_mid=mid,
                jump_ticks=jump_ticks,
            )

        # 2) 成交冷却
        if now < state.cooldown_until:
            self._stats["blocked_cooldown"] += 1
            return AntiSnipeDecision(
                allow=False,
                reason="post_fill_cooldown",
                filtered_mid=filtered,
                raw_mid=mid,
                jump_ticks=jump_ticks,
            )

        # 3) 暂停期内 / 暂停刚结束需要稳定确认
        if now < state.paused_until:
            state.stable_streak = 0
            self._stats["blocked_jump"] += 1
            return AntiSnipeDecision(
                allow=False,
                reason="mid_jump_pause",
                filtered_mid=filtered,
                raw_mid=mid,
                jump_ticks=jump_ticks,
            )
        if state.paused_until > 0 and cfg.stable_ticks_required > 0:
            if jump_ticks <= cfg.stable_band_ticks:
                state.stable_streak += 1
            else:
                state.stable_streak = 0
            if state.stable_streak < cfg.stable_ticks_required:
                self._stats["blocked_unstable"] += 1
                return AntiSnipeDecision(
                    allow=False,
                    reason="awaiting_stable_mid",
                    filtered_mid=filtered,
                    raw_mid=mid,
                    jump_ticks=jump_ticks,
                )
            # 确认完成。**重新播种滤波器**：暂停期间 EMA 还停在跳变前的
            # 旧价位附近，直接拿它当锚点重新报价，等于在新行情下挂一个
            # 明显偏离的单 —— 保护措施反而制造了它要防的那次被吃。
            filtered = _median(list(state.mids)) if cfg.use_median else float(mid)
            state.filtered_mid = filtered
            state.paused_until = 0.0
            state.stable_streak = 0
            state.last_bid = None
            state.last_ask = None

        return AntiSnipeDecision(
            allow=True, filtered_mid=filtered, raw_mid=mid, jump_ticks=jump_ticks
        )

    def _filter(self, state: _TokenState, mid: float) -> float:
        cfg = self._config
        base = _median(list(state.mids)) if cfg.use_median and state.mids else mid
        if cfg.ema_alpha <= 0 or state.filtered_mid is None:
            return base
        alpha = min(1.0, float(cfg.ema_alpha))
        return alpha * base + (1.0 - alpha) * state.filtered_mid

    def clamp_chase(
        self,
        token_id: str,
        *,
        bid: Optional[float],
        ask: Optional[float],
        tick_size: float = 0.01,
    ) -> tuple[Optional[float], Optional[float]]:
        """限制本次报价相对上次的移动幅度，并记录新报价.

        追价本身就是亏损来源：报价跟着一个可能马上回撤的 mid 跑，等于
        不断在更差的价位重挂。这里把单次移动限死在 `max_chase_ticks` 内。
        """
        cfg = self._config
        state = self._state(token_id)
        if not cfg.enabled or cfg.max_chase_ticks <= 0:
            state.last_bid, state.last_ask = bid, ask
            return bid, ask

        limit = max(1e-6, float(tick_size or 0.01)) * cfg.max_chase_ticks
        new_bid = self._clamp_one(state.last_bid, bid, limit)
        new_ask = self._clamp_one(state.last_ask, ask, limit)
        if (new_bid, new_ask) != (bid, ask):
            self._stats["chase_clamped"] += 1
        state.last_bid, state.last_ask = new_bid, new_ask
        return new_bid, new_ask

    @staticmethod
    def _clamp_one(
        previous: Optional[float], proposed: Optional[float], limit: float
    ) -> Optional[float]:
        if proposed is None or previous is None or limit <= 0:
            return proposed
        delta = proposed - previous
        if abs(delta) <= limit:
            return proposed
        return previous + (limit if delta > 0 else -limit)

    def reset(self, token_id: str) -> None:
        self._states.pop(token_id, None)

    def stats(self) -> dict[str, int]:
        out = dict(self._stats)
        out["tracked_tokens"] = len(self._states)
        return out
