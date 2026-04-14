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

import concurrent.futures
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import (
    ArbOpportunity,
    OrderSide,
    TradeRecord,
    TradeStatus,
)

LOG = logging.getLogger(__name__)
_PENDING_REMOTE_STATUSES = {
    "accepted",
    "live",
    "open",
    "pending",
    "queued",
    "resting",
    "submitted",
}
_PARTIAL_REMOTE_STATUSES = {
    "partially_filled",
    "partially-filled",
    "partial",
}
_FAILED_REMOTE_STATUSES = {
    "cancelled",
    "canceled",
    "failed",
    "killed",
    "rejected",
    "unmatched",
}


@dataclass
class OrderSubmissionResult:
    order_id: str
    trade_status: TradeStatus
    error: str = ""
    response: Any = None
    fill_size: float | None = None
    fill_price: float | None = None


class ExecutionEngine:
    """套利交易执行引擎."""

    def __init__(self, config: ArbConfig, trading_client: Any):
        self._config = config
        self._client = trading_client
        self._trade_history: list[TradeRecord] = []
        self._simulated_trade_history: list[TradeRecord] = []
        self._max_history = 2000
        self._execution_order_type = self._resolve_execution_order_type()
        self._gtc_order_type = self._resolve_named_order_type("GTC")

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
            "[cid=%s] 开始执行套利: %s, %d 条腿, size=%.2f, 预期净利=$%.4f",
            arb_id,
            opp.arb_type.value,
            len(opp.legs),
            actual_size,
            opp.net_edge * actual_size,
        )

        if self._config.dry_run:
            LOG.info("=== DRY RUN 模式 === 使用盘口深度模拟成交")
            return self._simulate_dry_run_arbitrage(opp, arb_id, actual_size)

        records = self._submit_legs_parallel(opp, arb_id, actual_size)
        all_success = len(records) == len(opp.legs) and all(r.status == TradeStatus.FILLED for r in records)

        cancellable_order_ids = [
            record.order_id for record in records
            if record.order_id and record.status in (TradeStatus.PENDING, TradeStatus.PARTIAL, TradeStatus.FAILED)
        ]
        if not all_success and cancellable_order_ids:
            LOG.warning("[cid=%s] 套利部分失败，尝试撤销已提交的订单…", arb_id)
            cancelled_order_ids = self._rollback_orders(cancellable_order_ids)
            if cancelled_order_ids:
                for record in records:
                    if record.order_id in cancelled_order_ids:
                        record.status = TradeStatus.CANCELLED
                        record.rolled_back = True

        if not all_success:
            for record in records:
                if record.status == TradeStatus.FILLED:
                    record.error = "hedge_incomplete"

        return records

    def is_successful_execution(
        self, opp: ArbOpportunity, trades: list[TradeRecord]
    ) -> bool:
        return (
            len(trades) == len(opp.legs)
            and all(t.status == TradeStatus.FILLED for t in trades)
        )

    def _resolve_execution_order_type(self) -> Any:
        if self._config.dry_run:
            return "FOK"

        for candidate in ("FOK", "FAK", "GTC"):
            resolved = self._resolve_named_order_type(candidate)
            if resolved is not None:
                return resolved
        raise ValueError("py_clob_client OrderType 缺少 FOK/FAK/GTC，无法初始化 ExecutionEngine")

    def _resolve_named_order_type(self, name: str) -> Any:
        if self._config.dry_run:
            return name
        from py_clob_client.clob_types import OrderType

        return getattr(OrderType, name, None)

    def _submit_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
        *,
        order_type: Any | None = None,
        post_only: bool = False,
    ) -> OrderSubmissionResult:
        """提交单笔订单到 CLOB."""
        from py_clob_client.clob_types import (
            OrderArgs,
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
        execution_type = order_type if order_type is not None else self._execution_order_type
        LOG.debug("提交套利腿使用订单类型: %s", execution_type)
        resp = self._client.post_order(
            signed_order, orderType=execution_type, post_only=post_only
        )

        order_id = ""
        success = False
        remote_status = ""
        error_msg = ""
        fill_size: float | None = None
        fill_price: float | None = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("id") or ""
            success = bool(resp.get("success", not resp.get("error")))
            remote_status = str(resp.get("status") or "").lower()
            error_msg = str(resp.get("errorMsg") or resp.get("error") or "")
            fill_size = _coerce_fill_field(
                resp.get("fillSize") or resp.get("filledSize") or resp.get("matchedAmount") or resp.get("sizeMatched")
            )
            fill_price = _coerce_fill_field(
                resp.get("fillPrice") or resp.get("avgPrice") or resp.get("averagePrice") or resp.get("matchedPrice")
            )
        elif hasattr(resp, "orderID"):
            order_id = str(resp.orderID)
            success = bool(getattr(resp, "success", True))
            remote_status = str(getattr(resp, "status", "")).lower()
            error_msg = str(getattr(resp, "errorMsg", "") or getattr(resp, "error", ""))
            fill_size = _coerce_fill_field(
                getattr(resp, "fillSize", None)
                or getattr(resp, "filledSize", None)
                or getattr(resp, "matchedAmount", None)
                or getattr(resp, "sizeMatched", None)
            )
            fill_price = _coerce_fill_field(
                getattr(resp, "fillPrice", None)
                or getattr(resp, "avgPrice", None)
                or getattr(resp, "averagePrice", None)
                or getattr(resp, "matchedPrice", None)
            )

        if success and remote_status in {"matched", "filled"}:
            trade_status = TradeStatus.FILLED
        elif success and remote_status in _PARTIAL_REMOTE_STATUSES:
            trade_status = TradeStatus.PARTIAL
        elif success and remote_status in _FAILED_REMOTE_STATUSES:
            trade_status = TradeStatus.FAILED
        elif success and remote_status in _PENDING_REMOTE_STATUSES:
            trade_status = TradeStatus.PENDING
        elif success and execution_type != self._gtc_order_type:
            trade_status = TradeStatus.PENDING if order_id else TradeStatus.FAILED
        elif success and remote_status:
            trade_status = TradeStatus.PENDING
        elif success and order_id:
            trade_status = TradeStatus.PENDING
        else:
            trade_status = TradeStatus.FAILED

        return OrderSubmissionResult(
            order_id=str(order_id),
            trade_status=trade_status,
            error=error_msg,
            response=resp,
            fill_size=fill_size,
            fill_price=fill_price,
        )

    def _rollback_orders(self, order_ids: list[str]) -> set[str]:
        """尽最大努力撤销已提交的订单."""
        cancelled: set[str] = set()
        for oid in order_ids:
            try:
                self._client.cancel(oid)
                LOG.info("回滚: 已撤销订单 %s", oid[:16])
                cancelled.add(oid)
            except (AttributeError, RuntimeError, ValueError) as e:
                LOG.error("回滚: 撤销订单 %s 失败: %s", oid[:16], e)
        return cancelled

    def submit_limit_order(
        self,
        *,
        token_id: str,
        condition_id: str,
        outcome: str,
        side: OrderSide,
        price: float,
        size: float,
        arb_id: str | None = None,
        post_only: bool = False,
        order_type_name: str = "GTC",
    ) -> TradeRecord:
        """提交单笔限价单，供做市/单腿策略复用."""
        arb_ref = arb_id or str(uuid.uuid4())[:12]
        trade = TradeRecord(
            trade_id=str(uuid.uuid4())[:12],
            arb_id=arb_ref,
            token_id=token_id,
            condition_id=condition_id,
            side=side,
            price=float(price),
            size=float(size),
            economic_cost=float(price),
        )

        if size <= 0 or price <= 0:
            trade.status = TradeStatus.FAILED
            trade.error = "invalid_order_args"
            self._append_trade_record(trade, simulated=self._config.dry_run)
            return trade

        if self._config.dry_run:
            trade.status = TradeStatus.PENDING if post_only else TradeStatus.FILLED
            trade.simulated = True
            if trade.status == TradeStatus.FILLED:
                trade.fill_price = float(price)
                trade.fill_size = float(size)
            self._append_trade_record(trade, simulated=True)
            return trade

        try:
            submission = self._submit_order(
                token_id,
                side,
                float(price),
                float(size),
                order_type=self._resolve_named_order_type(order_type_name),
                post_only=post_only,
            )
            trade.order_id = submission.order_id
            trade.status = submission.trade_status
            trade.error = submission.error or ""
            if submission.fill_price is not None:
                trade.fill_price = submission.fill_price
            if submission.fill_size is not None:
                trade.fill_size = submission.fill_size
            if trade.status == TradeStatus.FILLED and trade.fill_price is None:
                trade.fill_price = float(price)
            if trade.status == TradeStatus.FILLED and trade.fill_size is None:
                trade.fill_size = float(size)
        except Exception as exc:  # pragma: no cover - defensive around client transport
            trade.status = TradeStatus.FAILED
            trade.error = str(exc)
        self._append_trade_record(trade)
        return trade

    def ensure_sufficient_collateral(self, required_notional: float) -> tuple[bool, str, float | None]:
        """在 live 模式下检查可用 collateral 是否足够."""
        if self._config.dry_run:
            return True, "", None
        if required_notional <= 0:
            return False, "required_notional_invalid", None

        available = self.get_available_collateral_balance()
        if available is None:
            return False, "balance_check_unavailable", None
        if available + 1e-9 < required_notional:
            return False, f"insufficient_balance available={available:.4f} required={required_notional:.4f}", available
        return True, "", available

    def get_available_collateral_balance(self) -> float | None:
        """读取可用 USDC 余额（余额与 allowance 的较小值）."""
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        except Exception:
            return self._parse_balance_response(
                getattr(self._client, "get_balance_allowance", lambda *args, **kwargs: None)()
            )

        try:
            response = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
        except TypeError:
            try:
                response = self._client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=-1)
                )
            except Exception:
                return None
        except Exception:
            return None
        return self._parse_balance_response(response)

    def get_recent_trades(self, limit: int = 20, *, include_simulated: bool = False) -> list[TradeRecord]:
        history = self._trade_history if not include_simulated else self._trade_history + self._simulated_trade_history
        return history[-limit:]

    def get_pnl_summary(self, *, include_simulated: bool = False) -> dict:
        """计算已执行交易的盈亏摘要."""
        total_cost = 0.0
        total_filled = 0
        total_failed = 0

        arb_ids: set[str] = set()
        history = self._trade_history if not include_simulated else self._trade_history + self._simulated_trade_history
        for t in history:
            arb_ids.add(t.arb_id)
            if t.status == TradeStatus.FILLED:
                leg_cost = t.economic_cost if t.economic_cost is not None else t.price
                total_cost += leg_cost * t.size
                total_filled += 1
            elif t.status == TradeStatus.FAILED:
                total_failed += 1

        return {
            "total_arbs": len(arb_ids),
            "total_trades": len(history),
            "filled": total_filled,
            "failed": total_failed,
            "total_cost": total_cost,
        }

    def _submit_legs_parallel(
        self,
        opp: ArbOpportunity,
        arb_id: str,
        actual_size: float,
    ) -> list[TradeRecord]:
        records = [
            TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=arb_id,
                token_id=leg.token_id,
                condition_id=leg.condition_id,
                side=leg.side,
                price=leg.execution_price if leg.execution_price is not None else leg.price,
                size=actual_size,
                economic_cost=leg.economic_cost if leg.economic_cost is not None else leg.price,
            )
            for leg in opp.legs
        ]
        if len(records) != len(opp.legs):
            raise RuntimeError("套利腿与交易记录数量不一致")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(opp.legs)) as pool:
            future_map = {
                pool.submit(
                    self._submit_order,
                    leg.token_id,
                    leg.side,
                    record.price,
                    actual_size,
                ): (index, leg, record)
                for index, (leg, record) in enumerate(zip(opp.legs, records, strict=True))
            }
            for future in concurrent.futures.as_completed(future_map):
                index, leg, record = future_map[future]
                try:
                    submission = future.result()
                    record.order_id = submission.order_id
                    record.status = submission.trade_status
                    record.error = submission.error or ""
                    if submission.trade_status == TradeStatus.FILLED:
                        record.fill_price = submission.fill_price if submission.fill_price is not None else record.price
                        record.fill_size = submission.fill_size if submission.fill_size is not None else actual_size
                        LOG.info(
                            "  腿 %d/%d 成功: %s %s @ $%.4f x %.2f, order=%s",
                            index + 1,
                            len(opp.legs),
                            leg.side.value,
                            leg.outcome,
                            record.price,
                            actual_size,
                            submission.order_id[:16] if submission.order_id else "N/A",
                        )
                    elif submission.trade_status == TradeStatus.PARTIAL:
                        record.fill_price = submission.fill_price if submission.fill_price is not None else record.price
                        record.fill_size = submission.fill_size if submission.fill_size is not None else None
                        LOG.warning(
                            "  腿 %d/%d 部分成交: %s %s @ $%.4f filled=%.2f, order=%s",
                            index + 1,
                            len(opp.legs),
                            leg.side.value,
                            leg.outcome,
                            record.fill_price,
                            record.fill_size or 0.0,
                            submission.order_id[:16] if submission.order_id else "N/A",
                        )
                    else:
                        LOG.warning(
                            "  腿 %d/%d 未确认全成: %s %s @ $%.4f, status=%s, order=%s",
                            index + 1,
                            len(opp.legs),
                            leg.side.value,
                            leg.outcome,
                            record.price,
                            record.status.value,
                            submission.order_id[:16] if submission.order_id else "N/A",
                        )
                except (RuntimeError, ValueError, TypeError, AttributeError, ImportError) as e:
                    record.status = TradeStatus.FAILED
                    record.error = str(e)
                    LOG.error(
                        "  腿 %d/%d 失败: %s %s @ $%.4f — %s",
                        index + 1,
                        len(opp.legs),
                        leg.side.value,
                        leg.outcome,
                        record.price,
                        e,
                    )

        for record in records:
            self._append_trade_record(record)
        return records

    def _append_trade_record(self, record: TradeRecord, *, simulated: bool = False) -> None:
        target = self._simulated_trade_history if simulated else self._trade_history
        target.append(record)
        if len(target) > self._max_history:
            del target[:-self._max_history]

    def _simulate_dry_run_arbitrage(
        self,
        opp: ArbOpportunity,
        arb_id: str,
        actual_size: float,
    ) -> list[TradeRecord]:
        records: list[TradeRecord] = []
        for leg in opp.legs:
            fill_size = max(0.0, min(actual_size, float(leg.available_size or 0.0)))
            if fill_size >= actual_size - 1e-9:
                status = TradeStatus.FILLED
            elif fill_size > 0:
                status = TradeStatus.PARTIAL
            else:
                status = TradeStatus.FAILED

            record = TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=arb_id,
                token_id=leg.token_id,
                condition_id=leg.condition_id,
                side=leg.side,
                price=leg.execution_price if leg.execution_price is not None else leg.price,
                size=actual_size,
                status=status,
                fill_price=(leg.execution_price if leg.execution_price is not None else leg.price) if fill_size > 0 else None,
                fill_size=fill_size if fill_size > 0 else None,
                economic_cost=leg.economic_cost if leg.economic_cost is not None else leg.price,
                simulated=True,
                error="simulated_insufficient_depth" if status != TradeStatus.FILLED else None,
            )
            records.append(record)
            self._append_trade_record(record, simulated=True)
            LOG.info(
                "  [DRY] %s %s @ $%.4f target=%.2f filled=%.2f status=%s (token=%s…)",
                leg.side.value,
                leg.outcome,
                record.price,
                actual_size,
                fill_size,
                record.status.value,
                leg.token_id[:16],
            )
        return records

    def _parse_balance_response(self, response: Any) -> float | None:
        if response is None:
            return None
        if isinstance(response, (int, float)):
            return float(response)
        if not isinstance(response, dict):
            return None

        payload = response
        for key in ("balanceAllowance", "data", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                payload = nested
                break

        direct_available = _coerce_fill_field(
            payload.get("available")
            or payload.get("available_balance")
            or payload.get("availableBalance")
            or payload.get("buying_power")
            or payload.get("buyingPower")
        )
        if direct_available is not None:
            return direct_available

        balance = _coerce_fill_field(
            payload.get("balance")
            or payload.get("balance_decimal")
            or payload.get("balanceDecimal")
        )
        allowance = _coerce_fill_field(
            payload.get("allowance")
            or payload.get("allowance_decimal")
            or payload.get("allowanceDecimal")
        )
        if balance is not None and allowance is not None:
            return min(balance, allowance)
        return balance if balance is not None else allowance


def _coerce_fill_field(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
