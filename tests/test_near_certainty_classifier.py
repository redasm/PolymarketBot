"""Unit tests for NearCertaintyClassifier.

The classifier itself is dead-simple — these tests pin the contract so
future config changes (different thresholds, asymmetric tails) don't
silently break the orchestrator wiring.
"""

from __future__ import annotations

import pytest

from polymarket_arb.strategies.signal_policies import NearCertaintyClassifier


def test_classifier_rejects_invalid_thresholds():
    with pytest.raises(ValueError):
        NearCertaintyClassifier(high_threshold=0.4)
    with pytest.raises(ValueError):
        NearCertaintyClassifier(low_threshold=0.6)
    with pytest.raises(ValueError):
        NearCertaintyClassifier(size_multiplier=1.5)


def test_high_zone_fires_at_threshold_inclusive():
    clf = NearCertaintyClassifier(high_threshold=0.92, shadow_mode=False)
    r = clf.classify(0.92)
    assert r.applied
    assert r.risk_zone == "high_certainty"
    assert r.size_multiplier == 0.60
    assert "near_certainty_high_zone" in r.reasons


def test_longshot_zone_fires_at_threshold_inclusive():
    clf = NearCertaintyClassifier(low_threshold=0.08, shadow_mode=False)
    r = clf.classify(0.08)
    assert r.applied
    assert r.risk_zone == "longshot"


def test_normal_zone_no_fire():
    clf = NearCertaintyClassifier()
    r = clf.classify(0.55)
    assert not r.applied
    assert r.risk_zone == "normal"
    assert r.size_multiplier == 1.0
    assert r.confidence_delta == 0.0


def test_missing_price_returns_unknown_no_apply():
    clf = NearCertaintyClassifier()
    r = clf.classify(None)
    assert not r.applied
    assert r.risk_zone == "unknown"


def test_shadow_mode_flag_propagates_to_result():
    shadow = NearCertaintyClassifier(shadow_mode=True)
    live = NearCertaintyClassifier(shadow_mode=False)

    assert shadow.classify(0.95).shadow_mode is True
    assert live.classify(0.95).shadow_mode is False
