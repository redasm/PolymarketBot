"""VolEstimator 单元测试：warmup 行为、sigma 合理性、多尺度一致性."""

import math

import pytest

from polymarket_arb.volatility_estimator import VolEstimator


def _generate_brownian_closes(n: int, start: float = 100_000.0, sigma_1m: float = 0.001, seed: int = 42) -> list[float]:
    """生成模拟的 GBM 1 分钟收盘价序列（确定性 seed）."""
    import random
    rng = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        z = rng.gauss(0, 1)
        closes.append(closes[-1] * math.exp(sigma_1m * z))
    return closes


class TestVolEstimatorWarmup:
    def test_not_ready_before_min_bars(self):
        vol = VolEstimator(fast_minutes=60, slow_minutes=360, min_bars=20)
        for i in range(15):
            vol.update_1m_close(100_000 + i, ts_ms=i * 60_000)
        assert not vol.is_ready()
        snap = vol.snapshot()
        assert snap["ready"] is False
        assert snap["sigma_fast_15m"] is None

    def test_ready_after_min_bars(self):
        vol = VolEstimator(fast_minutes=10, slow_minutes=30, min_bars=5)
        closes = _generate_brownian_closes(25, sigma_1m=0.001)
        for i, px in enumerate(closes):
            vol.update_1m_close(px, ts_ms=i * 60_000)
        assert vol.is_ready()
        snap = vol.snapshot()
        assert snap["ready"] is True
        assert snap["sigma_fast_15m"] is not None
        assert snap["sigma_fast_15m"] > 0


class TestVolEstimatorSigma:
    def test_constant_price_zero_sigma(self):
        """恒定价格 → sigma 应为 0 或极小."""
        vol = VolEstimator(fast_minutes=10, slow_minutes=20, min_bars=5)
        for i in range(30):
            vol.update_1m_close(50_000.0, ts_ms=i * 60_000)
        snap = vol.snapshot()
        assert snap["ready"] is True
        assert snap["sigma_fast_15m"] == pytest.approx(0.0, abs=1e-8)

    def test_volatile_price_positive_sigma(self):
        """波动价格 → sigma 应为正."""
        closes = _generate_brownian_closes(100, sigma_1m=0.002)
        vol = VolEstimator(fast_minutes=60, slow_minutes=360, min_bars=20)
        for i, px in enumerate(closes):
            vol.update_1m_close(px, ts_ms=i * 60_000)
        snap = vol.snapshot()
        assert snap["ready"] is True
        assert snap["sigma_fast_15m"] > 0

    def test_sigma_15m_is_sqrt15_of_1m(self):
        """sigma_15m 应 ≈ sigma_1m × √15."""
        closes = _generate_brownian_closes(200, sigma_1m=0.001)
        vol = VolEstimator(fast_minutes=60, slow_minutes=360, min_bars=20)
        for i, px in enumerate(closes):
            vol.update_1m_close(px, ts_ms=i * 60_000)

        snap = vol.snapshot()
        sigma_15m = snap["sigma_fast_15m"]
        assert sigma_15m is not None

        log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        actual_1m = (sum((r - sum(log_rets) / len(log_rets)) ** 2 for r in log_rets[-60:]) / 59) ** 0.5
        expected_15m = actual_1m * math.sqrt(15)
        assert sigma_15m == pytest.approx(expected_15m, rel=0.05)


class TestVolEstimatorWarmupFromCloses:
    def test_warmup_from_list(self):
        vol = VolEstimator(fast_minutes=60, slow_minutes=360, min_bars=20)
        closes = _generate_brownian_closes(100)
        count = vol.warmup_from_closes(closes, ts_ms=100 * 60_000)
        assert count == 99
        assert vol.is_ready()
        snap = vol.snapshot()
        assert snap["sigma_fast_15m"] is not None


class TestVolEstimatorBlend:
    def test_blend_weight_neutral(self):
        """rvol_5s=None → w=0.5 → blend 是 fast 和 slow 的均值."""
        closes = _generate_brownian_closes(400, sigma_1m=0.001)
        vol = VolEstimator(fast_minutes=60, slow_minutes=360, min_bars=20)
        for i, px in enumerate(closes):
            vol.update_1m_close(px, ts_ms=i * 60_000)

        snap = vol.snapshot(rvol_5s=None)
        fast = snap["sigma_fast_15m"]
        slow = snap["sigma_slow_15m"]
        blend = snap["sigma_blend_15m"]
        if fast is not None and slow is not None and blend is not None:
            expected_blend = 0.5 * fast + 0.5 * slow
            assert blend == pytest.approx(expected_blend, rel=0.01)

    def test_high_rvol_biases_toward_fast(self):
        """高 rvol → blend 更接近 fast."""
        closes = _generate_brownian_closes(400, sigma_1m=0.001)
        vol = VolEstimator(fast_minutes=60, slow_minutes=360, min_bars=20)
        for i, px in enumerate(closes):
            vol.update_1m_close(px, ts_ms=i * 60_000)

        snap = vol.snapshot(rvol_5s=3.0)
        fast = snap["sigma_fast_15m"]
        blend = snap["sigma_blend_15m"]
        if fast is not None and blend is not None:
            assert abs(blend - fast) < abs(blend - snap["sigma_slow_15m"])
