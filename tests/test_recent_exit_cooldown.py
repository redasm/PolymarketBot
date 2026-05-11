"""Regression: cross-restart cooldown for recently-exited markets.

Bug context: every time the operator stopped the bot, the in-memory
position tracking died with it, and the first cycle after restart
re-entered the exact same market the operator had just closed
(the statistical signal is stable on long-horizon markets).
This module pins the persistence + gate contract that prevents that.
"""

from __future__ import annotations

import json
from pathlib import Path

from polymarket_arb.strategies.recent_exit_cooldown import (
    RecentExitCooldownStore,
    make_recent_exit_cooldown_store,
)
from tests.conftest import make_test_config


def _store(tmp_path: Path, *, cooldown: float = 3600.0) -> RecentExitCooldownStore:
    return RecentExitCooldownStore(
        state_file=str(tmp_path / "recent_exits.json"),
        cooldown_sec=cooldown,
    )


class TestRecordAndGate:
    def test_unknown_market_is_not_blocked(self, tmp_path):
        store = _store(tmp_path)
        blocked, remaining = store.in_cooldown("cond-1", now_ts=1000.0)
        assert blocked is False
        assert remaining == 0.0

    def test_recording_blocks_re_entry_within_window(self, tmp_path):
        store = _store(tmp_path, cooldown=3600.0)
        store.record_exit("cond-1", now_ts=1000.0)
        blocked, remaining = store.in_cooldown("cond-1", now_ts=1500.0)
        assert blocked is True
        assert 3098.0 <= remaining <= 3102.0  # 3600 - 500 ≈ 3100

    def test_window_expiry_unblocks(self, tmp_path):
        store = _store(tmp_path, cooldown=3600.0)
        store.record_exit("cond-1", now_ts=1000.0)
        blocked, _ = store.in_cooldown("cond-1", now_ts=1000.0 + 3601.0)
        assert blocked is False

    def test_zero_cooldown_is_no_op(self, tmp_path):
        store = _store(tmp_path, cooldown=0.0)
        store.record_exit("cond-1", now_ts=1000.0)
        blocked, _ = store.in_cooldown("cond-1", now_ts=1001.0)
        assert blocked is False
        assert store.snapshot() == {}


class TestPersistence:
    def test_survives_restart(self, tmp_path):
        store_a = _store(tmp_path, cooldown=7200.0)
        store_a.record_exit("cond-A", now_ts=1000.0)
        # Simulate a fresh process loading the same file.
        store_b = _store(tmp_path, cooldown=7200.0)
        blocked, _ = store_b.in_cooldown("cond-A", now_ts=2000.0)
        assert blocked is True

    def test_corrupt_state_file_starts_empty_and_recovers(self, tmp_path):
        state_file = tmp_path / "recent_exits.json"
        state_file.write_text("{not valid json", encoding="utf-8")
        store = RecentExitCooldownStore(
            state_file=str(state_file), cooldown_sec=600.0
        )
        # Bot must not crash on corrupt state.
        assert store.snapshot() == {}
        # And must still be able to record new exits cleanly.
        store.record_exit("cond-X", now_ts=10.0)
        assert "cond-X" in store.snapshot()
        # The corrupt file should now be replaced with valid JSON.
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        assert "cond-X" in payload["entries"]

    def test_old_entries_pruned_on_write(self, tmp_path):
        store = _store(tmp_path, cooldown=3600.0)
        # An entry from 3h ago — past 2× cooldown.
        store.record_exit("cond-old", now_ts=1000.0)
        # Move forward, recording a new entry. Old one should be pruned.
        store.record_exit("cond-new", now_ts=1000.0 + 3 * 3600.0 + 1.0)
        snap = store.snapshot()
        assert "cond-old" not in snap
        assert "cond-new" in snap


class TestFactory:
    def test_factory_reads_config_fields(self, tmp_path):
        cfg = make_test_config(
            t2_recent_exits_state_file=str(tmp_path / "x.json"),
            t2_post_exit_cooldown_sec=1234.0,
        )
        store = make_recent_exit_cooldown_store(cfg)
        assert store.cooldown_sec == 1234.0

    def test_factory_handles_missing_fields(self, tmp_path):
        cfg = make_test_config(
            t2_recent_exits_state_file="",
            t2_post_exit_cooldown_sec=0.0,
        )
        store = make_recent_exit_cooldown_store(cfg)
        assert store.cooldown_sec == 0.0
        # No-op record + check on a disabled store should not raise.
        store.record_exit("cond-1", now_ts=1.0)
        blocked, _ = store.in_cooldown("cond-1", now_ts=2.0)
        assert blocked is False
