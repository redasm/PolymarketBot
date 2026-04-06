"""跨平台套利：Polymarket vs Kalshi vs 其他预测市场.

这是利润最丰厚的策略类型，原因:
1. 不同平台的用户群体不同 → 信息不对称 → 定价偏差
2. 跨平台的做市商竞争弱于单平台内部
3. 偏差修正速度慢（需要资金在平台间转移，有延迟和摩擦）

策略:
  同一事件在 Polymarket 的隐含概率 = P_poly
  同一事件在 Kalshi 的隐含概率 = P_kalshi

  如果 P_poly + (1 - P_kalshi) < 1.0:
    → 在 Polymarket 买 Yes + 在 Kalshi 买 No（等价于 Kalshi 卖 Yes）
    → 无论结果如何，净收入 > 净支出

  实际上更常见的情况是:
  P_poly 和 P_kalshi 有显著偏差（如 5%+），但不一定形成无风险套利。
  这时可以用统计套利：在便宜的平台买，在贵的平台卖。

风险:
  - 结算规则差异：两个平台对同一事件的定义/结算可能不同
  - 资金锁定：Polymarket 用 USDC, Kalshi 用 USD，资金不能即时转移
  - 对手方风险：平台可能出问题
  - 时间风险：市场关闭时间不同

实现:
  - 事件匹配：用 NLP 或手动映射将两平台的事件对齐
  - 价格监控：同时监控两平台价格
  - 综合定价：取两平台价格的加权平均作为 "fair value"
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

LOG = logging.getLogger(__name__)

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass
class CrossPlatformPair:
    """跨平台事件配对."""

    pair_id: str
    event_description: str
    polymarket_condition_id: str
    polymarket_token_id_yes: str
    polymarket_slug: str
    kalshi_ticker: str
    kalshi_event_ticker: str

    poly_yes_price: float = 0.0
    poly_no_price: float = 0.0
    kalshi_yes_price: float = 0.0
    kalshi_no_price: float = 0.0

    last_updated: float = field(default_factory=time.time)


@dataclass
class CrossPlatformOpportunity:
    """跨平台套利机会."""

    pair: CrossPlatformPair
    direction: str  # "poly_yes_kalshi_no" 或 "poly_no_kalshi_yes"
    poly_cost: float
    kalshi_cost: float
    total_cost: float
    gross_edge: float
    net_edge: float
    edge_pct: float
    confidence: float


class KalshiClient:
    """Kalshi 公开 API 客户端（只读，用于价格查询）."""

    def __init__(self, base_url: str = KALSHI_API_BASE):
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def get_market(self, ticker: str) -> Optional[dict]:
        """获取单个市场的当前价格."""
        try:
            resp = self._session.get(f"{self._base}/markets/{ticker}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("market") or data
            LOG.warning("Kalshi get_market %s: HTTP %d", ticker, resp.status_code)
            return None
        except Exception as e:
            LOG.error("Kalshi API 错误: %s", e)
            return None

    def get_event_markets(self, event_ticker: str) -> list[dict]:
        """获取一个事件下的所有市场."""
        try:
            resp = self._session.get(
                f"{self._base}/events/{event_ticker}/markets",
                params={"limit": 50},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("markets") or []
            return []
        except Exception as e:
            LOG.error("Kalshi events API 错误: %s", e)
            return []

    def extract_prices(self, market: dict) -> tuple[float, float]:
        """从 Kalshi 市场数据中提取 Yes/No 价格.

        Returns:
            (yes_price, no_price) 归一化到 0-1 区间
        """
        yes_bid = float(market.get("yes_bid", 0) or 0) / 100.0
        yes_ask = float(market.get("yes_ask", 0) or 0) / 100.0
        no_bid = float(market.get("no_bid", 0) or 0) / 100.0
        no_ask = float(market.get("no_ask", 0) or 0) / 100.0

        yes_mid = (yes_bid + yes_ask) / 2.0 if yes_bid and yes_ask else yes_ask or yes_bid
        no_mid = (no_bid + no_ask) / 2.0 if no_bid and no_ask else no_ask or no_bid

        return (yes_mid, no_mid)


class CrossPlatformScanner:
    """扫描跨平台套利机会.

    使用流程:
    1. 配置事件配对表（手动或通过 NLP 自动匹配）
    2. 同时拉取两平台价格
    3. 检测是否存在跨平台套利
    """

    def __init__(self, kalshi: KalshiClient, poly_ob_analyzer: "OrderBookAnalyzer"):
        self._kalshi = kalshi
        self._poly_ob = poly_ob_analyzer
        self._pairs: list[CrossPlatformPair] = []

    def add_pair(self, pair: CrossPlatformPair) -> None:
        self._pairs.append(pair)

    def load_pairs_from_config(self, pairs_config: list[dict]) -> None:
        """从配置加载事件配对."""
        for cfg in pairs_config:
            pair = CrossPlatformPair(
                pair_id=cfg.get("pair_id", ""),
                event_description=cfg.get("description", ""),
                polymarket_condition_id=cfg.get("poly_condition_id", ""),
                polymarket_token_id_yes=cfg.get("poly_token_yes", ""),
                polymarket_slug=cfg.get("poly_slug", ""),
                kalshi_ticker=cfg.get("kalshi_ticker", ""),
                kalshi_event_ticker=cfg.get("kalshi_event", ""),
            )
            self._pairs.append(pair)

    def scan(self) -> list[CrossPlatformOpportunity]:
        """扫描所有配对的跨平台套利机会."""
        opportunities: list[CrossPlatformOpportunity] = []

        for pair in self._pairs:
            self._refresh_prices(pair)
            opp = self._check_pair(pair)
            if opp is not None:
                opportunities.append(opp)

        return opportunities

    def _refresh_prices(self, pair: CrossPlatformPair) -> None:
        """刷新配对的两平台价格."""
        poly_snap = self._poly_ob.get_snapshot(pair.polymarket_token_id_yes)
        if poly_snap and poly_snap.best_ask is not None:
            pair.poly_yes_price = poly_snap.best_ask
            pair.poly_no_price = 1.0 - (poly_snap.best_bid or 0)

        kalshi_market = self._kalshi.get_market(pair.kalshi_ticker)
        if kalshi_market:
            yes_p, no_p = self._kalshi.extract_prices(kalshi_market)
            pair.kalshi_yes_price = yes_p
            pair.kalshi_no_price = no_p

        pair.last_updated = time.time()

    def _check_pair(self, pair: CrossPlatformPair) -> Optional[CrossPlatformOpportunity]:
        """检查单个配对是否存在套利.

        两种方向:
        A) Poly买Yes + Kalshi买No: cost = poly_yes_ask + kalshi_no_ask
        B) Poly买No + Kalshi买Yes: cost = poly_no_ask + kalshi_yes_ask

        任一方向 cost < 1.0 (扣费后) → 套利
        """
        poly_fee_rate = 0.02
        kalshi_fee_rate = 0.0

        cost_a = pair.poly_yes_price + pair.kalshi_no_price
        cost_b = (1.0 - pair.poly_no_price) + pair.kalshi_yes_price

        for direction, cost in [("poly_yes_kalshi_no", cost_a), ("poly_no_kalshi_yes", cost_b)]:
            if cost <= 0 or cost >= 1.0:
                continue

            gross = 1.0 - cost
            fee = poly_fee_rate + kalshi_fee_rate
            net = gross - fee

            if net <= 0.005:
                continue

            edge_pct = (net / cost) * 100

            opp = CrossPlatformOpportunity(
                pair=pair,
                direction=direction,
                poly_cost=pair.poly_yes_price if "poly_yes" in direction else (1.0 - pair.poly_no_price),
                kalshi_cost=pair.kalshi_no_price if "kalshi_no" in direction else pair.kalshi_yes_price,
                total_cost=cost,
                gross_edge=gross,
                net_edge=net,
                edge_pct=edge_pct,
                confidence=min(1.0, edge_pct / 10.0),
            )
            LOG.info(
                "跨平台机会: %s | %s | poly=%.2f kalshi=%.2f | edge=%.2f%%",
                pair.event_description,
                direction,
                opp.poly_cost,
                opp.kalshi_cost,
                edge_pct,
            )
            return opp

        return None


def format_cross_platform_zh(opp: CrossPlatformOpportunity) -> str:
    lines = [
        "🌐 跨平台套利机会",
        f"事件: {opp.pair.event_description}",
        f"方向: {opp.direction}",
        f"Polymarket 成本: ${opp.poly_cost:.4f}",
        f"Kalshi 成本: ${opp.kalshi_cost:.4f}",
        f"总成本: ${opp.total_cost:.4f}",
        f"净利: ${opp.net_edge:.4f} ({opp.edge_pct:.2f}%)",
    ]
    return "\n".join(lines)
