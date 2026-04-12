"""Kelly sizing tests."""

from polymarket_arb.strategies.kelly import kelly_binary, kelly_multi_opportunity


def test_kelly_multi_opportunity_applies_fraction_once():
    allocations = kelly_multi_opportunity(
        opportunities=[(0.75, 1.0, 1000.0)],
        bankroll=100.0,
        kelly_fraction=0.25,
    )

    # raw Kelly fraction = (0.75*1 - 0.25) / 1 = 0.5
    # quarter-Kelly should allocate 12.5 USDC, not 3.125 USDC.
    assert allocations[0] == 12.5


def test_kelly_binary_marks_below_min_bet_and_zeroes_growth():
    result = kelly_binary(
        win_prob=0.55,
        net_odds=1.0,
        bankroll=10.0,
        kelly_fraction=0.25,
        max_bet_pct=0.10,
        min_bet_usdc=1.0,
    )

    assert result.optimal_size_usdc == 0.0
    assert result.expected_growth_rate == 0.0
    assert result.warning_reason == "below_min_bet"


def test_kelly_binary_surfaces_full_bankroll_risk():
    result = kelly_binary(
        win_prob=0.99,
        net_odds=100.0,
        bankroll=100.0,
        kelly_fraction=2.0,
        max_bet_pct=1.0,
        min_bet_usdc=0.0,
    )

    assert result.adjusted_fraction == 1.0
    assert result.expected_growth_rate == float("-inf")
    assert result.warning_reason == "full_bankroll_risk"
