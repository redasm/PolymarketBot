"""钱包盈利质量评分（离线路径，不在扫描热路径上）.

现状问题:

`quant_input_scanner.discover_wallets_from_trades` 挑钱包的排序键是
`(交易笔数, 名义额)` —— 也就是"谁交易得多、下注得大"。这挑出来的是**最
活跃**的钱包，不是**最赚钱**的钱包。跟随一个高频亏钱的钱包，比不跟随
更糟。

本模块从 Data API `/closed-positions` 的 `realizedPnl` 算出四个维度:

  - **win_rate**：已平仓头寸里赚钱的比例；
  - **profit_factor**：总盈利 / 总亏损。>1 才是正期望，1.5 意味着每亏
    1 块能赚 1.5 块；
  - **consistency**：把已平仓头寸按时间等分成若干段，盈利段占比。
    只看总 PnL 会把"早期赚一票、之后一路亏"误判成好钱包；
  - **top_trade_share**：单笔盈利占总盈利的比例。一个钱包 90% 的利润
    来自一笔，那多半是运气而不是能力，跟随它没有可复制性。

**阈值来源与状态（重要）**：下面这组默认阈值取自公开参考实现
(MrFadiAi/Polymarket-bot 的 smart-money 过滤口径)，**没有在本项目的
数据上验证过**。因此:

  - 评分函数永远返回完整指标，即使不通过；
  - 过滤默认关闭（`WALLET_QUALITY_FILTER_ENABLED=false`），先把指标写
    进产物观测分布，再决定阈值。

这和 CLAUDE.md 的"没看过实际数据分布就不要给阈值建议"是一致的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

LOG = logging.getLogger(__name__)

# 参考实现的口径，未在本项目数据上验证。
DEFAULT_MIN_CLOSED_POSITIONS = 20
DEFAULT_MIN_WIN_RATE = 0.60
DEFAULT_MIN_PROFIT_FACTOR = 1.5
DEFAULT_MIN_TOTAL_PNL_USDC = 500.0
DEFAULT_MAX_TOP_TRADE_SHARE = 0.30
DEFAULT_MIN_CONSISTENCY = 0.70


@dataclass(frozen=True)
class WalletQualityThresholds:
    min_closed_positions: int = DEFAULT_MIN_CLOSED_POSITIONS
    min_win_rate: float = DEFAULT_MIN_WIN_RATE
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR
    min_total_pnl_usdc: float = DEFAULT_MIN_TOTAL_PNL_USDC
    max_top_trade_share: float = DEFAULT_MAX_TOP_TRADE_SHARE
    min_consistency: float = DEFAULT_MIN_CONSISTENCY


@dataclass(frozen=True)
class WalletQuality:
    """一个钱包的盈利质量画像。`passed` 之外的字段永远可读."""

    wallet_address: str
    closed_positions: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: Optional[float] = None
    total_pnl: float = 0.0
    top_trade_share: float = 0.0
    consistency: float = 0.0
    passed: bool = False
    reject_reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet_address": self.wallet_address,
            "closed_positions": self.closed_positions,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "gross_profit": round(self.gross_profit, 6),
            "gross_loss": round(self.gross_loss, 6),
            "profit_factor": (
                round(self.profit_factor, 4) if self.profit_factor is not None else None
            ),
            "total_pnl": round(self.total_pnl, 6),
            "top_trade_share": round(self.top_trade_share, 4),
            "consistency": round(self.consistency, 4),
            "passed": self.passed,
            "reject_reasons": list(self.reject_reasons),
        }


def _pnl_of(row: Any) -> Optional[float]:
    if not isinstance(row, dict):
        return None
    for key in ("realizedPnl", "realized_pnl", "cashPnl", "pnl"):
        if key in row:
            try:
                value = float(row[key])
            except (TypeError, ValueError):
                return None
            return None if value != value else value
    return None


def _timestamp_of(row: dict) -> float:
    for key in ("timestamp", "ts", "closedAt", "closed_at"):
        if key in row:
            try:
                value = float(row[key])
            except (TypeError, ValueError):
                continue
            if value != value or value <= 0:
                continue
            return value / 1000.0 if value > 1e11 else value
    return 0.0


def _period_consistency(pnls: list[float], *, max_buckets: int = 4) -> float:
    """按时间等分成若干段，返回**盈利段占比**.

    刻意不复用胜率：胜率已经单独是一道门槛，再用胜率算 consistency 只是
    把同一个指标卡两遍。这里要回答的是另一个问题 —— "这个钱包是一直在
    赚，还是早期赚了一票之后一路亏"。分段盈利占比能抓到后者，总 PnL
    抓不到。

    样本不足以分 4 段时自动降到 2 段、再到 1 段，避免每段只有一两笔时
    的噪声。
    """
    if not pnls:
        return 0.0
    buckets = max(1, min(int(max_buckets), len(pnls) // 5 or 1))
    size = len(pnls) / buckets
    profitable = 0
    for index in range(buckets):
        start = int(round(index * size))
        end = int(round((index + 1) * size)) if index < buckets - 1 else len(pnls)
        chunk = pnls[start:end]
        if chunk and sum(chunk) > 0:
            profitable += 1
    return profitable / buckets


def score_wallet_quality(
    wallet_address: str,
    closed_positions: list[dict],
    *,
    thresholds: WalletQualityThresholds | None = None,
) -> WalletQuality:
    """从已平仓头寸算出盈利质量画像（纯函数，无 IO）."""
    thresholds = thresholds or WalletQualityThresholds()

    rows = [row for row in closed_positions if isinstance(row, dict)]
    rows.sort(key=_timestamp_of)
    pnls = [pnl for pnl in (_pnl_of(row) for row in rows) if pnl is not None]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_pnl = sum(pnls)
    count = len(pnls)

    win_rate = (len(wins) / count) if count else 0.0
    profit_factor: Optional[float]
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        # 从没亏过：profit_factor 数学上是无穷，用 None 表示"无法定义"，
        # 由下面的样本量门槛决定它能不能过 —— 3 笔全赚不该拿满分。
        profit_factor = None
    else:
        profit_factor = 0.0

    top_trade_share = (max(wins) / gross_profit) if (wins and gross_profit > 0) else 0.0
    consistency = _period_consistency(pnls)

    reasons: list[str] = []
    if count < thresholds.min_closed_positions:
        reasons.append("too_few_closed_positions")
    if win_rate < thresholds.min_win_rate:
        reasons.append("win_rate_below_min")
    if profit_factor is not None and profit_factor < thresholds.min_profit_factor:
        reasons.append("profit_factor_below_min")
    if total_pnl < thresholds.min_total_pnl_usdc:
        reasons.append("total_pnl_below_min")
    if top_trade_share > thresholds.max_top_trade_share:
        # 利润高度集中在一笔 = 一击好运，不是可复制的能力。
        reasons.append("single_trade_concentration")
    if consistency < thresholds.min_consistency:
        reasons.append("inconsistent_across_periods")

    return WalletQuality(
        wallet_address=wallet_address,
        closed_positions=count,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
        top_trade_share=top_trade_share,
        consistency=consistency,
        passed=not reasons,
        reject_reasons=tuple(reasons),
    )


def rank_wallets_by_quality(
    qualities: list[WalletQuality],
    *,
    require_passed: bool = True,
    max_wallets: int = 50,
) -> list[WalletQuality]:
    """按质量排序。`require_passed=False` 时只排序不过滤（观测模式）.

    排序键刻意是 (profit_factor, total_pnl) 而不是笔数 —— 我们要的是
    赚得多且稳，不是交易得勤。profit_factor 为 None（从没亏过）的钱包
    按样本量决定：样本足够就排在最前，不足则退到最后，避免"3 笔全赚"
    压过"200 笔稳定盈利"。
    """
    pool = [q for q in qualities if q.passed] if require_passed else list(qualities)

    def _key(q: WalletQuality) -> tuple[float, float, float]:
        if q.profit_factor is None:
            pf = float("inf") if q.closed_positions >= DEFAULT_MIN_CLOSED_POSITIONS else 0.0
        else:
            pf = q.profit_factor
        return (pf, q.total_pnl, float(q.closed_positions))

    pool.sort(key=_key, reverse=True)
    return pool[: max(0, int(max_wallets))]
