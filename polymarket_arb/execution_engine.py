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
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from math import gcd
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import (
    ArbOpportunity,
    OrderSide,
    TradeRecord,
    TradeStatus,
)

LOG = logging.getLogger(__name__)
_PRICE_QUANT = Decimal("0.01")
_SIZE_QUANT = Decimal("0.00001")
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


@dataclass
class OrderSyncResult:
    polled: list[TradeRecord]
    changed: list[TradeRecord]


@dataclass
class RollbackResult:
    """Best-effort cancel outcome split by failure mode.

    `cancelled` — order acknowledged as cancelled by the remote CLOB.
    `transient_failed` — network/transport error; remote order may still be
    live, caller MUST keep the trade in PENDING and trigger a forced
    portfolio reconcile so the next risk check sees ground truth.
    """

    cancelled: set[str] = field(default_factory=set)
    transient_failed: set[str] = field(default_factory=set)


class ExecutionEngine:
    """套利交易执行引擎."""

    _BALANCE_CACHE_TTL_SEC = 5.0

    def __init__(self, config: ArbConfig, trading_client: Any):
        self._config = config
        self._client = trading_client
        self._trade_history: list[TradeRecord] = []
        self._simulated_trade_history: list[TradeRecord] = []
        self._max_history = 2000
        self._execution_order_type = self._resolve_execution_order_type()
        self._gtc_order_type = self._resolve_named_order_type("GTC")
        self._balance_cache: float | None = None
        self._balance_cache_ts: float = 0.0
        # Set whenever a rollback hits a transient (network) failure, so the
        # main loop can force a portfolio sync before approving new trades.
        # Cleared by `consume_force_portfolio_resync()`.
        self._force_portfolio_resync_ts: float | None = None

    @property
    def trade_history(self) -> list[TradeRecord]:
        return list(self._trade_history)

    def execute_arbitrage(
        self,
        opp: ArbOpportunity,
        size: float,
        *,
        order_type_name: str | None = None,
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

        order_type = self._resolve_named_order_type(order_type_name) if order_type_name else None
        records = self._submit_legs_parallel(opp, arb_id, actual_size, order_type=order_type)
        all_success = len(records) == len(opp.legs) and all(r.status == TradeStatus.FILLED for r in records)

        cancellable_order_ids = [
            record.order_id for record in records
            if record.order_id and record.status in (TradeStatus.PENDING, TradeStatus.PARTIAL, TradeStatus.FAILED)
        ]
        if not all_success and cancellable_order_ids:
            LOG.warning("[cid=%s] 套利部分失败，尝试撤销已提交的订单…", arb_id)
            outcome = self._rollback_orders(cancellable_order_ids)
            cancelled_ids, transient_ids = _coerce_rollback_outcome(outcome)
            if cancelled_ids:
                for record in records:
                    if record.order_id in cancelled_ids:
                        record.status = TradeStatus.CANCELLED
                        record.rolled_back = True
            if transient_ids:
                # Transient (network) cancel failures: order may still be live
                # on the CLOB. Keep it in PENDING so reconcile_pending_order_statuses
                # can poll it next cycle, append a tag for forensics, and arm
                # a forced portfolio resync before any new trade approval.
                for record in records:
                    if record.order_id in transient_ids:
                        record.status = TradeStatus.PENDING
                        record.error = (record.error + "|" if record.error else "") + "rollback_transient_failed"
                self._force_portfolio_resync_ts = time.time()
                LOG.error(
                    "[cid=%s] 部分腿撤单失败 (transport): order_ids=%s — 已请求 portfolio 强制同步",
                    arb_id,
                    sorted(transient_ids),
                )

        if not all_success:
            for record in records:
                if record.status == TradeStatus.FILLED:
                    record.error = "hedge_incomplete"
            records.extend(self._auto_flatten_incomplete_fills(opp, arb_id, records))

        return records

    def consume_force_portfolio_resync(self) -> bool:
        """Pop the force-resync flag set after a transient rollback failure.

        Returns True exactly once per failure event so the main loop can
        prioritise a portfolio_sync.refresh() before its next risk check.
        """
        if self._force_portfolio_resync_ts is None:
            return False
        self._force_portfolio_resync_ts = None
        return True

    def is_successful_execution(
        self, opp: ArbOpportunity, trades: list[TradeRecord]
    ) -> bool:
        return (
            len(trades) == len(opp.legs)
            and all(t.status == TradeStatus.FILLED for t in trades)
        )

    def _resolve_execution_order_type(self) -> Any:
        # In dry-run we never reach `_submit_order_v1/v2`; the orchestrator
        # short-circuits into `_simulate_dry_run_arbitrage`. Returning None
        # here keeps the contract uniform: live callers always see an Enum
        # (or get a hard ValueError); dry-run callers must not consume it.
        if self._config.dry_run:
            return None

        for candidate in ("FOK", "FAK", "GTC"):
            resolved = self._resolve_named_order_type(candidate)
            if resolved is not None:
                return resolved
        raise ValueError("py_clob_client OrderType 缺少 FOK/FAK/GTC，无法初始化 ExecutionEngine")

    def _resolve_named_order_type(self, name: str) -> Any:
        # Live path: always resolve to the underlying Enum member; downstream
        # CLOB clients reject raw strings on the v2 API. Dry-run callers
        # should never consult this.
        if self._config.dry_run:
            return None
        try:
            from py_clob_client_v2 import OrderType
        except ImportError:
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
        price, size = _quantize_clob_order_args(price, size)
        if price <= 0 or size <= 0:
            return OrderSubmissionResult(
                order_id="",
                trade_status=TradeStatus.FAILED,
                error="invalid_quantized_order_args",
            )
        if hasattr(self._client, "create_and_post_order"):
            return self._submit_order_v2(
                token_id,
                side,
                price,
                size,
                order_type=order_type,
                post_only=post_only,
            )

        return self._submit_order_v1(
            token_id,
            side,
            price,
            size,
            order_type=order_type,
            post_only=post_only,
        )

    def _submit_order_v1(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
        *,
        order_type: Any | None = None,
        post_only: bool = False,
    ) -> OrderSubmissionResult:
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
        self.invalidate_balance_cache()
        return self._parse_order_submission_response(resp, execution_type=execution_type)

    def _submit_order_v2(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
        *,
        order_type: Any | None = None,
        post_only: bool = False,
    ) -> OrderSubmissionResult:
        from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, Side

        clob_side = Side.BUY if side == OrderSide.BUY else Side.SELL
        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size),
            side=clob_side,
        )
        execution_type = order_type if order_type is not None else self._execution_order_type
        resp = self._client.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=execution_type,
            post_only=post_only,
        )
        self.invalidate_balance_cache()
        return self._parse_order_submission_response(resp, execution_type=execution_type)

    def _parse_order_submission_response(self, resp: Any, *, execution_type: Any) -> OrderSubmissionResult:
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
        elif success and execution_type != self._gtc_order_type:
            trade_status = TradeStatus.FAILED
            if not error_msg:
                error_msg = "non_gtc_not_filled"
        elif success and remote_status in _PENDING_REMOTE_STATUSES:
            trade_status = TradeStatus.PENDING
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

    def _rollback_orders(self, order_ids: list[str]) -> RollbackResult:
        """Cancel orders best-effort, classifying failures by mode.

        Two outcomes matter to the caller:
        - cancelled: client confirmed the cancel.
        - transient_failed: cancel raised a transport/network error; the
          order may still be live on the CLOB. Caller MUST keep the
          corresponding TradeRecord in PENDING and force a portfolio resync.

        Logical errors (AttributeError / ValueError / RuntimeError) signal
        local programmer mistakes — we treat them as cancelled-on-our-side
        because retrying won't help and the order most likely never went
        through; this preserves the original behaviour for those classes.
        """
        result = RollbackResult()
        for oid in order_ids:
            try:
                self._client.cancel(oid)
            except (AttributeError, RuntimeError, ValueError) as exc:
                # Programming/contract bugs — keep prior semantics: order
                # almost certainly never landed; do not pessimistically
                # block downstream tiers.
                LOG.error("回滚: 撤销订单 %s 失败 (logical): %s", oid[:16], exc)
                continue
            except Exception as exc:
                # Anything else — requests.RequestException, httpx errors,
                # OSError, timeouts. Order may still be alive on the CLOB.
                LOG.error("回滚: 撤销订单 %s 失败 (transport): %s", oid[:16], exc)
                result.transient_failed.add(oid)
                continue
            LOG.info("回滚: 已撤销订单 %s", oid[:16])
            result.cancelled.add(oid)
        return result

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
        normalized_price, normalized_size = _quantize_clob_order_args(price, size)
        trade = TradeRecord(
            trade_id=str(uuid.uuid4())[:12],
            arb_id=arb_ref,
            token_id=token_id,
            condition_id=condition_id,
            side=side,
            price=normalized_price,
            size=normalized_size,
            economic_cost=normalized_price,
            post_only=post_only,
            order_type_name=order_type_name,
        )

        if normalized_size <= 0 or normalized_price <= 0:
            trade.status = TradeStatus.FAILED
            trade.error = "invalid_order_args"
            self._append_trade_record(trade, simulated=self._config.dry_run)
            return trade

        if self._config.dry_run:
            trade.status = TradeStatus.PENDING if post_only else TradeStatus.FILLED
            trade.simulated = True
            if trade.status == TradeStatus.FILLED:
                trade.fill_price = normalized_price
                trade.fill_size = normalized_size
            self._append_trade_record(trade, simulated=True)
            return trade

        try:
            submission = self._submit_order(
                token_id,
                side,
                normalized_price,
                normalized_size,
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
                trade.fill_price = normalized_price
            if trade.status == TradeStatus.FILLED and trade.fill_size is None:
                trade.fill_size = normalized_size
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

    def get_available_collateral_balance(self, *, use_cache: bool = True) -> float | None:
        """读取可用 USDC 余额（余额与 allowance 的较小值）.

        实盘模式下每次 pre-trade 都会调用；为避免在余额不足时每条信号都打一次 API，
        默认走短 TTL 缓存。真实下单前或余额可能变化时调用 `invalidate_balance_cache()`。
        """
        if use_cache and self._balance_cache is not None:
            if time.time() - self._balance_cache_ts < self._BALANCE_CACHE_TTL_SEC:
                return self._balance_cache

        value = self._fetch_collateral_balance_uncached()
        self._balance_cache = value
        self._balance_cache_ts = time.time()
        return value

    def invalidate_balance_cache(self) -> None:
        """在下单/成交/充值等可能改变余额的动作后调用."""
        self._balance_cache = None
        self._balance_cache_ts = 0.0

    def _fetch_collateral_balance_uncached(self) -> float | None:
        balance_params = self._build_collateral_balance_params()
        try:
            if balance_params is None:
                response = self._client.get_balance_allowance()
            else:
                response = self._client.get_balance_allowance(balance_params)
        except TypeError:
            if self._config.signature_type == 3:
                return None
            try:
                fallback_params = self._build_legacy_balance_params_with_signature(-1)
                response = self._client.get_balance_allowance(fallback_params)
            except Exception:
                return None
        except Exception:
            return None
        return self._parse_balance_response(response)

    def _build_collateral_balance_params(self) -> Any | None:
        if self._config.signature_type == 3:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            return BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=_resolve_poly_1271_signature_type(),
            )

        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        except Exception:
            return None
        return BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)

    def _build_legacy_balance_params_with_signature(self, signature_type: int) -> Any:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        return BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=signature_type,
        )

    def get_recent_trades(self, limit: int = 20, *, include_simulated: bool = False) -> list[TradeRecord]:
        history = self._trade_history if not include_simulated else self._trade_history + self._simulated_trade_history
        return history[-limit:]

    def sync_pending_trade_statuses(self) -> OrderSyncResult:
        """主动同步本地 pending/partial 订单状态.

        返回:
            OrderSyncResult:
              - polled: 本轮查询过的挂单/部分成交记录
              - changed: 状态或成交字段有变化的记录
        """
        if self._config.dry_run or not hasattr(self._client, "get_order"):
            return OrderSyncResult(polled=[], changed=[])

        polled: list[TradeRecord] = []
        changed: list[TradeRecord] = []

        for trade in self._trade_history:
            if trade.simulated or not trade.order_id:
                continue
            if trade.status not in (TradeStatus.PENDING, TradeStatus.PARTIAL):
                continue

            try:
                response = self._client.get_order(trade.order_id)
            except Exception as exc:  # pragma: no cover - depends on client transport
                LOG.debug("同步订单状态失败 order=%s: %s", trade.order_id[:16], exc)
                continue

            polled.append(trade)
            next_status, fill_size, fill_price, error = _parse_trade_sync_response(
                response,
                requested_size=trade.size,
                current_status=trade.status,
            )

            before = (
                trade.status,
                trade.fill_size,
                trade.fill_price,
                trade.error or "",
            )
            trade.status = next_status
            if fill_size is not None:
                trade.fill_size = fill_size
            if fill_price is not None:
                trade.fill_price = fill_price
            if error:
                trade.error = error

            after = (
                trade.status,
                trade.fill_size,
                trade.fill_price,
                trade.error or "",
            )
            if after != before:
                changed.append(trade)
                LOG.info(
                    "订单状态已同步: order=%s status=%s fill_size=%s fill_price=%s",
                    trade.order_id[:16],
                    trade.status.value,
                    f"{trade.fill_size:.4f}" if trade.fill_size is not None else "N/A",
                    f"{trade.fill_price:.4f}" if trade.fill_price is not None else "N/A",
                )

        return OrderSyncResult(polled=polled, changed=changed)

    def cancel_stale_maker_orders(self, max_age_sec: float) -> list[TradeRecord]:
        """撤销超过 TTL 仍未成交的 live 挂单 (T3 做市 GTC / post_only 订单)。

        返回本轮成功撤单的 TradeRecord 列表，调用方应把它传给
        risk_manager.reconcile_pending_order_statuses 以释放预留敞口。
        """
        if self._config.dry_run or max_age_sec <= 0:
            return []
        if not hasattr(self._client, "cancel"):
            return []

        now = time.time()
        cancelled: list[TradeRecord] = []
        for trade in self._trade_history:
            if trade.simulated or not trade.order_id:
                continue
            if trade.status not in (TradeStatus.PENDING, TradeStatus.PARTIAL):
                continue
            if (now - trade.timestamp) <= max_age_sec:
                continue
            if not bool(getattr(trade, "post_only", False)):
                continue
            if str(getattr(trade, "order_type_name", "") or "").upper() != "GTC":
                continue

            try:
                self._client.cancel(trade.order_id)
            except Exception as exc:  # pragma: no cover - depends on client transport
                LOG.warning(
                    "撤销过期挂单失败 order=%s age=%.1fs: %s",
                    trade.order_id[:16],
                    now - trade.timestamp,
                    exc,
                )
                continue

            trade.status = TradeStatus.CANCELLED
            trade.error = trade.error or "maker_order_ttl_expired"
            cancelled.append(trade)
            LOG.info(
                "撤销过期挂单: order=%s trade=%s market=%s age=%.1fs",
                trade.order_id[:16],
                trade.trade_id,
                trade.condition_id[:12],
                now - trade.timestamp,
            )
        return cancelled

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
                size = t.fill_size if t.fill_size is not None else t.size
                total_cost += leg_cost * size
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
        *,
        order_type: Any | None = None,
    ) -> list[TradeRecord]:
        records: list[TradeRecord] = []
        leg_prices = [
            _quantize_clob_price(leg.execution_price if leg.execution_price is not None else leg.price)
            for leg in opp.legs
        ]
        quantized_size = _quantize_common_clob_order_size(leg_prices, actual_size)
        for leg, quantized_price in zip(opp.legs, leg_prices):
            records.append(TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=arb_id,
                token_id=leg.token_id,
                condition_id=leg.condition_id,
                side=leg.side,
                price=quantized_price,
                size=quantized_size,
                economic_cost=leg.economic_cost if leg.economic_cost is not None else leg.price,
            ))
        if len(records) != len(opp.legs):
            raise RuntimeError("套利腿与交易记录数量不一致")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(opp.legs)) as pool:
            future_map = {
                pool.submit(
                    self._submit_order,
                    leg.token_id,
                    leg.side,
                    record.price,
                    record.size,
                    order_type=order_type,
                ): (index, leg, record)
                # `zip(strict=True)` 仅在较新的 Python 版本可用；这里前面已经做过长度一致性校验，
                # 因此直接使用普通 zip 以兼容部署环境中的旧版本解释器。
                for index, (leg, record) in enumerate(zip(opp.legs, records))
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
                        record.fill_size = submission.fill_size if submission.fill_size is not None else record.size
                        LOG.info(
                            "  腿 %d/%d 成功: %s %s @ $%.4f x %.2f, order=%s",
                            index + 1,
                            len(opp.legs),
                            leg.side.value,
                            leg.outcome,
                            record.price,
                            record.size,
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
                except Exception as e:  # pragma: no cover - depends on remote CLOB failures
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

    def _auto_flatten_incomplete_fills(
        self,
        opp: ArbOpportunity,
        arb_id: str,
        records: list[TradeRecord],
    ) -> list[TradeRecord]:
        """Best-effort flatten for filled legs from a failed multi-leg arb."""
        if self._config.dry_run or len(opp.legs) <= 1:
            return []

        filled = [
            record
            for record in records
            if record.status in (TradeStatus.FILLED, TradeStatus.PARTIAL)
            and float(record.fill_size or 0.0) > 0
        ]
        if not filled:
            return []

        flatten_order_type = self._resolve_named_order_type("FAK")
        flatten_records: list[TradeRecord] = []
        for record in filled:
            reverse_side = OrderSide.SELL if record.side == OrderSide.BUY else OrderSide.BUY
            fill_size = float(record.fill_size or 0.0)
            flatten_price = 0.01 if reverse_side == OrderSide.SELL else 0.99
            flatten = TradeRecord(
                trade_id=str(uuid.uuid4())[:12],
                arb_id=arb_id,
                token_id=record.token_id,
                condition_id=record.condition_id,
                side=reverse_side,
                price=flatten_price,
                size=fill_size,
                economic_cost=flatten_price,
                error="auto_flatten_after_hedge_incomplete",
            )
            try:
                submission = self._submit_order(
                    record.token_id,
                    reverse_side,
                    flatten_price,
                    fill_size,
                    order_type=flatten_order_type,
                )
                flatten.order_id = submission.order_id
                flatten.status = submission.trade_status
                flatten.error = submission.error or flatten.error
                if submission.fill_price is not None:
                    flatten.fill_price = submission.fill_price
                if submission.fill_size is not None:
                    flatten.fill_size = submission.fill_size
                if flatten.status == TradeStatus.FILLED and flatten.fill_price is None:
                    flatten.fill_price = flatten_price
                if flatten.status == TradeStatus.FILLED and flatten.fill_size is None:
                    flatten.fill_size = fill_size
            except Exception as exc:  # pragma: no cover - live transport
                flatten.status = TradeStatus.FAILED
                flatten.error = f"auto_flatten_failed:{exc}"
            self._append_trade_record(flatten)
            flatten_records.append(flatten)
            LOG.warning(
                "[cid=%s] hedge_incomplete 自动平仓: token=%s side=%s size=%.4f status=%s",
                arb_id,
                record.token_id[:16],
                reverse_side.value,
                fill_size,
                flatten.status.value,
            )
        return flatten_records

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

        direct_available = _coerce_collateral_amount(
            payload.get("available")
            or payload.get("available_balance")
            or payload.get("availableBalance")
            or payload.get("buying_power")
            or payload.get("buyingPower")
        )
        if direct_available is not None:
            return direct_available

        balance = _coerce_collateral_amount(
            payload.get("balance")
            or payload.get("balance_decimal")
            or payload.get("balanceDecimal")
        )
        allowance = _coerce_collateral_amount(
            payload.get("allowance")
            or payload.get("allowance_decimal")
            or payload.get("allowanceDecimal")
        )
        if balance is not None and allowance is not None:
            return min(balance, allowance)
        return balance if balance is not None else allowance


def _coerce_rollback_outcome(outcome: Any) -> tuple[set[str], set[str]]:
    """Bridge between the new `RollbackResult` and legacy `set[str]` mocks.

    Existing tests stub `_rollback_orders` with `lambda ids: set(ids)`; this
    keeps the call sites working without forcing every test to migrate.
    """
    if isinstance(outcome, RollbackResult):
        return outcome.cancelled, outcome.transient_failed
    if isinstance(outcome, set):
        return outcome, set()
    if outcome is None:
        return set(), set()
    return set(outcome), set()


def _quantize_clob_order_args(price: float, size: float) -> tuple[float, float]:
    """Normalize order args to CLOB decimal precision.

    CLOB rejects market buy orders when maker amount has >2 decimals or taker
    amount has >5 decimals. For OrderArgs that means both share size and the
    derived USDC notional (`price * size`) must be representable exactly at
    their allowed precision.
    """
    quantized_price = _quantize_clob_price(price)
    quantized_size = _quantize_clob_size(size)
    if quantized_price <= 0 or quantized_size <= 0:
        return 0.0, 0.0

    legal_size = _quantize_common_clob_order_size([quantized_price], quantized_size)
    if legal_size <= 0:
        return 0.0, 0.0
    return quantized_price, legal_size


def _quantize_clob_price(price: float) -> float:
    return _quantize_decimal(price, _PRICE_QUANT, rounding=ROUND_HALF_UP)


def _quantize_clob_size(size: float) -> float:
    return _quantize_decimal(size, _SIZE_QUANT, rounding=ROUND_DOWN)


def _quantize_common_clob_order_size(prices: list[float], size: float) -> float:
    quantized_size = _quantize_clob_size(size)
    size_units = int((Decimal(str(quantized_size)) * 100000).to_integral_value(rounding=ROUND_DOWN))
    if size_units <= 0:
        return 0.0

    size_unit_step = 1
    for price in prices:
        price_cents = int((Decimal(str(price)) * 100).to_integral_value())
        if price_cents <= 0:
            return 0.0
        step = 100000 // gcd(price_cents, 100000)
        size_unit_step = _lcm(size_unit_step, step)

    legal_size_units = (size_units // size_unit_step) * size_unit_step
    if legal_size_units <= 0:
        return 0.0
    return float(Decimal(legal_size_units) / Decimal(100000))


def _lcm(left: int, right: int) -> int:
    return abs(left * right) // gcd(left, right)


def _quantize_decimal(value: float, quantum: Decimal, *, rounding: str) -> float:
    try:
        decimal_value = Decimal(str(value))
    except Exception:
        return 0.0
    if decimal_value <= 0:
        return 0.0
    return float(decimal_value.quantize(quantum, rounding=rounding))


def _coerce_fill_field(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _coerce_collateral_amount(value: Any) -> float | None:
    """Parse CLOB collateral amounts, accepting decimal USDC or raw 6-decimal units."""
    parsed = _coerce_fill_field(value)
    if parsed is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and parsed >= 1_000_000:
            return parsed / 1_000_000.0
    elif isinstance(value, int) and parsed >= 1_000_000:
        return parsed / 1_000_000.0
    return parsed


def _resolve_poly_1271_signature_type() -> Any:
    try:
        from py_clob_client_v2 import SignatureTypeV2
    except ImportError:
        return 3
    return getattr(SignatureTypeV2, "POLY_1271", 3)


def _parse_trade_sync_response(
    response: Any,
    *,
    requested_size: float,
    current_status: TradeStatus,
) -> tuple[TradeStatus, float | None, float | None, str]:
    payload = response
    if isinstance(payload, dict):
        for key in ("data", "result", "order"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                payload = nested
                break

    remote_status = ""
    error_msg = ""
    fill_size: float | None = None
    fill_price: float | None = None
    success = True

    if isinstance(payload, dict):
        success = bool(payload.get("success", not payload.get("error")))
        remote_status = str(payload.get("status") or "").lower()
        error_msg = str(payload.get("errorMsg") or payload.get("error") or "")
        fill_size = _coerce_fill_field(
            payload.get("fillSize")
            or payload.get("filledSize")
            or payload.get("matchedAmount")
            or payload.get("sizeMatched")
            or payload.get("filled")
        )
        fill_price = _coerce_fill_field(
            payload.get("fillPrice")
            or payload.get("avgPrice")
            or payload.get("averagePrice")
            or payload.get("matchedPrice")
        )
    else:
        remote_status = str(getattr(payload, "status", "")).lower()
        error_msg = str(getattr(payload, "errorMsg", "") or getattr(payload, "error", ""))
        fill_size = _coerce_fill_field(
            getattr(payload, "fillSize", None)
            or getattr(payload, "filledSize", None)
            or getattr(payload, "matchedAmount", None)
            or getattr(payload, "sizeMatched", None)
            or getattr(payload, "filled", None)
        )
        fill_price = _coerce_fill_field(
            getattr(payload, "fillPrice", None)
            or getattr(payload, "avgPrice", None)
            or getattr(payload, "averagePrice", None)
            or getattr(payload, "matchedPrice", None)
        )

    if not success:
        return TradeStatus.FAILED, fill_size, fill_price, error_msg
    if remote_status in {"matched", "filled"}:
        return TradeStatus.FILLED, fill_size or float(requested_size), fill_price, error_msg
    if remote_status in _PARTIAL_REMOTE_STATUSES:
        return TradeStatus.PARTIAL, fill_size, fill_price, error_msg
    if remote_status in _FAILED_REMOTE_STATUSES:
        next_status = TradeStatus.CANCELLED if remote_status in {"cancelled", "canceled"} else TradeStatus.FAILED
        return next_status, fill_size, fill_price, error_msg
    if remote_status in _PENDING_REMOTE_STATUSES:
        return TradeStatus.PENDING, fill_size, fill_price, error_msg
    if fill_size is not None:
        if fill_size + 1e-9 >= float(requested_size):
            return TradeStatus.FILLED, fill_size, fill_price, error_msg
        if fill_size > 0:
            return TradeStatus.PARTIAL, fill_size, fill_price, error_msg
    return current_status, fill_size, fill_price, error_msg
