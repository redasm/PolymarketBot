"""Tests for daily-realized PnL rollover in `ShadowPositionLifecycle`.

Regression context: in shadow (dry_run) mode, `realized_daily_pnl` in
cycle_metrics stayed frozen at a cumulative value across UTC day boundaries
(observed 2.3297 for 4 consecutive days). Root cause: the lifecycle only
tracked cumulative `_realized_pnl`, and `risk_manager._apply_shadow_snapshot_locked`
mapped that cumulative value onto `daily_pnl`, overwriting the value
`_maybe_reset_daily` had just zeroed.

These tests pin the fix: the lifecycle now tracks a separate day-resetting
`_daily_realized_pnl`, rolled over inside the every-cycle `snapshot()` call so
it zeroes even when no new fill occurs after midnight.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import polymarket_arb.main_helpers.shadow_position_lifecycle as slc_mod
from polymarket_arb.event_recorder import EventRecorder
from polymarket_arb.main_helpers.shadow_position_lifecycle import ShadowPositionLifecycle
from polymarket_arb.models import OrderSide, TradeRecord, TradeStatus


class _Clock:
    """Mutable wall-clock used to drive UTC-day rollover deterministically."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def advance_days(self, n: float) -> None:
        self.now += n * 86400.0


def _patch_clock(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    def fake_day_start(now_ts: float | None = None) -> float:
        ts = clock.now if now_ts is None else now_ts
        # Epoch is UTC midnight, so flooring to 86400 yields the UTC day start.
        return (ts // 86400) * 86400

    monkeypatch.setattr(slc_mod, "_utc_day_start", fake_day_start)


def _ledger(tmp_path: Path) -> ShadowPositionLifecycle:
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=False)
    return ShadowPositionLifecycle(event_recorder=recorder)


def _buy(token_id: str, condition_id: str, price: float, size: float, *, trade_id: str = "b") -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id, arb_id="a", token_id=token_id, condition_id=condition_id,
        side=OrderSide.BUY, price=price, size=size, status=TradeStatus.FILLED,
        fill_price=price, fill_size=size,
    )


def _sell(token_id: str, condition_id: str, price: float, size: float, *, trade_id: str = "s") -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id, arb_id="a", token_id=token_id, condition_id=condition_id,
        side=OrderSide.SELL, price=price, size=size, status=TradeStatus.FILLED,
        fill_price=price, fill_size=size,
    )


def test_snapshot_exposes_daily_realized_pnl_key(tmp_path: Path) -> None:
    """snapshot() must always emit the key so risk_manager never hits the fallback."""
    snap = _ledger(tmp_path).snapshot()
    assert "daily_realized_pnl" in snap
    assert snap["daily_realized_pnl"] == 0.0


def test_daily_realized_equals_cumulative_within_same_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0, trade_id="b1"))
    led.record_fill(_sell("t1", "c1", price=0.60, size=4.0, trade_id="s1"))  # +0.40
    snap = led.snapshot()
    assert snap["realized_pnl"] == pytest.approx(0.40)
    assert snap["daily_realized_pnl"] == pytest.approx(0.40)


def test_daily_realized_resets_at_utc_midnight_via_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Core regression: cross midnight, take NO new fill, snapshot() must zero
    daily_realized while cumulative realized is preserved (the 4-day-freeze bug)."""
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0, trade_id="b1"))
    led.record_fill(_sell("t1", "c1", price=0.60, size=4.0, trade_id="s1"))  # +0.40 today
    assert led.snapshot()["daily_realized_pnl"] == pytest.approx(0.40)

    clock.advance_days(1)  # cross UTC midnight, no new trade
    snap = led.snapshot()
    assert snap["daily_realized_pnl"] == pytest.approx(0.0)   # rolled over
    assert snap["realized_pnl"] == pytest.approx(0.40)        # cumulative preserved


def test_close_after_midnight_books_into_new_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0, trade_id="b1"))
    led.record_fill(_sell("t1", "c1", price=0.60, size=4.0, trade_id="s1"))  # +0.40 day0

    clock.advance_days(1)
    led.record_fill(_buy("t2", "c2", price=0.20, size=5.0, trade_id="b2"))
    led.record_fill(_sell("t2", "c2", price=0.30, size=5.0, trade_id="s2"))  # +0.50 day1
    snap = led.snapshot()
    assert snap["daily_realized_pnl"] == pytest.approx(0.50)   # only the new day
    assert snap["realized_pnl"] == pytest.approx(0.90)         # cumulative both days


def test_daily_realized_property_rolls_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    led = _ledger(tmp_path)
    led.record_fill(_buy("t1", "c1", price=0.50, size=4.0, trade_id="b1"))
    led.record_fill(_sell("t1", "c1", price=0.60, size=4.0, trade_id="s1"))
    assert led.daily_realized_pnl == pytest.approx(0.40)
    clock.advance_days(1)
    assert led.daily_realized_pnl == pytest.approx(0.0)
    assert led.realized_pnl == pytest.approx(0.40)
