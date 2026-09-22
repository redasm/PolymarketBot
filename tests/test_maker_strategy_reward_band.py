"""MakerStrategy 奖励带 clamp.

历史 bug: `reward_delta` 全仓库没有第二处赋值，恒为 0，clamp
(maker_strategy.compute_quote 内的 reward_lo/reward_hi 分支) 从未执行 —— T3
在不知道市场计分区间的情况下报价。这些用例锁死 clamp 的行为。
"""

from __future__ import annotations

import pytest

from polymarket_arb.strategies.maker_strategy import (
    DynamicSpreadCalculator,
    MakerStrategy,
)


def _strategy(**kwargs) -> MakerStrategy:
    return MakerStrategy(
        spread_calc=DynamicSpreadCalculator(base_spread_ticks=2.0),
        default_size=10.0,
        max_inventory=100.0,
        **kwargs,
    )


def test_reward_band_pulls_quotes_inside_scoring_range():
    strat = _strategy()
    wide = strat.compute_quote(
        token_id="t", condition_id="c", fair_value=0.50, tick_size=0.01, mid_price=0.50
    )
    clamped = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
        reward_delta=0.01,
    )
    assert wide.bid_price < clamped.bid_price
    assert wide.ask_price > clamped.ask_price
    assert clamped.bid_price >= 0.50 - 0.01 - 1e-9
    assert clamped.ask_price <= 0.50 + 0.01 + 1e-9


def test_reward_band_never_widens_an_already_inside_quote():
    """已在带内的报价不该被奖励带推宽（clamp 是单向收窄）."""
    strat = _strategy()
    baseline = strat.compute_quote(
        token_id="t", condition_id="c", fair_value=0.50, tick_size=0.01, mid_price=0.50
    )
    generous = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
        reward_delta=0.20,
    )
    assert generous.bid_price == pytest.approx(baseline.bid_price)
    assert generous.ask_price == pytest.approx(baseline.ask_price)


def test_reward_band_anchors_on_mid_not_fair_value():
    """计分区间以 mid 为中心，不是以模型 fair value 为中心."""
    strat = _strategy()
    quote = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.52,
        tick_size=0.01,
        mid_price=0.50,
        reward_delta=0.02,
    )
    assert quote.bid_price is not None
    assert 0.48 - 1e-9 <= quote.bid_price <= 0.50 + 1e-9
    assert quote.ask_price is not None
    assert 0.50 - 1e-9 <= quote.ask_price <= 0.52 + 1e-9


def test_reward_band_never_produces_a_crossed_quote():
    """回归: fair value 远离 mid 时，旧 clamp 会报出 bid > ask.

    旧实现只抬低 bid / 压高 ask，fair=0.60 而 mid=0.50 时 bid 停在
    0.57、ask 被压到 0.52，spread 变成负数。δ 恒为 0 时这段代码从不
    执行，所以缺陷一直藏着；接入 rewards_max_spread 后立刻会触发。
    """
    strat = _strategy()
    quote = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.60,
        tick_size=0.01,
        mid_price=0.50,
        reward_delta=0.02,
    )
    assert quote is not None
    if quote.bid_price is not None and quote.ask_price is not None:
        assert quote.bid_price < quote.ask_price
    assert quote.spread >= 0


def test_reward_band_drops_the_negative_ev_side():
    """压进带内会让 ask 低于 fair value 时，撤掉卖侧而不是亏着挂."""
    strat = _strategy()
    quote = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.60,
        tick_size=0.01,
        mid_price=0.50,
        reward_delta=0.02,
    )
    # 卖 0.52 而自己估值 0.60 = 负期望，必须撤掉。
    assert quote.ask_price is None
    # 买侧仍然成立（0.50 <= 0.60），继续提供流动性并计分。
    assert quote.bid_price is not None
    assert quote.bid_price <= 0.60


def test_reward_band_drops_negative_ev_bid_when_fair_value_is_low():
    strat = _strategy()
    quote = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.40,
        tick_size=0.01,
        mid_price=0.50,
        reward_delta=0.02,
    )
    assert quote.bid_price is None
    assert quote.ask_price is not None
    assert quote.ask_price >= 0.40


def test_zero_delta_is_legacy_behaviour():
    strat = _strategy()
    legacy = strat.compute_quote(
        token_id="t", condition_id="c", fair_value=0.50, tick_size=0.01, mid_price=0.50
    )
    explicit_zero = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=0.50,
        reward_delta=0.0,
    )
    assert explicit_zero.bid_price == pytest.approx(legacy.bid_price)
    assert explicit_zero.ask_price == pytest.approx(legacy.ask_price)


def test_constructor_default_delta_applies_without_per_call_override():
    strat = _strategy(reward_delta=0.01)
    quote = strat.compute_quote(
        token_id="t", condition_id="c", fair_value=0.50, tick_size=0.01, mid_price=0.50
    )
    assert quote.bid_price >= 0.49 - 1e-9
    assert quote.ask_price <= 0.51 + 1e-9


def test_clamp_requires_mid_price():
    """没有 mid 就没有带心，clamp 必须整体跳过而不是崩溃."""
    strat = _strategy()
    quote = strat.compute_quote(
        token_id="t",
        condition_id="c",
        fair_value=0.50,
        tick_size=0.01,
        mid_price=None,
        reward_delta=0.01,
    )
    assert quote is not None
    assert quote.bid_price is not None and quote.ask_price is not None
