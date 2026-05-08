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
    DIRECTIONAL = "directional"  # 单腿方向性交易（T2 / AI）
    MARKET_MAKING = "market_making"  # 做市挂单


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
    event_title: str = ""
    event_ticker: str = ""
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
    execution_price: Optional[float] = None
    economic_cost: Optional[float] = None

    def __post_init__(self) -> None:
        if self.execution_price is None:
            self.execution_price = self.price
        if self.economic_cost is None:
            self.economic_cost = self.price


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
    economic_cost: Optional[float] = None
    rolled_back: bool = False
    simulated: bool = False
    post_only: bool = False
    order_type_name: Optional[str] = None
    inventory_accounted_size: float = 0.0


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
    last_portfolio_sync_ts: float = 0.0
    portfolio_sync_ok: bool = False
    portfolio_sync_error: str = ""
    portfolio_sync_consecutive_failures: int = 0

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
    research_overview: dict = field(default_factory=dict)
    research_signals: list[dict] = field(default_factory=list)


@dataclass
class ResearchSignal:
    """标准化后的研究信号摘要."""

    topic_id: str
    event_candidates: list[str] = field(default_factory=list)
    summary: str = ""
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.0
    freshness_sec: float = 0.0
    stance: str = "uncertain"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "event_candidates": list(self.event_candidates),
            "summary": self.summary,
            "sources": list(self.sources),
            "confidence": round(self.confidence, 3),
            "freshness_sec": round(self.freshness_sec, 1),
            "stance": self.stance,
            "metadata": dict(self.metadata),
        }


@dataclass
class ResearchSignalReport:
    """一轮 research signal 聚合后的结构化报告."""

    generated_at: float
    window_sec: int
    market_count: int
    row_count: int
    topic_count: int
    source_counts: dict[str, int] = field(default_factory=dict)
    signals: list[ResearchSignal] = field(default_factory=list)
    cache_hit: bool = False
    dropped_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "window_sec": self.window_sec,
            "market_count": self.market_count,
            "row_count": self.row_count,
            "topic_count": self.topic_count,
            "source_counts": dict(self.source_counts),
            "cache_hit": self.cache_hit,
            "dropped_rows": self.dropped_rows,
            "signals": [signal.to_dict() for signal in self.signals],
        }


@dataclass
class MarketSnapshotRow:
    """回测用市场快照行."""

    ts_ms: int
    condition_id: str
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_size: float = 0.0
    ask_size: float = 0.0


@dataclass
class OrderBookEventRow:
    """回测用订单簿事件."""

    ts_ms: int
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float
    event_type: str = "update"


@dataclass
class TradeEventRow:
    """回测用成交事件."""

    ts_ms: int
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float


@dataclass
class EventMetadataRow:
    """回测/研究共用的事件元数据."""

    event_id: str
    condition_id: str
    title: str
    slug: str = ""
    market_slug: str = ""
    outcomes: list[str] = field(default_factory=list)
    end_date: str = ""


@dataclass
class SimulatedExecution:
    """回测执行模型输出."""

    filled: bool
    filled_size: float
    average_price: Optional[float]
    fees_paid: float
    slippage_bps: float
    latency_ms: int
    notes: list[str] = field(default_factory=list)


