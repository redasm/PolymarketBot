"""utils_time helper tests."""

from __future__ import annotations

import pytest

from polymarket_arb.utils_time import floor_ts


def test_floor_ts_rejects_non_positive_interval():
    with pytest.raises(ValueError, match="interval_ms must be positive"):
        floor_ts(1_234, 0)


def test_floor_ts_rounds_down_to_interval_boundary():
    assert floor_ts(12_345, 1_000) == 12_000
