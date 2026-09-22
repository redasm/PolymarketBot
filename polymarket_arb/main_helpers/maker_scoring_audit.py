"""T3 挂单计分校验 (CLOB /orders-scoring).

为什么需要它:

把报价夹进 `[mid - δ, mid + δ]`（见 `rewards_client` + `maker_strategy`
的奖励带 clamp）只是**先验**判断"这单应该计分"。真正是否计入流动性
奖励取决于交易所侧的一堆条件：奖励带参数是否已经变了、挂单规模是否
达到 `rewards_min_size`、订单是不是还在簿上、市场是否仍在激励名单里。
先验和实际不一致时，T3 会以为自己在赚奖励，实际一分没拿 —— 而这在
telemetry 里是完全看不见的。

本模块每 `MAKER_SCORING_AUDIT_INTERVAL_SEC` 拉一次在簿挂单的计分状态，
写 `risk_events` 的 `maker_scoring_audit` 行：`scoring_ratio` 就是
"T3 到底有没有在赚奖励"的客观指标。

三个刻意的保守选择:

1. **未知 ≠ 未计分**。`are_orders_scoring` 失败或没返回某个 order 时，
   该订单不计入分母，也永远不会被撤。一次 API 抖动不应该引发一轮误撤单。
2. **默认只观测不动作**。`MAKER_SCORING_CANCEL_UNSCORED` 默认 false，
   先积累数据再决定要不要自动撤单。
3. **撤单前有 grace 窗口**。刚挂出的单需要时间才会被计分，连续
   `MAKER_SCORING_UNSCORED_GRACE_SEC` 观测到未计分才算数。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from polymarket_arb.config import ArbConfig
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.main_helpers.order_sync import (
    _release_t3_exposure_for_synced_orders,
)
from polymarket_arb.risk_manager import RiskManager
from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator

LOG = logging.getLogger("main_loop")


@dataclass
class MakerScoringAuditState:
    """跨周期状态：上次运行时间 + 每个 order 首次被观测为未计分的时刻."""

    last_run_ts: float = 0.0
    first_unscored_ts: dict[str, float] = field(default_factory=dict)
    last_summary: dict = field(default_factory=dict)

    def observe(self, order_id: str, scoring: bool, now: float) -> float:
        """更新未计分起始时刻，返回该订单已连续未计分的秒数."""
        if scoring:
            self.first_unscored_ts.pop(order_id, None)
            return 0.0
        started = self.first_unscored_ts.setdefault(order_id, now)
        return max(0.0, now - started)

    def forget(self, order_ids: set[str]) -> None:
        """订单已离簿，清掉它的观测状态，避免字典无限增长."""
        for order_id in list(self.first_unscored_ts):
            if order_id not in order_ids:
                self.first_unscored_ts.pop(order_id, None)


def audit_maker_order_scoring(
    *,
    config: ArbConfig,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    event_recorder: EventRecorder,
    state: MakerScoringAuditState,
    orchestrator: StrategyOrchestrator | None = None,
    now: float | None = None,
) -> dict:
    """校验在簿 T3 挂单的计分状态，返回本轮摘要（也写 telemetry）.

    永不抛异常：失败记 `maker_scoring_audit_error` 并返回空摘要。
    """
    now = time.time() if now is None else float(now)
    if not config.maker_scoring_audit_enabled or config.dry_run:
        return {}
    interval = max(0.0, float(config.maker_scoring_audit_interval_sec))
    if interval > 0 and (now - state.last_run_ts) < interval:
        return {}
    state.last_run_ts = now

    try:
        live_trades = executor.live_maker_trades()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("读取在簿挂单失败: %s", exc)
        _write_error(event_recorder, exc, now)
        return {}

    live_by_order = {
        str(trade.order_id): trade for trade in live_trades if trade.order_id
    }
    state.forget(set(live_by_order))
    if not live_by_order:
        return {}

    try:
        scoring_map = executor.check_orders_scoring(list(live_by_order))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("挂单计分校验失败: %s", exc)
        _write_error(event_recorder, exc, now)
        return {}

    scored = 0
    unscored = 0
    unknown = 0
    unscored_markets: dict[str, int] = {}
    cancel_candidates: list[str] = []

    for order_id, trade in live_by_order.items():
        if order_id not in scoring_map:
            # 未知：不计入分母，也不推进 grace 计时。
            unknown += 1
            continue
        is_scoring = bool(scoring_map[order_id])
        stale_sec = state.observe(order_id, is_scoring, now)
        if is_scoring:
            scored += 1
            continue
        unscored += 1
        market = str(trade.condition_id or "")[:12]
        unscored_markets[market] = unscored_markets.get(market, 0) + 1
        if (
            config.maker_scoring_cancel_unscored
            and stale_sec >= max(0.0, float(config.maker_scoring_unscored_grace_sec))
        ):
            cancel_candidates.append(order_id)

    checked = scored + unscored
    summary = {
        "event": "maker_scoring_audit",
        "live_orders": len(live_by_order),
        "checked": checked,
        "scoring": scored,
        "not_scoring": unscored,
        "unknown": unknown,
        "scoring_ratio": round(scored / checked, 4) if checked else None,
        "unscored_by_market": dict(
            sorted(unscored_markets.items(), key=lambda kv: kv[1], reverse=True)[:10]
        ),
        "cancelled": 0,
        "ts": now,
    }

    if cancel_candidates:
        summary["cancelled"] = _cancel_unscored(
            executor=executor,
            risk_mgr=risk_mgr,
            event_recorder=event_recorder,
            orchestrator=orchestrator,
            order_ids=cancel_candidates,
            state=state,
            now=now,
        )

    state.last_summary = summary
    if event_recorder.is_enabled:
        event_recorder.write_event("risk_events", summary)
    if checked and summary["scoring_ratio"] is not None:
        LOG.info(
            "T3 计分校验: %d/%d 计分 (%.0f%%), 未知 %d, 撤单 %d",
            scored,
            checked,
            summary["scoring_ratio"] * 100.0,
            unknown,
            summary["cancelled"],
        )
    return summary


def _cancel_unscored(
    *,
    executor: ExecutionEngine,
    risk_mgr: RiskManager,
    event_recorder: EventRecorder,
    orchestrator: StrategyOrchestrator | None,
    order_ids: list[str],
    state: MakerScoringAuditState,
    now: float,
) -> int:
    try:
        cancelled = executor.cancel_maker_orders_by_id(
            order_ids, reason="maker_order_not_scoring"
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("撤销未计分挂单失败: %s", exc)
        _write_error(event_recorder, exc, now)
        return 0
    if not cancelled:
        return 0

    risk_mgr.reconcile_pending_order_statuses(cancelled)
    _release_t3_exposure_for_synced_orders(orchestrator, cancelled)
    for trade in cancelled:
        state.first_unscored_ts.pop(str(trade.order_id), None)
        if event_recorder.is_enabled:
            event_recorder.write_event(
                "risk_events",
                {
                    "event": "maker_order_cancelled",
                    "reason": "not_scoring",
                    "order_id": trade.order_id,
                    "trade_id": trade.trade_id,
                    "condition_id": trade.condition_id,
                    "age_sec": now - trade.timestamp,
                    "ts": now,
                },
            )
    return len(cancelled)


def _write_error(event_recorder: EventRecorder, exc: Exception, now: float) -> None:
    if not event_recorder.is_enabled:
        return
    event_recorder.write_event(
        "risk_events",
        {"event": "maker_scoring_audit_error", "error": str(exc), "ts": now},
    )
