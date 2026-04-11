"""FairValueModel 单元测试：用已知解析解验证定价公式."""

import math

import pytest

from polymarket_arb.fair_value_model import (
    _standard_normal_cdf,
    compute_edge_bps,
    compute_fair_updown,
    compute_general_fair_value,
)


class TestStandardNormalCDF:
    def test_symmetry(self):
        assert _standard_normal_cdf(0) == pytest.approx(0.5, abs=1e-6)

    def test_positive(self):
        assert _standard_normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)

    def test_negative(self):
        assert _standard_normal_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)

    def test_large(self):
        assert _standard_normal_cdf(5.0) > 0.9999


class TestComputeFairUpdown:
    """用手算的 z-score 验证 GBM 定价."""

    def test_price_above_ref_high_fair_up(self):
        """S_now > ref_px → fair_up 应 > 0.5."""
        result = compute_fair_updown(
            s_now=105_000, ref_px=104_000, sigma_15m=0.003, tau_sec=600, window_sec=900,
        )
        assert result["fair_up"] > 0.5
        assert result["fair_down"] < 0.5
        assert abs(result["fair_up"] + result["fair_down"] - 1.0) < 1e-4

    def test_price_below_ref_low_fair_up(self):
        """S_now < ref_px → fair_up 应 < 0.5."""
        result = compute_fair_updown(
            s_now=99_000, ref_px=100_000, sigma_15m=0.003, tau_sec=600,
        )
        assert result["fair_up"] < 0.5

    def test_price_equals_ref(self):
        """S_now == ref_px, drift=0 → fair_up ≈ 0.5."""
        result = compute_fair_updown(
            s_now=100_000, ref_px=100_000, sigma_15m=0.003, tau_sec=450,
        )
        assert result["fair_up"] == pytest.approx(0.5, abs=0.02)

    def test_tau_near_zero(self):
        """快到期、价格高于参考 → fair_up 接近 1."""
        result = compute_fair_updown(
            s_now=101_000, ref_px=100_000, sigma_15m=0.003, tau_sec=0.5,
        )
        assert result["fair_up"] >= 0.9

    def test_sigma_too_low(self):
        """sigma < MIN_SIGMA → 退化为方向判断."""
        result = compute_fair_updown(
            s_now=101_000, ref_px=100_000, sigma_15m=0.00001, tau_sec=600,
        )
        assert result["fair_up"] == 0.6
        assert result["inputs"]["reason"] == "sigma_too_low"

    def test_invalid_prices(self):
        result = compute_fair_updown(s_now=0, ref_px=100_000, sigma_15m=0.003, tau_sec=600)
        assert result["fair_up"] == 0.5

    def test_manual_z_score(self):
        """手算: ln(105000/104000) / (0.003 × √(600/900)) ≈ 3.20 → Φ ≈ 0.9993."""
        result = compute_fair_updown(
            s_now=105_000, ref_px=104_000, sigma_15m=0.003, tau_sec=600, window_sec=900,
        )
        expected_z = math.log(105_000 / 104_000) / (0.003 * math.sqrt(600 / 900))
        assert result["z_score"] == pytest.approx(expected_z, abs=0.01)


class TestComputeEdgeBps:
    def test_positive_edge(self):
        assert compute_edge_bps(0.55, 0.50) == pytest.approx(500.0, abs=0.1)

    def test_negative_edge(self):
        assert compute_edge_bps(0.45, 0.50) == pytest.approx(-500.0, abs=0.1)

    def test_none_input(self):
        assert compute_edge_bps(None, 0.5) is None
        assert compute_edge_bps(0.5, None) is None


class TestComputeGeneralFairValue:
    def test_no_signals_returns_market(self):
        """无信号 → 应接近市场价."""
        fair = compute_general_fair_value(0.6)
        assert fair == pytest.approx(0.6, abs=0.01)

    def test_bullish_obi_increases_fair(self):
        """OBI > 0（买压）→ fair 应高于市场价."""
        fair = compute_general_fair_value(0.5, obi_score=0.8)
        assert fair > 0.5

    def test_spot_fair_dominates(self):
        """当 spot_fair 远高于市场价时，输出应显著偏向 spot."""
        fair = compute_general_fair_value(0.5, spot_fair=0.80)
        assert fair > 0.55

    def test_boundary_prices(self):
        assert compute_general_fair_value(0.0) == 0.0
        assert compute_general_fair_value(1.0) == 1.0
