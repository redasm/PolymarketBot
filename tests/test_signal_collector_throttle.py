"""Regression: T2 collector source-side throttling.

Bug context: the orchestrator rate-cap was hitting ~342 skips per scan
cycle on a 2-market universe because the collector re-emitted the same
statistical signal every cycle on stable orderbooks. 2.8M signals/day
were ingested only to be discarded. The fix throttles at the source.
"""

from __future__ import annotations

from polymarket_arb.main_helpers.signal_collectors import (
    STATISTICAL_REEMIT_DEVIATION_DELTA,
    STATISTICAL_REEMIT_WINDOW_SEC,
    _statistical_should_emit,
    reset_statistical_signal_throttle,
)


class TestStatisticalThrottle:
    def setup_method(self):
        reset_statistical_signal_throttle()

    def test_first_emit_passes_through(self):
        assert _statistical_should_emit("m1", "buy_yes", deviation=0.02, now=100.0)

    def test_immediate_resubmit_blocked(self):
        _statistical_should_emit("m1", "buy_yes", deviation=0.02, now=100.0)
        assert not _statistical_should_emit("m1", "buy_yes", deviation=0.021, now=101.0)

    def test_direction_flip_always_passes(self):
        _statistical_should_emit("m1", "buy_yes", deviation=0.02, now=100.0)
        # Same market, opposite action — must re-emit immediately so the
        # orchestrator sees the directional flip.
        assert _statistical_should_emit("m1", "buy_no", deviation=-0.02, now=101.0)

    def test_large_deviation_change_re_emits(self):
        _statistical_should_emit("m1", "buy_yes", deviation=0.01, now=100.0)
        # Move bigger than the delta → fresh signal, even within window.
        new_dev = 0.01 + STATISTICAL_REEMIT_DEVIATION_DELTA + 0.001
        assert _statistical_should_emit("m1", "buy_yes", deviation=new_dev, now=110.0)

    def test_window_expiry_re_emits(self):
        _statistical_should_emit("m1", "buy_yes", deviation=0.02, now=100.0)
        future = 100.0 + STATISTICAL_REEMIT_WINDOW_SEC + 1.0
        assert _statistical_should_emit("m1", "buy_yes", deviation=0.0201, now=future)

    def test_separate_markets_dont_share_throttle(self):
        _statistical_should_emit("m1", "buy_yes", deviation=0.02, now=100.0)
        assert _statistical_should_emit("m2", "buy_yes", deviation=0.02, now=100.0)
