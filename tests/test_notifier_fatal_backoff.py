"""Regression: fatal-error notifications use exponential backoff per key.

Bug context: the T2 exit retry loop produced 12 fatal_error notifications
in 14 hours (default cooldown was 300s = 5min). Each retry escalation
on the same underlying incident counted as a fresh fatal event in the
daily summary, inflating "严重错误 12" for a single root cause.

The fix: per-key exponential backoff (base × 2^(N-1), capped at 24h)
plus a 1h base default. ``fatal_error_count`` now reflects actual sends.
"""

from __future__ import annotations

from polymarket_arb.notifier import NotificationManager
from tests.conftest import make_test_config


def _make_manager(tmp_path, *, cooldown=60.0):
    sent: list[tuple[str, str]] = []
    manager = NotificationManager(
        make_test_config(
            notification_state_file=str(tmp_path / "notification_state.json"),
            fatal_error_cooldown_sec=cooldown,
            notify_on_fatal_error=True,
        )
    )
    manager._backend = type(
        "_Backend",
        (),
        {"send": lambda self, message, category="general", force=False: sent.append((category, message)) or True},
    )()
    return manager, sent


class TestExponentialBackoff:
    def test_first_send_passes_through(self, tmp_path):
        mgr, sent = _make_manager(tmp_path, cooldown=60.0)
        ok = mgr.notify_fatal_error("boom", error_key="t2_exit:abc", now_ts=1000.0)
        assert ok is True
        assert len(sent) == 1
        assert mgr._state["current_daily_stats"]["fatal_error_count"] == 1

    def test_second_send_blocked_within_one_cooldown(self, tmp_path):
        mgr, sent = _make_manager(tmp_path, cooldown=60.0)
        mgr.notify_fatal_error("boom", error_key="t2_exit:abc", now_ts=1000.0)
        ok = mgr.notify_fatal_error("boom", error_key="t2_exit:abc", now_ts=1059.0)
        assert ok is False
        assert len(sent) == 1
        # Count must not grow when nothing was actually sent.
        assert mgr._state["current_daily_stats"]["fatal_error_count"] == 1

    def test_backoff_doubles_each_send(self, tmp_path):
        mgr, sent = _make_manager(tmp_path, cooldown=60.0)
        # Send #1 at t=1000 (cooldown 0)
        mgr.notify_fatal_error("e", error_key="k", now_ts=1000.0)
        # Send #2 requires ≥ 60s after #1 → at t=1060 it goes through
        assert mgr.notify_fatal_error("e", error_key="k", now_ts=1060.0) is True
        # Send #3 requires ≥ 120s after #2 → at t=1119 still blocked
        assert mgr.notify_fatal_error("e", error_key="k", now_ts=1119.0) is False
        # At t=1180 (120s after #2) it goes through
        assert mgr.notify_fatal_error("e", error_key="k", now_ts=1180.0) is True
        # Send #4 requires ≥ 240s after #3 → at t=1300 still blocked
        assert mgr.notify_fatal_error("e", error_key="k", now_ts=1300.0) is False
        # At t=1420 (240s after #3) it goes through
        assert mgr.notify_fatal_error("e", error_key="k", now_ts=1420.0) is True
        assert len(sent) == 4
        assert mgr._state["current_daily_stats"]["fatal_error_count"] == 4

    def test_separate_keys_have_independent_backoff(self, tmp_path):
        mgr, sent = _make_manager(tmp_path, cooldown=60.0)
        # First send for two different keys at the same instant — both go.
        assert mgr.notify_fatal_error("a", error_key="k1", now_ts=1000.0) is True
        assert mgr.notify_fatal_error("b", error_key="k2", now_ts=1000.0) is True
        assert len(sent) == 2

    def test_14h_incident_emits_only_log2_of_14_alerts(self, tmp_path):
        """The actual prod scenario: a 14h sustained T2 exit loop. The
        old code (5min cooldown, no per-key backoff) sent 12 alerts.
        With a 1h base + 2× backoff we send 4 — once at 0, +1h, +2h,
        +4h. The next slot would be +8h after the 4th send (t=14h+),
        which falls outside the window."""
        mgr, sent = _make_manager(tmp_path, cooldown=3600.0)
        start = 1000.0
        # Hammer the same key once per minute for 14 hours.
        for minute in range(14 * 60):
            mgr.notify_fatal_error("boom", error_key="incident", now_ts=start + minute * 60)
        # Backoff schedule (minutes from start): 0, 60, 180, 420 → 4 sends.
        # Send #5 would need t ≥ 420 + 8h = 900min; 14h = 840min, so no #5.
        assert len(sent) == 4
        assert mgr._state["current_daily_stats"]["fatal_error_count"] == 4
