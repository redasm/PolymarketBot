"""T3 抗狙击保护.

做市的主要成本是逆向选择而不是 spread。这些用例锁住五道保护各自的行为，
以及两个曾经写错过的关键点：跳变要对比**原始** mid（对比滤波值会让 token
永久停在暂停态），以及恢复报价时必须重新播种滤波器（否则拿跳变前的旧价
位挂单，保护措施反而制造了它要防的那次被吃）。
"""

from __future__ import annotations

import pytest

from polymarket_arb.strategies.maker_anti_snipe import (
    AntiSnipeConfig,
    AntiSnipeGuard,
)


def _guard(**kwargs) -> AntiSnipeGuard:
    return AntiSnipeGuard(AntiSnipeConfig(**kwargs))


# --------- 跳变暂停 ----------


def test_small_moves_are_allowed():
    guard = _guard()
    assert guard.evaluate("t", 0.50, 0.0).allow is True
    # 1 tick 的移动是常态，不该暂停。
    assert guard.evaluate("t", 0.51, 1.0).allow is True


def test_large_jump_pauses_quoting():
    guard = _guard(jump_pause_ticks=3.0, jump_pause_sec=20.0)
    guard.evaluate("t", 0.50, 0.0)
    decision = guard.evaluate("t", 0.60, 1.0)
    assert decision.allow is False
    assert decision.reason == "mid_jump"
    assert decision.jump_ticks == pytest.approx(10.0)
    assert guard.evaluate("t", 0.60, 5.0).reason == "mid_jump_pause"


def test_jump_is_measured_against_raw_mid_not_filtered():
    """回归：对比滤波值会让跳变后的 jump 永远超阈值，token 永久暂停."""
    guard = _guard(jump_pause_ticks=3.0, jump_pause_sec=5.0, stable_ticks_required=1)
    guard.evaluate("t", 0.50, 0.0)
    guard.evaluate("t", 0.70, 1.0)  # 跳变，进入暂停
    # 新价位稳定后，jump_ticks 应该回到 0 —— 而不是继续对比滞后的 EMA。
    after = guard.evaluate("t", 0.70, 10.0)
    assert after.jump_ticks == pytest.approx(0.0)
    assert after.allow is True


def test_stable_confirmation_required_after_pause():
    guard = _guard(
        jump_pause_ticks=3.0,
        jump_pause_sec=5.0,
        stable_ticks_required=2,
        stable_band_ticks=1.0,
    )
    guard.evaluate("t", 0.50, 0.0)
    guard.evaluate("t", 0.70, 1.0)
    assert guard.evaluate("t", 0.70, 10.0).reason == "awaiting_stable_mid"
    assert guard.evaluate("t", 0.70, 11.0).allow is True


def test_choppy_mid_resets_the_stable_streak():
    guard = _guard(
        jump_pause_ticks=5.0,
        jump_pause_sec=5.0,
        stable_ticks_required=2,
        stable_band_ticks=1.0,
    )
    guard.evaluate("t", 0.50, 0.0)
    guard.evaluate("t", 0.70, 1.0)
    guard.evaluate("t", 0.70, 10.0)  # streak = 1
    guard.evaluate("t", 0.73, 11.0)  # 3 tick 抖动 → streak 归零
    assert guard.evaluate("t", 0.73, 12.0).allow is False


def test_filter_is_reseeded_when_quoting_resumes():
    """回归：恢复时若沿用滞后的 EMA，会在新行情下挂一个明显偏离的单."""
    guard = _guard(
        jump_pause_ticks=3.0,
        jump_pause_sec=5.0,
        stable_ticks_required=1,
        ema_alpha=0.2,
    )
    guard.evaluate("t", 0.50, 0.0)
    guard.evaluate("t", 0.70, 1.0)
    resumed = guard.evaluate("t", 0.70, 10.0)
    assert resumed.allow is True
    # 锚点必须已经跟上新价位，而不是停在 0.5x。
    assert resumed.filtered_mid == pytest.approx(0.70, abs=0.02)


# --------- 滤波 ----------


def test_median_filter_absorbs_a_single_outlier_tick():
    guard = _guard(jump_pause_ticks=1000.0, ema_alpha=0.0, use_median=True)
    for ts, mid in enumerate([0.50, 0.50, 0.50, 0.50, 0.50]):
        guard.evaluate("t", mid, float(ts))
    spike = guard.evaluate("t", 0.80, 10.0)
    # 单个异常 tick 不该把报价锚点拉到 0.80。
    assert spike.filtered_mid == pytest.approx(0.50)
    assert spike.raw_mid == pytest.approx(0.80)


