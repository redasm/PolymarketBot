"""UPDOWN 市场 spot-anchored 定价 (T2 UPDOWN Phase 2).

把三件事串起来,且与网络 IO 解耦 (便于单测):
  1. 解析 UPDOWN slug `{sym}-updown-{w}m-{slot}` -> (symbol, window_sec, slot)
  2. 从注入的 spot provider (鸭子类型: get_spot / get_sigma_15m / ref_tracker)
     取 s_now / sigma / ref_px
  3. 调 `fair_value_model.compute_fair_updown` 得 GBM 公允概率

provider 协议 (BinanceSpotFeed 即满足):
  - get_spot(symbol) -> float | None
  - get_sigma_15m(symbol) -> float | None
  - ref_tracker.get_ref(symbol, window_sec, slot) -> float | None

为什么独立于 BayesianPriceModel:
  普通 T2 市场的 confidence 来自 OBI/动量信号强度,而 UPDOWN 在 15m 薄簿上
  OBI/动量基本是噪声。所以 UPDOWN 不走贝叶斯 blend,而是用 GBM fair 直接作
  model_prob,confidence 由模型数据质量给 (落在 T2 文档区间 0.55-0.70)。
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from polymarket_arb.fair_value_model import compute_fair_updown

LOG = logging.getLogger(__name__)

# btc-updown-15m-1780272000  /  eth-up-down-5m-... 等变体
_SLUG_RE = re.compile(r"(?P<sym>[a-z0-9]+)-up-?down-(?P<win>\d+)m-(?P<slot>\d+)")

# confidence 区间 (与 CLAUDE.md "T2 uses 0.55-0.70" 一致)
_CONF_MIN = 0.55
_CONF_MAX = 0.70


@dataclass(frozen=True)
class UpdownSlug:
    symbol: str
    window_sec: int
    slot: int


@dataclass(frozen=True)
class UpdownFairValue:
    ok: bool
    reason: str = ""
    fair_up: Optional[float] = None
    fair_down: Optional[float] = None
    z_score: Optional[float] = None
    confidence: float = 0.0
    tau_sec: Optional[float] = None
    ref_px: Optional[float] = None
    s_now: Optional[float] = None
    sigma_15m: Optional[float] = None
    symbol: str = ""
    window_sec: int = 0
    slot: int = 0


def parse_updown_slug(slug: str) -> Optional[UpdownSlug]:
    """从 slug / event_slug 解析 (symbol, window_sec, slot);失败返回 None."""
    if not slug:
        return None
    m = _SLUG_RE.search(slug.lower())
    if not m:
        return None
    try:
        return UpdownSlug(
            symbol=m.group("sym"),
            window_sec=int(m.group("win")) * 60,
            slot=int(m.group("slot")),
        )
    except (ValueError, TypeError):
        return None


def _confidence_from_z(z_score: Optional[float]) -> float:
    """由 |z| 映射到 [_CONF_MIN, _CONF_MAX].

    |z| 越大表示模型方向性越强 -> confidence 越高,但封顶 0.70 (T2 上限)。
    z=0 (纯 50/50) 仍给下限 0.55: 模型对"就是 50/50"也是有把握的。
    """
    if z_score is None:
        return _CONF_MIN
    az = abs(float(z_score))
    # tanh 平滑饱和: |z|~1.5 时接近上限
    frac = math.tanh(az / 1.5)
    return round(_CONF_MIN + (_CONF_MAX - _CONF_MIN) * frac, 4)


class UpdownPricer:
    """对单个 UPDOWN 市场做 spot-anchored GBM 定价.

    Args:
        spot_feed: 满足 get_spot/get_sigma_15m/ref_tracker 协议的对象
        min_tau_sec: tau 小于此值时拒绝定价 (临近结算噪声大、滑点风险高)
        window_sec_default: slug 无法解析窗口时的回退 (默认 900)
    """

    def __init__(
        self,
        spot_feed: Any,
        *,
        min_tau_sec: float = 30.0,
        window_sec_default: int = 900,
    ) -> None:
        self._feed = spot_feed
        self._min_tau_sec = float(min_tau_sec)
        self._window_sec_default = int(window_sec_default)

    def price(
        self,
        *,
        slug: str,
        event_slug: str = "",
        now_sec: Optional[float] = None,
    ) -> UpdownFairValue:
        if now_sec is None:
            now_sec = time.time()
        parsed = parse_updown_slug(slug) or parse_updown_slug(event_slug)
        if parsed is None:
            return UpdownFairValue(ok=False, reason="updown_slug_unparsed")

        sym, window_sec, slot = parsed.symbol, parsed.window_sec, parsed.slot
        s_now = self._feed.get_spot(sym)
        if s_now is None or s_now <= 0:
            return UpdownFairValue(ok=False, reason="updown_no_spot", symbol=sym,
                                   window_sec=window_sec, slot=slot)
        ref_px = self._feed.ref_tracker.get_ref(sym, window_sec, slot)
        if ref_px is None or ref_px <= 0:
            return UpdownFairValue(ok=False, reason="updown_no_ref", symbol=sym,
                                   window_sec=window_sec, slot=slot, s_now=s_now)
        sigma = self._feed.get_sigma_15m(sym)
        if sigma is None or sigma <= 0:
            return UpdownFairValue(ok=False, reason="updown_sigma_not_ready", symbol=sym,
                                   window_sec=window_sec, slot=slot, s_now=s_now, ref_px=ref_px)

        tau_sec = (slot + window_sec) - now_sec
        if tau_sec < self._min_tau_sec:
            return UpdownFairValue(ok=False, reason="updown_tau_too_small", symbol=sym,
                                   window_sec=window_sec, slot=slot, s_now=s_now,
                                   ref_px=ref_px, sigma_15m=sigma, tau_sec=tau_sec)

        result = compute_fair_updown(
            s_now=s_now,
            ref_px=ref_px,
            sigma_15m=sigma,
            tau_sec=tau_sec,
            window_sec=float(window_sec),
        )
        z = result.get("z_score")
        return UpdownFairValue(
            ok=True,
            reason="ok",
            fair_up=result.get("fair_up"),
            fair_down=result.get("fair_down"),
            z_score=z,
            confidence=_confidence_from_z(z),
            tau_sec=tau_sec,
            ref_px=ref_px,
            s_now=s_now,
            sigma_15m=sigma,
            symbol=sym,
            window_sec=window_sec,
            slot=slot,
        )