@dataclass
class BacktestReport:
    """回测结果摘要."""

    strategy_name: str
    dataset_name: str
    total_signals: int = 0
    total_trades: int = 0
    filled_trades: int = 0
    skipped_signals: int = 0
    execution_model: str = ""
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    fill_rate: float = 0.0
    avg_slippage_bps: float = 0.0
    avg_latency_ms: float = 0.0
    avg_signal_edge_bps: float = 0.0
    total_fees_paid: float = 0.0
    total_notional_usdc: float = 0.0
    profit_factor: float | None = None
    profit_factor_infinite: bool = False
    notes: list[str] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "dataset_name": self.dataset_name,
            "total_signals": self.total_signals,
            "total_trades": self.total_trades,
            "filled_trades": self.filled_trades,
            "skipped_signals": self.skipped_signals,
            "execution_model": self.execution_model,
            "gross_pnl": round(self.gross_pnl, 4),
            "net_pnl": round(self.net_pnl, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "win_rate": round(self.win_rate, 4),
            "fill_rate": round(self.fill_rate, 4),
            "avg_slippage_bps": round(self.avg_slippage_bps, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "avg_signal_edge_bps": round(self.avg_signal_edge_bps, 2),
            "total_fees_paid": round(self.total_fees_paid, 4),
            "total_notional_usdc": round(self.total_notional_usdc, 4),
            "profit_factor": round(self.profit_factor, 4) if self.profit_factor is not None else None,
            "profit_factor_infinite": self.profit_factor_infinite,
            "notes": list(self.notes),
            "generated_at": self.generated_at,
        }


@dataclass
class FeeStructure:
    """Polymarket 手续费结构."""

    taker_fee_rate: float = 0.02
    maker_rebate: float = 0.0

    def estimate_fee(self, cost: float, num_legs: int) -> float:
        """兼容旧调用方的保守费用估算.

        Prefer estimate_price_fee()/estimate_leg_fees() for Polymarket CLOB
        markets. Official CLOB fees are proportional to p * (1 - p), not
        notional alone. Without individual leg prices we assume an even split.
        """
        if cost <= 0:
            return 0.0
        legs = max(1, int(num_legs or 1))
        price = max(0.0, min(1.0, float(cost) / legs))
        return self.estimate_price_fee(price, size=float(legs))

    def estimate_price_fee(self, price: float, size: float = 1.0) -> float:
        """Estimate CLOB taker fee for `size` shares at a binary-token price.

        Polymarket's CLOB fee shape is fee_rate * price * (1 - price) per
        share. The function clamps prices to [0, 1] so malformed orderbook
        rows cannot produce negative fees.
        """
        if size <= 0 or self.taker_fee_rate <= 0:
            return 0.0
        bounded = max(0.0, min(1.0, float(price)))
        return float(size) * float(self.taker_fee_rate) * bounded * (1.0 - bounded)

    def estimate_leg_fees(self, prices: list[float], size: float = 1.0) -> float:
        """Estimate total taker fees for several legs filled with same size."""
        return sum(self.estimate_price_fee(price, size=size) for price in prices)

    @classmethod
    def for_market(cls, default_taker_fee_rate: float, market: MarketInfo | None = None) -> "FeeStructure":
        """Build a fee structure using market metadata when available.

        Gamma/CLOB payloads can expose fees in slightly different shapes
        (`feeRateBps`, `base_fee`, `feeRate`, and friends). The config value
        remains a fallback so offline tests and cached datasets still work.
        """
        return cls(taker_fee_rate=resolve_polymarket_fee_rate(default_taker_fee_rate, market))


def resolve_polymarket_fee_rate(default_taker_fee_rate: float, market: MarketInfo | dict | None = None) -> float:
    """Resolve a decimal taker fee rate from market metadata.

    Returns a decimal rate (`0.02` for 2%). Bps-like fields are converted from
    basis points, while decimal-looking fields are used directly.

    Resolution order (newest Gamma fields first):
      1. `feesEnabled=false` → 0.0 (no fees on this market)
      2. `feeSchedule.rate` (decimal) — current Gamma format as of 2026-04
      3. `takerBaseFee` (interpreted as bps × 100; e.g. 1000 → 10 bps decimal scaled)
      4. Legacy `feeRateBps` / `baseFee` (basis points)
      5. Legacy `feeRate` / `takerFeeRate` (decimal or bps depending on magnitude)
      6. Fallback to the env-configured default.
    """
    raw: dict[str, Any] = {}
    if isinstance(market, MarketInfo):
        raw = dict(market.raw or {})
    elif isinstance(market, dict):
        raw = dict(market)

    fees_enabled = _first_present(raw, ("feesEnabled", "fees_enabled"))
    if fees_enabled is not None and not _coerce_bool(fees_enabled):
        return 0.0

    schedule = _first_present(raw, ("feeSchedule", "fee_schedule"))
    if isinstance(schedule, dict):
        rate_value = schedule.get("rate")
        rate = _coerce_non_negative_float(rate_value)
        if rate is not None:
            return rate / 10_000.0 if rate > 1.0 else rate

    taker_base = _first_present(raw, ("takerBaseFee", "taker_base_fee"))
    taker_base_value = _coerce_non_negative_float(taker_base)
    if taker_base_value is not None:
        # Polymarket's `takerBaseFee` field is denominated in 1/1_000_000 of
        # notional (ppm). 1000 ppm == 0.001 == 10 bps. We treat any value
        # >100 as ppm; smaller values are already a decimal rate.
        if taker_base_value > 100:
            return taker_base_value / 1_000_000.0
        return taker_base_value / 10_000.0 if taker_base_value > 1.0 else taker_base_value

    bps_value = _first_present(
        raw,
        (
            "feeRateBps",
            "fee_rate_bps",
            "baseFeeBps",
            "base_fee_bps",
            "base_fee",
            "baseFee",
        ),
    )
    bps = _coerce_non_negative_float(bps_value)
    if bps is not None:
        return bps / 10_000.0

    rate_value = _first_present(raw, ("feeRate", "fee_rate", "takerFeeRate", "taker_fee_rate"))
    rate = _coerce_non_negative_float(rate_value)
    if rate is not None:
        return rate / 10_000.0 if rate > 1.0 else rate

    return max(0.0, float(default_taker_fee_rate))


def _first_present(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _coerce_non_negative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text not in {"0", "false", "no", "off", "disabled"}