def test_ema_smooths_the_anchor():
    guard = _guard(jump_pause_ticks=1000.0, ema_alpha=0.5, use_median=False)
    guard.evaluate("t", 0.50, 0.0)
    second = guard.evaluate("t", 0.60, 1.0)
    assert 0.50 < second.filtered_mid < 0.60


# --------- 成交冷却 ----------


def test_post_fill_cooldown_blocks_requoting():
    guard = _guard(post_fill_cooldown_sec=15.0)
    guard.evaluate("t", 0.50, 0.0)
    guard.register_fill("t", 1.0)
    assert guard.evaluate("t", 0.50, 5.0).reason == "post_fill_cooldown"
    assert guard.evaluate("t", 0.50, 20.0).allow is True


def test_cooldown_is_per_token():
    guard = _guard(post_fill_cooldown_sec=15.0)
    guard.evaluate("a", 0.50, 0.0)
    guard.evaluate("b", 0.50, 0.0)
    guard.register_fill("a", 1.0)
    assert guard.evaluate("a", 0.50, 5.0).allow is False
    assert guard.evaluate("b", 0.50, 5.0).allow is True


# --------- 追价上限 ----------


def test_chase_limit_caps_quote_movement():
    guard = _guard(max_chase_ticks=2.0)
    guard.clamp_chase("t", bid=0.50, ask=0.52, tick_size=0.01)
    bid, ask = guard.clamp_chase("t", bid=0.60, ask=0.62, tick_size=0.01)
    assert bid == pytest.approx(0.52)
    assert ask == pytest.approx(0.54)
    assert guard.stats()["chase_clamped"] == 1


def test_chase_limit_allows_small_moves_untouched():
    guard = _guard(max_chase_ticks=2.0)
    guard.clamp_chase("t", bid=0.50, ask=0.52, tick_size=0.01)
    bid, ask = guard.clamp_chase("t", bid=0.51, ask=0.53, tick_size=0.01)
    assert (bid, ask) == (pytest.approx(0.51), pytest.approx(0.53))
    assert guard.stats()["chase_clamped"] == 0


def test_chase_limit_handles_missing_sides():
    guard = _guard(max_chase_ticks=2.0)
    guard.clamp_chase("t", bid=0.50, ask=None, tick_size=0.01)
    bid, ask = guard.clamp_chase("t", bid=None, ask=0.90, tick_size=0.01)
    assert bid is None
    # 上一次没有 ask，无从限制，直接放行。
    assert ask == pytest.approx(0.90)


def test_chase_limit_scales_with_tick_size():
    guard = _guard(max_chase_ticks=2.0)
    guard.clamp_chase("t", bid=0.500, ask=0.502, tick_size=0.001)
    bid, _ = guard.clamp_chase("t", bid=0.600, ask=0.602, tick_size=0.001)
    assert bid == pytest.approx(0.502)


# --------- 关闭与边界 ----------


def test_disabled_guard_allows_everything():
    guard = _guard(enabled=False)
    guard.evaluate("t", 0.50, 0.0)
    decision = guard.evaluate("t", 0.90, 1.0)
    assert decision.allow is True
    assert decision.filtered_mid == pytest.approx(0.90)
    guard.register_fill("t", 1.0)
    assert guard.evaluate("t", 0.90, 2.0).allow is True
    assert guard.clamp_chase("t", bid=0.10, ask=0.95, tick_size=0.01) == (0.10, 0.95)


def test_invalid_mid_is_blocked():
    guard = _guard()
    assert guard.evaluate("t", 0.0, 0.0).reason == "invalid_mid"
    assert guard.evaluate("t", None, 0.0).allow is False


def test_reset_clears_token_state():
    guard = _guard(post_fill_cooldown_sec=60.0)
    guard.evaluate("t", 0.50, 0.0)
    guard.register_fill("t", 0.0)
    guard.reset("t")
    assert guard.evaluate("t", 0.50, 1.0).allow is True


def test_stats_track_block_reasons():
    guard = _guard(jump_pause_ticks=3.0, post_fill_cooldown_sec=10.0)
    guard.evaluate("t", 0.50, 0.0)
    guard.evaluate("t", 0.70, 1.0)
    stats = guard.stats()
    assert stats["evaluated"] == 2
    assert stats["blocked_jump"] == 1
    assert stats["tracked_tokens"] == 1
