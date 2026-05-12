from __future__ import annotations

from datetime import datetime, timezone

from polymarket_arb.notifier import NotificationManager
from tests.conftest import make_test_config


def _ts(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_notification_manager_sends_pnl_alert_only_once_per_risk_day(tmp_path):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(
            notification_state_file=str(tmp_path / "notification_state.json"),
            pnl_profit_alert_usdc=20.0,
            pnl_loss_alert_usdc=10.0,
        )
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()

    ts_day1 = _ts(2026, 4, 17, 10, 0)
    assert manager.maybe_notify_pnl_alert(daily_pnl=25.0, now_ts=ts_day1) is True
    assert manager.maybe_notify_pnl_alert(daily_pnl=25.0, now_ts=ts_day1 + 60) is False

    ts_day2 = _ts(2026, 4, 18, 10, 0)
    assert manager.maybe_notify_pnl_alert(daily_pnl=25.0, now_ts=ts_day2) is True
    assert [category for category, _ in sent] == ["pnl_profit", "pnl_profit"]


def test_notification_manager_daily_summary_uses_completed_day_stats_after_rollover(tmp_path):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(
            notification_state_file=str(tmp_path / "notification_state.json"),
            daily_summary_time_hhmm="08:05",
            daily_summary_timezone="Asia/Shanghai",
            dry_run=False,
        )
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()

    ts_before_rollover = _ts(2026, 4, 17, 23, 50)
    manager.notify_trade_success(
        event_title="BTC event",
        arb_type="T0",
        filled_legs=2,
        total_legs=2,
        expected_profit=1.25,
        simulated=False,
        now_ts=ts_before_rollover,
    )
    manager.notify_trade_failure(
        event_title="ETH event",
        filled_legs=1,
        total_legs=2,
        simulated=False,
        details="hedge_incomplete",
        now_ts=ts_before_rollover,
    )
    manager.notify_fatal_error(
        "连续 API 错误",
        error_key="api_error_streak",
        now_ts=ts_before_rollover,
    )
    manager.observe_cycle(
        daily_pnl=3.5,
        open_positions=2,
        total_exposure=42.0,
        is_halted=False,
        halt_reason="",
        wallet_usdc=123.45,
        now_ts=ts_before_rollover,
    )

    ts_after_rollover = _ts(2026, 4, 18, 0, 6)
    manager.observe_cycle(
        daily_pnl=0.0,
        open_positions=1,
        total_exposure=10.0,
        is_halted=False,
        halt_reason="",
        now_ts=ts_after_rollover,
    )

    assert manager.maybe_notify_daily_summary(now_ts=ts_after_rollover) is True
    assert manager.maybe_notify_daily_summary(now_ts=ts_after_rollover + 60) is False

    daily_messages = [message for category, message in sent if category == "daily_summary"]
    assert len(daily_messages) == 1
    assert "统计日(UTC): 2026-04-17" in daily_messages[0]
    assert "交易概览: 成功 1 | 失败 1 | 严重错误 1" in daily_messages[0]
    assert "理论累计净利(LIVE): $+1.2500" in daily_messages[0]
    assert "真实已实现日盈亏:   $+3.50" in daily_messages[0]
    assert "资金状态: 持仓 2 | 敞口 $42.00" in daily_messages[0]
    assert "钱包余额(USDC): $123.45" in daily_messages[0]
    # Default test config has dry_run=False, so mode tag must be LIVE.
    assert "🔴 LIVE" in daily_messages[0]


def test_notification_manager_formats_startup_and_shutdown_messages(tmp_path):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(
            notification_state_file=str(tmp_path / "notification_state.json"),
        )
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()

    assert manager.notify_startup(
        mode="LIVE",
        min_profit_usd=0.01,
        min_profit_pct=0.6,
        scan_interval_sec=3.0,
        ws_enabled=True,
        portfolio_sync_enabled=True,
        max_order_size_usdc=10.0,
        max_exposure_per_market=25.0,
        max_total_exposure=100.0,
        max_daily_loss=10.0,
        max_open_positions=3,
    ) is True
    assert manager.notify_shutdown(
        run_id="run-1",
        cycle_count=10,
        total_t0_opportunities=2,
        total_directional_signals=3,
        total_arbs_executed=2,
        simulated_successes=1,
    ) is True

    assert sent[0][0] == "startup"
    assert "账户同步: 启用" in sent[0][1]
    assert sent[1][0] == "shutdown"
    assert "实例: run-1" in sent[1][1]
    # T0 vs directional split should make it into the shutdown payload
    # so operators can distinguish "no T0 detected" from "no signal at
    # all" without diffing telemetry NDJSON.
    assert "T0 结构机会: 2" in sent[1][1]
    assert "定向信号(T1/T2/T3): 3" in sent[1][1]


def test_daily_summary_renders_shadow_mode_tag_and_unknown_wallet(tmp_path):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(
            notification_state_file=str(tmp_path / "notification_state.json"),
            daily_summary_time_hhmm="08:05",
            daily_summary_timezone="Asia/Shanghai",
            dry_run=True,
        )
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()

    ts_before = _ts(2026, 4, 17, 23, 50)
    # Note: no wallet_usdc passed to observe_cycle → daily summary must
    # fall back to the "N/A" wallet line instead of fabricating $0.00.
    manager.observe_cycle(
        daily_pnl=0.0,
        open_positions=0,
        total_exposure=0.0,
        is_halted=False,
        halt_reason="",
        now_ts=ts_before,
    )
    ts_after = _ts(2026, 4, 18, 0, 6)
    manager.observe_cycle(
        daily_pnl=0.0,
        open_positions=0,
        total_exposure=0.0,
        is_halted=False,
        halt_reason="",
        now_ts=ts_after,
    )
    assert manager.maybe_notify_daily_summary(now_ts=ts_after) is True
    msg = [m for c, m in sent if c == "daily_summary"][0]
    assert "🌓 SHADOW" in msg
    assert "钱包余额(USDC): N/A" in msg
    assert "virtual_fills" in msg


def test_trade_success_appends_capital_snapshot_when_known(tmp_path):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(notification_state_file=str(tmp_path / "notification_state.json"))
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()

    ts = _ts(2026, 4, 17, 12, 0)
    manager.observe_cycle(
        daily_pnl=1.5,
        open_positions=3,
        total_exposure=27.5,
        is_halted=False,
        halt_reason="",
        wallet_usdc=200.0,
        now_ts=ts,
    )
    manager.notify_trade_success(
        event_title="BTC milestone",
        arb_type="T0",
        filled_legs=2,
        total_legs=2,
        expected_profit=0.42,
        simulated=False,
        now_ts=ts + 5,
    )
    msg = [m for c, m in sent if c == "trade_success"][0]
    assert "持仓/敞口: 3 / $27.50" in msg
    assert "钱包(USDC): $200.00" in msg


def test_pnl_alert_includes_trade_count_and_wallet(tmp_path):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(
            notification_state_file=str(tmp_path / "notification_state.json"),
            pnl_profit_alert_usdc=5.0,
        )
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()

    ts = _ts(2026, 4, 17, 12, 0)
    manager.observe_cycle(
        daily_pnl=10.0,
        open_positions=2,
        total_exposure=30.0,
        is_halted=False,
        halt_reason="",
        wallet_usdc=180.0,
        now_ts=ts,
    )
    # Simulate one success + one failure on this risk-day.
    manager.notify_trade_success(
        event_title="x", arb_type="T2", filled_legs=1, total_legs=1,
        expected_profit=2.0, simulated=False, now_ts=ts,
    )
    manager.notify_trade_failure(
        event_title="y", filled_legs=0, total_legs=1,
        simulated=False, now_ts=ts,
    )
    assert manager.maybe_notify_pnl_alert(daily_pnl=10.0, now_ts=ts + 1) is True
    msg = [m for c, m in sent if c == "pnl_profit"][0]
    assert "当日成交: 成功 1 | 失败 1" in msg
    assert "钱包(USDC): $180.00" in msg


def test_fatal_error_appends_context_lines(tmp_path):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(notification_state_file=str(tmp_path / "notification_state.json"))
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()

    assert manager.notify_fatal_error(
        "风控已熔断\n原因: 连续失败 3 次",
        error_key="risk_halt:consecutive",
        context_lines=[
            "连续失败: 3/3",
            "自动恢复: 剩余 3540s / 3600s（无新失败即解除）",
        ],
        now_ts=_ts(2026, 4, 17, 12, 0),
    ) is True
    msg = [m for c, m in sent if c.startswith("fatal_error:")][0]
    assert "连续失败: 3/3" in msg
    assert "自动恢复: 剩余 3540s" in msg
