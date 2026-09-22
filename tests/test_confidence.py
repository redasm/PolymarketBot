"""Shared confidence helper tests."""

import pytest

from polymarket_arb.confidence import clamp_confidence, confidence_from_edge_pct, confidence_from_signal_strength


def test_clamp_confidence_bounds_values():
    assert clamp_confidence(-1) == 0.0
    assert clamp_confidence(0.4) == 0.4
    assert clamp_confidence(5) == 1.0


def test_confidence_from_edge_pct_uses_common_scale():
    assert confidence_from_edge_pct(0.0, full_confidence_pct=10.0) == 0.0
    assert confidence_from_edge_pct(5.0, full_confidence_pct=10.0) == pytest.approx(0.4621, rel=1e-3)
    assert confidence_from_edge_pct(10.0, full_confidence_pct=10.0) == pytest.approx(0.7616, rel=1e-3)
    assert 0.9 < confidence_from_edge_pct(20.0, full_confidence_pct=10.0) < 1.0


def test_confidence_from_signal_strength_scales_and_clamps():
    assert confidence_from_signal_strength(0.2, scale=2.0) == 0.4
    assert confidence_from_signal_strength(1.0, scale=2.0) == 1.0
