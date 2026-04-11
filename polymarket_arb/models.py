"""数据模型：套利机会、订单簿快照、持仓、风险状态等."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ArbType(str, Enum):
    """套利类型."""

    BINARY = "binary"  # 二元市场 Yes+No < 1
    MULTI_OUTCOME = "multi_outcome"  # 多结果市场 sum(asks) < 1


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class OrderBookLevel:
    """订单簿单个价位."""

    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    """单个 token 的订单簿快照."""

    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    tick_size: float = 0.01
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def best_ask_size(self) -> float:
        if not self.asks:
            return 0.0
        return self.asks[0].size if self.asks else 0.0

    @property
    def best_bid_size(self) -> float:
        if not self.bids:
            return 0.0
        return self.bids[0].size if self.bids else 0.0


@dataclass
class MarketInfo:
    """从 Gamma API 获取的市场元数据."""

    condition_id: str
    question: str
    slug: str
    tokens: list[TokenInfo]
    active: bool = True
    closed: bool = False
    volume_24h: float = 0.0
    liquidity: float = 0.0
    event_id: str = ""
    event_slug: str = ""
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: list[float] = field(default_factory=list)
    neg_risk: bool = False
    end_date: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class TokenInfo:
    """市场中单个结果对应的 token."""

    token_id: str
    outcome: str
    price: float = 0.0
    winner: Optional[bool] = None


@dataclass
class EventInfo:
    """Polymarket 事件（可包含多个市场）."""

    event_id: str
    slug: str
    title: str
    markets: list[MarketInfo] = field(default_factory=list)
    active: bool = True
    closed: bool = False


@dataclass
class ArbOpportunity:
    """检测到的套利机会."""

    arb_type: ArbType
    event_id: str
    event_title: str
    markets: list[MarketInfo]
    total_cost: float  # 买入所有结果的总花费
    guaranteed_payout: float  # 保证的回收（通常 = 1.0）
    gross_edge: float  # 毛利 = payout - cost
    net_edge: float  # 净利 = gross - fees
    edge_pct: float  # 利润率 (%)
    legs: list[ArbLeg]  # 每条腿的详细信息
    timestamp: float = field(default_factory=time.time)
    max_executable_size: float = 0.0  # 可执行的最大数量（受流动性限制）
    confidence: float = 0.0  # 置信度 0-1

    @property
    def is_profitable(self) -> bool:
        return self.net_edge > 0


@dataclass
class ArbLeg:
    """套利交易的单条腿."""

    token_id: str
    condition_id: str
    outcome: str
    side: OrderSide
    price: float
    size: float
    available_size: float  # 该价位可用深度


@dataclass
class TradeRecord:
    """已执行交易的记录."""

    trade_id: str
    arb_id: str
    token_id: str
    condition_id: str
    side: OrderSide
    price: float
    size: float
    status: TradeStatus = TradeStatus.PENDING
    order_id: Optional[str] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None


@dataclass
class PositionSnapshot:
    """持仓快照."""

    token_id: str
    condition_id: str
    outcome: str
    size: float
    avg_price: float
    current_value: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class RiskState:
    """全局风险状态."""

    total_exposure: float = 0.0
    open_positions: int = 0
    daily_pnl: float = 0.0
    consecutive_failures: int = 0
    is_halted: bool = False
    halt_reason: str = ""
    positions: list[PositionSnapshot] = field(default_factory=list)

    def check_can_trade(
        self,
        max_positions: int,
        max_total_exposure: float,
        max_daily_loss: float,
        max_failures: int,
    ) -> tuple[bool, str]:
        """检查是否允许开新仓位."""
        if self.is_halted:
            return False, f"交易已暂停: {self.halt_reason}"
        if self.open_positions >= max_positions:
            return False, f"持仓数 {self.open_positions} 已达上限 {max_positions}"
        if self.total_exposure >= max_total_exposure:
            return False, f"总敞口 ${self.total_exposure:.2f} 已达上限 ${max_total_exposure:.2f}"
        if self.daily_pnl <= -max_daily_loss:
            return False, f"日亏损 ${abs(self.daily_pnl):.2f} 已触发止损线 ${max_daily_loss:.2f}"
        if self.consecutive_failures >= max_failures > 0:
            return False, f"连续失败 {self.consecutive_failures} 次，已达上限 {max_failures}"
        return True, ""


@dataclass
class AIDecision:
    """AI 决策引擎的输出."""

    action: str  # "BUY_YES" | "BUY_NO" | "SELL_YES" | "SELL_NO" | "HOLD" | "CLOSE"
    market_id: str
    confidence: float  # 0-1
    recommended_size_pct: float  # 占可用资金的比例 0-1
    reasoning: str
    risk_adjustment: dict = field(default_factory=dict)
    urgency: float = 0.5  # 0-1
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "market_id": self.market_id,
            "confidence": round(self.confidence, 3),
            "recommended_size_pct": round(self.recommended_size_pct, 3),
            "reasoning": self.reasoning,
            "risk_adjustment": self.risk_adjustment,
            "urgency": round(self.urgency, 3),
            "timestamp": self.timestamp,
        }


@dataclass
class MarketContext:
    """传递给 AI 决策引擎的市场上下文快照."""

    timestamp: float
    active_markets: list[dict] = field(default_factory=list)
    orderbook_summary: dict = field(default_factory=dict)
    volatility: dict = field(default_factory=dict)
    edge_signals: list[dict] = field(default_factory=list)
    recent_trades: list[dict] = field(default_factory=list)
    risk_state: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)


@dataclass
class FeeStructure:
    """Polymarket 手续费结构."""

    taker_fee_rate: float = 0.02  # 2% taker fee on winning outcome
    maker_rebate: float = 0.0

    def estimate_fee(self, cost: float, num_legs: int) -> float:
        """估算最坏情况的手续费（假设胜出方收 taker fee）。

        对于套利：买入所有结果中只有一个会胜出，
        所以 fee = taker_rate * payout_of_winning_leg。
        由于套利策略中所有结果都买入相同数量的份额，
        胜出方的 payout 固定为 $1.00/share。
        """
        return self.taker_fee_rate * 1.0  # 对单份额而言
