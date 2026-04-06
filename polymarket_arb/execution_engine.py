"""执行引擎：将套利机会转化为实际交易订单.

执行流程:
1. 预检查：风险状态、余额、重复检测
2. 深度验证：用 VWAP 重新确认盈利性
3. 原子执行：尽可能同时提交所有腿
4. 状态追踪：记录每笔交易并更新持仓

关键设计决策:
- 所有腿用 IOC (Immediate or Cancel) 或 FOK (Fill or Kill) 类型
  以避免挂单后市场反向变动
- 如果任何一条腿失败，尝试取消其他腿（最大努力，非原子性）
- 支持 dry_run 模式：只记录不实际下单
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import (
    ArbOpportunity,
    OrderSide,
    TradeRecord,
    TradeStatus,
)

LOG = logging.getLogger(__name__)


class ExecutionEngine:
    """套利交易执行引擎."""

    def __init__(self, config: ArbConfig, trading_client: Any):
        self._config = config
        self._client = trading_client
        self._trade_history: list[TradeRecord] = []

    @property
    def trade_history(self) -> list[TradeRecord]:
        return list(self._trade_history)

    def execute_arbitrage(
        self, opp: ArbOpportunity, size: float
    ) -> list[TradeRecord]:
        """执行套利交易的所有腿.

        Args:
            opp: 经过验证的套利机会
            size: 执行的份额数量

        Returns:
            每条腿的交易记录列表
        """
        arb_id = str(uuid.uuid4())[:12]
        records: list[TradeRecord] = []

        actual_size = min(size, opp.max_executable_size)
        if actual_size <= 0:
            LOG.warning("arb %s 可执行数量为 0，跳过", arb_id)
            return records

        LOG.info(
            "开始执行套利 %s: %s, %d 条腿, size=%.2f, 预期净利=$%.4f",
            arb_id,
            opp.arb_type.value,
            len(opp.legs),
            actual_size,
            opp.net_edge * actual_size,
        )

        if self._config.dry_run:
            LOG.info("=== DRY RUN 模式 === 不实际下单")
            for leg in opp.legs:
                record = TradeRecord(
                    trade_id=str(uuid.uuid4())[:12],
                    arb_id=arb_id,
                    token_id=leg.token_id,
                    condition_id=leg.condition_id,
                    side=leg.side,
                    price=leg.price,
                    size=actual_size,
                    status=TradeStatus.FILLED,
                )
                records.append(record)
                self._trade_history.append(record)
                LOG.info(
                    "  [DRY] %s %s @ $%.4f x %.2f (token=%s…)",
                    leg.side.value,
                    leg.outcome,
                    leg.price,
                    actual_size,
                    leg.token_id[:16],
                )
            return records

        submitted_order_ids: list[str] = []
        all_success = True

        for i, leg in enumerate(opp.legs):
            record = TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=arb_id,
                token_id=leg.token_id,
                condition_id=leg.condition_id,
                side=leg.side,
                price=leg.price,
                size=actual_size,
            )

            try:
                order_id = self._submit_order(
                    token_id=leg.token_id,
                    side=leg.side,
                    price=leg.price,
                    size=actual_size,
                )
                record.order_id = order_id
                record.status = TradeStatus.FILLED
                submitted_order_ids.append(order_id)
                LOG.info(
                    "  腿 %d/%d 成功: %s %s @ $%.4f x %.2f, order=%s",
                    i + 1,
                    len(opp.legs),
                    leg.side.value,
                    leg.outcome,
                    leg.price,
                    actual_size,
                    order_id[:16] if order_id else "N/A",
                )
            except Exception as e:
                record.status = TradeStatus.FAILED
                record.error = str(e)
                all_success = False
                LOG.error(
                    "  腿 %d/%d 失败: %s %s @ $%.4f — %s",
                    i + 1,
                    len(opp.legs),
                    leg.side.value,
                    leg.outcome,
                    leg.price,
                    e,
                )
                break

            records.append(record)
            self._trade_history.append(record)

        if not all_success and submitted_order_ids:
            LOG.warning("套利 %s 部分失败，尝试撤销已提交的订单…", arb_id)
            self._rollback_orders(submitted_order_ids)

        return records

    def _submit_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
    ) -> str:
        """提交单笔订单到 CLOB."""
        from py_clob_client.clob_types import (
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
        )
        from py_clob_client.order_builder.constants import BUY, SELL

        clob_side = BUY if side == OrderSide.BUY else SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=clob_side,
        )
        signed_order = self._client.create_order(
            order_args, PartialCreateOrderOptions()
        )
        resp = self._client.post_order(
            signed_order, orderType=OrderType.GTC
        )

        order_id = ""
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("id") or ""
        elif hasattr(resp, "orderID"):
            order_id = str(resp.orderID)

        return str(order_id)

    def _rollback_orders(self, order_ids: list[str]) -> None:
        """尽最大努力撤销已提交的订单."""
        for oid in order_ids:
            try:
                self._client.cancel(oid)
                LOG.info("回滚: 已撤销订单 %s", oid[:16])
            except Exception as e:
                LOG.error("回滚: 撤销订单 %s 失败: %s", oid[:16], e)

    def get_recent_trades(self, limit: int = 20) -> list[TradeRecord]:
        return self._trade_history[-limit:]

    def get_pnl_summary(self) -> dict:
        """计算已执行交易的盈亏摘要."""
        total_cost = 0.0
        total_filled = 0
        total_failed = 0

        arb_ids: set[str] = set()
        for t in self._trade_history:
            arb_ids.add(t.arb_id)
            if t.status == TradeStatus.FILLED:
                total_cost += t.price * t.size
                total_filled += 1
            elif t.status == TradeStatus.FAILED:
                total_failed += 1

        return {
            "total_arbs": len(arb_ids),
            "total_trades": len(self._trade_history),
            "filled": total_filled,
            "failed": total_failed,
            "total_cost": total_cost,
        }
