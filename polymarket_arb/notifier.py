"""统一通知层：路由到飞书，并封装业务告警逻辑."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from polymarket_arb.config import ArbConfig
from polymarket_arb.feishu_notifier import FeishuNotifier

LOG = logging.getLogger(__name__)


class _Backend(Protocol):
    def send(self, message: str, *, category: str = "general", force: bool = False) -> bool:
        ...


class NullNotifier:
    def send(self, message: str, *, category: str = "general", force: bool = False) -> bool:
        return False


@dataclass
class DailySummaryPayload:
    risk_date: str
    trade_success_count: int
    trade_failure_count: int
    fatal_error_count: int
    live_expected_profit_total: float
    simulated_expected_profit_total: float
    closing_daily_pnl: float
    closing_open_positions: int
    closing_total_exposure: float
    summary_local_date: str
    # Wallet USDC at day close (None = balance query unavailable / no creds).
    # Surfaced so operators don't have to check the chain manually to know
    # "how much capital is still on the table" after the day's activity.
    closing_wallet_usdc: float | None = None
    # "live" or "shadow" — disambiguates "0 trades" between "bot was idle"
    # and "bot is intentionally simulating (ARB_DRY_RUN=true)".
    mode: str = "live"


class NotificationManager:
    """统一通知管理器，负责 provider 路由、状态持久化和节流."""

    def __init__(self, config: ArbConfig):
        self._config = config
        self._backend = _build_backend(config)
        self._state_path = Path(config.notification_state_file)
        self._summary_tz = ZoneInfo(config.daily_summary_timezone)
        self._summary_hour, self._summary_minute = _parse_hhmm(config.daily_summary_time_hhmm)
        self._state = self._load_state()

    def send(self, message: str, *, category: str = "general", force: bool = False) -> bool:
        return self._backend.send(message, category=category, force=force)

    def notify_startup(
        self,
        *,
        mode: str,
        min_profit_usd: float,
        min_profit_pct: float,
        scan_interval_sec: float,
        ws_enabled: bool,
        portfolio_sync_enabled: bool,
        max_order_size_usdc: float,
        max_exposure_per_market: float,
        max_total_exposure: float,
        max_daily_loss: float,
        max_open_positions: int,
    ) -> bool:
        message = (
            "🤖 机器人启动\n"
            f"模式: {mode}\n"
            f"最小利润: ${min_profit_usd:.4f} / {min_profit_pct:.2f}%\n"
            f"扫描间隔: {scan_interval_sec:.1f}s\n"
            f"WebSocket: {'启用' if ws_enabled else '禁用'}\n"
            f"账户同步: {'启用' if portfolio_sync_enabled else '禁用'}\n"
            "---- 风控上限 ----\n"
            f"单笔上限: ${max_order_size_usdc:.2f}\n"
            f"单市场敞口: ${max_exposure_per_market:.2f}\n"
            f"总敞口: ${max_total_exposure:.2f}\n"
            f"日亏熔断: -${max_daily_loss:.2f}\n"
            f"最大持仓数: {max_open_positions}"
        )
        return self._backend.send(message, category="startup", force=True)

    def notify_shutdown(
        self,
        *,
        run_id: str,
        cycle_count: int,
        total_arbs_executed: int,
        simulated_successes: int,
        total_t0_opportunities: int = 0,
        total_directional_signals: int = 0,
    ) -> bool:
        # Split T0 (structural arb) from directional (T2/T3) signals so the
        # operator can tell "0 trades because no T0 was found" from
        # "0 trades because all the T2 signals were blocked downstream".
        message = (
            "🛑 机器人停止\n"
            f"实例: {run_id}\n"
            f"周期数: {cycle_count}\n"
            f"T0 结构机会: {total_t0_opportunities}\n"
            f"定向信号(T1/T2/T3): {total_directional_signals}\n"
            f"真实执行成功: {total_arbs_executed}\n"
            f"模拟执行成功: {simulated_successes}"
        )
        return self._backend.send(message, category="shutdown", force=True)

    def notify_arb_found(self, message: str, *, simulated: bool | None = None) -> bool:
        if not self._config.notify_on_arb_found:
            return False
        # Suppress per-opportunity push in shadow mode unless explicitly
        # opted in — under ARB_DRY_RUN every detected opportunity would
        # otherwise flood the chat (no real trade happens to "consume"
        # the alert). Callers that explicitly pass `simulated=False`
        # bypass the shadow check.
        is_shadow = simulated if simulated is not None else self._config.dry_run
        if is_shadow and not self._config.notify_arb_found_in_shadow:
            return False
        prefix = "🌓 [SHADOW] " if is_shadow else ""
        return self._backend.send(prefix + message, category="arb_found")

    def notify_trade_success(
        self,
        *,
        event_title: str,
        arb_type: str,
        filled_legs: int,
        total_legs: int,
        expected_profit: float,
        simulated: bool,
        now_ts: float | None = None,
    ) -> bool:
        now_ts = now_ts or time.time()
        self._ensure_daily_state(now_ts)
        stats = self._state["current_daily_stats"]
        stats["trade_success_count"] = int(stats.get("trade_success_count", 0)) + 1
        profit_key = "simulated_expected_profit_total" if simulated else "live_expected_profit_total"
        stats[profit_key] = float(stats.get(profit_key, 0.0)) + float(expected_profit)
        self._persist_state()

        if not self._config.notify_on_trade_success:
            return False

        mode = "DRY RUN" if simulated else "LIVE"
        lines = [
            "✅ 成交成功",
            f"事件: {event_title}",
            f"类型: {arb_type}",
            f"模式: {mode}",
            f"腿数: {filled_legs}/{total_legs}",
            f"预期净利: ${expected_profit:.4f}",
        ]
        lines.extend(self._format_capital_snapshot_lines())
        return self._backend.send("\n".join(lines), category="trade_success", force=True)

    def notify_trade_failure(
        self,
        *,
        event_title: str,
        filled_legs: int,
        total_legs: int,
        simulated: bool,
        details: str = "",
        now_ts: float | None = None,
    ) -> bool:
        now_ts = now_ts or time.time()
        self._ensure_daily_state(now_ts)
        stats = self._state["current_daily_stats"]
        stats["trade_failure_count"] = int(stats.get("trade_failure_count", 0)) + 1
        self._persist_state()

        if not self._config.notify_on_trade_failure:
            return False

        mode = "DRY RUN" if simulated else "LIVE"
        lines = [
            "❌ 成交失败",
            f"事件: {event_title}",
            f"模式: {mode}",
            f"已成交腿数: {filled_legs}/{total_legs}",
        ]
        if details:
            lines.append(f"详情: {details}")
        lines.extend(self._format_capital_snapshot_lines())
        return self._backend.send("\n".join(lines), category="trade_failure", force=True)

    def _format_capital_snapshot_lines(self) -> list[str]:
        """Render the latest known capital state for in-trade notifications.

        Pulled from ``current_risk_snapshot`` which ``observe_cycle`` keeps
        fresh (≤ 1 scan cycle stale). Returns an empty list when no snapshot
        has been recorded yet (e.g. trade fires before the first cycle).
        """
        snapshot = self._state.get("current_risk_snapshot") or {}
        if not snapshot:
            return []
        lines = [
            f"持仓/敞口: {int(snapshot.get('open_positions', 0))} / ${float(snapshot.get('total_exposure', 0.0)):.2f}",
        ]
        wallet = snapshot.get("wallet_usdc")
        if wallet is not None:
            lines.append(f"钱包(USDC): ${float(wallet):.2f}")
        return lines

    def notify_fatal_error(
        self,
        message: str,
        *,
        error_key: str,
        context_lines: list[str] | None = None,
        now_ts: float | None = None,
    ) -> bool:
        """Send a deduplicated fatal alert.

        Per-key cooldown is exponential so a long-running incident doesn't
        spam Feishu hourly: send N=1 immediately, then wait base × 2^(N-1)
        capped at 24h. ``fatal_error_count`` only increments on actual
        sends — past behaviour conflated dedup hits with new incidents.
        """
        now_ts = now_ts or time.time()
        self._ensure_daily_state(now_ts)
        stats = self._state["current_daily_stats"]
        last_sent = float(self._state.get("fatal_error_last_sent", {}).get(error_key, 0.0))
        send_counts = self._state.setdefault("fatal_error_send_counts", {})
        prior_sends = int(send_counts.get(error_key, 0))
        base_cooldown = max(0.0, float(self._config.fatal_error_cooldown_sec))
        if prior_sends <= 0:
            required_cooldown = 0.0
        else:
            required_cooldown = min(
                86400.0,
                base_cooldown * (2 ** (prior_sends - 1)),
            )
        should_send = (now_ts - last_sent) >= required_cooldown

        if not should_send:
            return False

        stats["fatal_error_count"] = int(stats.get("fatal_error_count", 0)) + 1
        self._state.setdefault("fatal_error_last_sent", {})[error_key] = now_ts
        send_counts[error_key] = prior_sends + 1
        self._persist_state()

        if not self._config.notify_on_fatal_error:
            return False

        body_lines = [
            "🚨 严重错误",
            f"错误键: {error_key}",
            message,
        ]
        if context_lines:
            body_lines.extend(context_lines)
        return self._backend.send(
            "\n".join(body_lines),
            category=f"fatal_error:{error_key}",
            force=True,
        )

    def maybe_notify_pnl_alert(
        self,
        *,
        daily_pnl: float,
        now_ts: float | None = None,
    ) -> bool:
        now_ts = now_ts or time.time()
        self._ensure_daily_state(now_ts)
        stats = self._state["current_daily_stats"]
        risk_date = str(stats["date_key"])
        pnl_alerts = self._state.setdefault("pnl_alerts", {})
        sent = False

        trade_success = int(stats.get("trade_success_count", 0))
        trade_failure = int(stats.get("trade_failure_count", 0))
        snapshot = self._state.get("current_risk_snapshot") or {}
        wallet = snapshot.get("wallet_usdc")
        wallet_line = (
            f"钱包(USDC): ${float(wallet):.2f}\n" if wallet is not None else ""
        )

        if (
            self._config.notify_on_pnl_alert
            and self._config.pnl_profit_alert_usdc > 0
            and daily_pnl >= self._config.pnl_profit_alert_usdc
            and pnl_alerts.get("profit_risk_date") != risk_date
        ):
            profit_sent = self._backend.send(
                "📈 盈利提醒\n"
                f"统计日(UTC): {risk_date}\n"
                f"已记录日盈亏: ${daily_pnl:+.2f}\n"
                f"盈利阈值: ${self._config.pnl_profit_alert_usdc:.2f}\n"
                f"当日成交: 成功 {trade_success} | 失败 {trade_failure}\n"
                f"{wallet_line}"
                "说明: 当前口径优先使用账户同步后的真实已实现盈亏",
                category="pnl_profit",
                force=True,
            )
            if profit_sent:
                pnl_alerts["profit_risk_date"] = risk_date
            sent = profit_sent or sent

        if (
            self._config.notify_on_pnl_alert
            and self._config.pnl_loss_alert_usdc > 0
            and daily_pnl <= -self._config.pnl_loss_alert_usdc
            and pnl_alerts.get("loss_risk_date") != risk_date
        ):
            loss_sent = self._backend.send(
                "📉 亏损提醒\n"
                f"统计日(UTC): {risk_date}\n"
                f"已记录日盈亏: ${daily_pnl:+.2f}\n"
                f"亏损阈值: -${self._config.pnl_loss_alert_usdc:.2f}\n"
                f"当日成交: 成功 {trade_success} | 失败 {trade_failure}\n"
                f"{wallet_line}"
                "说明: 当前口径优先使用账户同步后的真实已实现盈亏",
                category="pnl_loss",
                force=True,
            )
            if loss_sent:
                pnl_alerts["loss_risk_date"] = risk_date
            sent = loss_sent or sent

        if sent:
            self._persist_state()
        return sent

    def observe_cycle(
        self,
        *,
        daily_pnl: float,
        open_positions: int,
        total_exposure: float,
        is_halted: bool,
        halt_reason: str,
        wallet_usdc: float | None = None,
        now_ts: float | None = None,
    ) -> None:
        now_ts = now_ts or time.time()
        self._ensure_daily_state(now_ts)
        snapshot = self._state.setdefault("current_risk_snapshot", {})
        snapshot["daily_pnl"] = float(daily_pnl)
        snapshot["open_positions"] = int(open_positions)
        snapshot["total_exposure"] = float(total_exposure)
        snapshot["is_halted"] = bool(is_halted)
        snapshot["halt_reason"] = halt_reason
        # Only overwrite wallet_usdc when caller has a real reading.
        # Passing None means "balance query failed this cycle" — keep
        # the last known value so the daily summary still has data.
        if wallet_usdc is not None:
            snapshot["wallet_usdc"] = float(wallet_usdc)
        self._persist_state()

    def maybe_notify_daily_summary(self, *, now_ts: float | None = None) -> bool:
        now_ts = now_ts or time.time()
        self._ensure_daily_state(now_ts)
        local_now = self._local_now(now_ts)
        local_date = local_now.date().isoformat()
        if not self._config.notify_on_daily_summary:
            return False
        if self._state.get("last_daily_summary_local_date") == local_date:
            return False
        if (local_now.hour, local_now.minute) < (self._summary_hour, self._summary_minute):
            return False

        summary = self._select_summary_payload(local_date)
        if summary is None:
            return False

        message = _format_daily_summary_message(
            summary,
            timezone_name=self._config.daily_summary_timezone,
        )
        sent = self._backend.send(message, category="daily_summary", force=True)
        if sent:
            self._state["last_daily_summary_local_date"] = local_date
            self._persist_state()
        return sent

    def _select_summary_payload(self, local_date: str) -> DailySummaryPayload | None:
        mode = "shadow" if self._config.dry_run else "live"
        completed = self._state.get("completed_daily_stats") or {}
        if completed.get("summary_local_date") == local_date:
            completed = dict(completed)
            completed.setdefault("mode", mode)
            return _coerce_summary_payload(completed)

        current_stats = dict(self._state.get("current_daily_stats") or {})
        current_snapshot = dict(self._state.get("current_risk_snapshot") or {})
        if not current_stats:
            return None
        current_stats.setdefault("summary_local_date", local_date)
        current_stats.setdefault("closing_daily_pnl", float(current_snapshot.get("daily_pnl", 0.0)))
        current_stats.setdefault("closing_open_positions", int(current_snapshot.get("open_positions", 0)))
        current_stats.setdefault("closing_total_exposure", float(current_snapshot.get("total_exposure", 0.0)))
        if "wallet_usdc" in current_snapshot:
            current_stats.setdefault("closing_wallet_usdc", float(current_snapshot["wallet_usdc"]))
        current_stats.setdefault("mode", mode)
        return _coerce_summary_payload(current_stats)

    def _ensure_daily_state(self, now_ts: float) -> None:
        risk_date = _risk_date_key(now_ts)
        current_stats = self._state.setdefault("current_daily_stats", _default_daily_stats(risk_date))
        if current_stats.get("date_key") == risk_date:
            return

        previous_stats = dict(current_stats)
        previous_snapshot = dict(self._state.get("current_risk_snapshot") or {})
        previous_stats["closing_daily_pnl"] = float(previous_snapshot.get("daily_pnl", 0.0))
        previous_stats["closing_open_positions"] = int(previous_snapshot.get("open_positions", 0))
        previous_stats["closing_total_exposure"] = float(previous_snapshot.get("total_exposure", 0.0))
        if "wallet_usdc" in previous_snapshot:
            previous_stats["closing_wallet_usdc"] = float(previous_snapshot["wallet_usdc"])
        previous_stats["mode"] = "shadow" if self._config.dry_run else "live"
        previous_stats["summary_local_date"] = self._local_now(now_ts).date().isoformat()
        self._state["completed_daily_stats"] = previous_stats
        self._state["current_daily_stats"] = _default_daily_stats(risk_date)
        # Reset risk snapshot but preserve the last wallet reading so the
        # next cycle's notification has *some* balance to show even
        # before the executor queries balance again.
        prior_wallet = previous_snapshot.get("wallet_usdc")
        self._state["current_risk_snapshot"] = {
            "daily_pnl": 0.0,
            "open_positions": 0,
            "total_exposure": 0.0,
            "is_halted": False,
            "halt_reason": "",
        }
        if prior_wallet is not None:
            self._state["current_risk_snapshot"]["wallet_usdc"] = float(prior_wallet)
        self._persist_state()

    def _load_state(self) -> dict:
        default = {
            "current_daily_stats": _default_daily_stats(_risk_date_key(time.time())),
            "completed_daily_stats": {},
            "current_risk_snapshot": {
                "daily_pnl": 0.0,
                "open_positions": 0,
                "total_exposure": 0.0,
                "is_halted": False,
                "halt_reason": "",
            },
            "pnl_alerts": {},
            "fatal_error_last_sent": {},
            "last_daily_summary_local_date": "",
        }
        if not self._state_path.exists():
            return default
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return default
            default.update(payload)
            return default
        except Exception as exc:
            LOG.warning("通知状态文件读取失败，使用默认状态: %s", exc)
            return default

    def _persist_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            LOG.warning("通知状态文件写入失败: %s", exc)

    def _local_now(self, now_ts: float) -> datetime:
        return datetime.fromtimestamp(now_ts, tz=self._summary_tz)


def _build_backend(config: ArbConfig) -> _Backend:
    if config.feishu_app_id and config.feishu_app_secret and config.feishu_open_id:
        return FeishuNotifier(config)
    return NullNotifier()


def _default_daily_stats(risk_date: str) -> dict:
    return {
        "date_key": risk_date,
        "trade_success_count": 0,
        "trade_failure_count": 0,
        "fatal_error_count": 0,
        "live_expected_profit_total": 0.0,
        "simulated_expected_profit_total": 0.0,
    }


def _risk_date_key(now_ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now_ts))


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.strip().split(":", 1)
    return int(hour_text), int(minute_text)


def _coerce_summary_payload(payload: dict) -> DailySummaryPayload:
    wallet_raw = payload.get("closing_wallet_usdc")
    wallet_usdc = float(wallet_raw) if wallet_raw is not None else None
    return DailySummaryPayload(
        risk_date=str(payload.get("date_key", "")),
        trade_success_count=int(payload.get("trade_success_count", 0)),
        trade_failure_count=int(payload.get("trade_failure_count", 0)),
        fatal_error_count=int(payload.get("fatal_error_count", 0)),
        live_expected_profit_total=float(payload.get("live_expected_profit_total", 0.0)),
        simulated_expected_profit_total=float(payload.get("simulated_expected_profit_total", 0.0)),
        closing_daily_pnl=float(payload.get("closing_daily_pnl", 0.0)),
        closing_open_positions=int(payload.get("closing_open_positions", 0)),
        closing_total_exposure=float(payload.get("closing_total_exposure", 0.0)),
        summary_local_date=str(payload.get("summary_local_date", "")),
        closing_wallet_usdc=wallet_usdc,
        mode=str(payload.get("mode", "live")),
    )


def _format_daily_summary_message(summary: DailySummaryPayload, *, timezone_name: str) -> str:
    mode_tag = "🌓 SHADOW" if summary.mode == "shadow" else "🔴 LIVE"
    if summary.closing_wallet_usdc is None:
        wallet_line = "钱包余额(USDC): N/A  [余额查询不可用 — 检查 CLOB 凭证或网络]"
    else:
        wallet_line = f"钱包余额(USDC): ${summary.closing_wallet_usdc:.2f}"
    mode_note = (
        "  [dry_run / 模拟撮合，所有成交均为 virtual_fills]"
        if summary.mode == "shadow"
        else "  [真实下单 — 数字反映链上结算]"
    )
    return (
        "🧾 每日汇总\n"
        f"模式: {mode_tag}{mode_note}\n"
        f"日期: {summary.summary_local_date} ({timezone_name}) | 统计日(UTC): {summary.risk_date}\n"
        f"交易概览: 成功 {summary.trade_success_count} | 失败 {summary.trade_failure_count} | 严重错误 {summary.fatal_error_count}\n"
        f"理论累计净利(LIVE): ${summary.live_expected_profit_total:+.4f}  [按 net_edge×size 估算，未扣滑点/手续费]\n"
        f"理论累计净利(DRY):  ${summary.simulated_expected_profit_total:+.4f}  [dry-run 模拟盘口，仅供观测]\n"
        f"真实已实现日盈亏:   ${summary.closing_daily_pnl:+.2f}  [账户同步的 closed-positions realized PnL，未含浮盈浮亏]\n"
        f"资金状态: 持仓 {summary.closing_open_positions} | 敞口 ${summary.closing_total_exposure:.2f}\n"
        f"{wallet_line}\n"
        "口径: 落袋盈亏以「真实已实现日盈亏」为准；理论累计净利仅反映信号质量，不等同实盘收益"
    )
