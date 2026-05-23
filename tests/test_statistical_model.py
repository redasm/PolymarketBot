from __future__ import annotations

import pytest

from polymarket_arb.strategies.statistical_model import BayesianPriceModel, StatisticalMispricingDetector


def test_bayesian_model_damps_microstructure_signals_near_extreme_prices() -> None:
    model = BayesianPriceModel()

    center = model.estimate(0.50, obi_score=1.0, momentum_score=1.0)
    extreme = model.estimate(0.97, obi_score=1.0, momentum_score=1.0)

    assert center - 0.50 > extreme - 0.97
    assert model._microstructure_multiplier(0.97) < 0.50
    assert model._microstructure_multiplier(0.50) == 1.0


def test_statistical_detector_defaults_to_five_percent_min_deviation() -> None:
    detector = StatisticalMispricingDetector()

    assert detector._min_deviation == pytest.approx(0.05)


def test_statistical_detector_confidence_uses_damped_microstructure_strength() -> None:
    detector = StatisticalMispricingDetector(min_deviation=0.0, min_confidence=0.0)

    estimate = detector.estimate_market_probability(
        market_id="extreme",
        outcome="Yes",
        market_price=0.97,
        bids_total_size=100.0,
        asks_total_size=0.0,
    )

    assert estimate.signals["obi"] == pytest.approx(1.0)
    assert estimate.signals["microstructure_multiplier"] < 0.50
    assert estimate.confidence < 0.50
