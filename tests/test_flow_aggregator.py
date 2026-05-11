"""Tests for ``polymarket_arb.main_helpers.flow_aggregator``.

Covers the minimum-viable Becker 2025 follow-up data path:

- Per-market sliding window accumulation
- ``derive_taker_bought_yes`` mapping for binary tokens
- Eviction of stale trades past ``window_sec``
- Persistence round-trip (save → reload)
- ``FlowIngest`` token → market routing
- ``FlowBias`` stability / strength flags
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymarket_arb.main_helpers.flow_aggregator import (
    FlowAggregator,
    FlowIngest,
    derive_taker_bought_yes,
)


def test_derive_taker_bought_yes_handles_all_four_combinations() -> None:
    assert derive_taker_bought_yes(is_yes_token=True, taker_side="BUY") is True
    assert derive_taker_bought_yes(is_yes_token=True, taker_side="SELL") is False
    assert derive_taker_bought_yes(is_yes_token=False, taker_side="BUY") is False
    assert derive_taker_bought_yes(is_yes_token=False, taker_side="SELL") is True


def test_derive_taker_bought_yes_rejects_unknown_side() -> None:
    assert derive_taker_bought_yes(is_yes_token=True, taker_side="") is False
    assert derive_taker_bought_yes(is_yes_token=True, taker_side="HEDGE") is False


def test_record_trade_skips_invalid_inputs() -> None:
    agg = FlowAggregator(window_sec=60.0, min_trades=1)
    agg.record_trade(condition_id="", taker_bought_yes=True, shares=10.0, ts=100.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=0.0, ts=100.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=-1.0, ts=100.0)
    assert agg.get_bias("m1") is None


def test_get_bias_returns_share_weighted_taker_yes() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=2)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=70.0, ts=100.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=False, shares=30.0, ts=200.0)

    bias = agg.get_bias("m1", now=200.0)
    assert bias is not None
    assert bias.condition_id == "m1"
    assert bias.taker_yes_shares == pytest.approx(70.0)
    assert bias.taker_no_shares == pytest.approx(30.0)
    assert bias.total_shares == pytest.approx(100.0)
    assert bias.taker_yes_share == pytest.approx(0.70)
    assert bias.is_stable is True


def test_get_bias_marks_below_threshold_as_not_strong() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=2, strong_threshold=0.55)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=51.0, ts=100.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=False, shares=49.0, ts=110.0)

    bias = agg.get_bias("m1", now=110.0)
    assert bias is not None
    assert bias.is_stable is True
    assert bias.is_strong is False
    assert bias.lean == "neutral"


def test_get_bias_marks_above_threshold_as_strong() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=2, strong_threshold=0.55)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=70.0, ts=100.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=False, shares=30.0, ts=110.0)

    bias = agg.get_bias("m1", now=110.0)
    assert bias is not None
    assert bias.is_strong is True
    assert bias.lean == "yes"


def test_get_bias_marks_below_complement_threshold_as_strong_no() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=2, strong_threshold=0.55)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=20.0, ts=100.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=False, shares=80.0, ts=110.0)

    bias = agg.get_bias("m1", now=110.0)
    assert bias is not None
    assert bias.is_strong is True
    assert bias.lean == "no"


def test_get_bias_below_min_trades_is_not_stable() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=5, strong_threshold=0.55)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=100.0, ts=100.0)

    bias = agg.get_bias("m1", now=100.0)
    assert bias is not None
    assert bias.is_stable is False
    assert bias.is_strong is False
    assert bias.lean == "neutral"


def test_get_bias_evicts_trades_older_than_window() -> None:
    agg = FlowAggregator(window_sec=60.0, min_trades=1)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=100.0, ts=0.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=False, shares=10.0, ts=100.0)

    bias = agg.get_bias("m1", now=120.0)
    assert bias is not None
    # The taker_yes=100 at ts=0.0 is older than 120 - 60 = 60s and gets dropped.
    assert bias.taker_yes_shares == pytest.approx(0.0)
    assert bias.taker_no_shares == pytest.approx(10.0)


def test_get_bias_returns_none_when_window_fully_evicted() -> None:
    agg = FlowAggregator(window_sec=60.0, min_trades=1)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=100.0, ts=0.0)

    assert agg.get_bias("m1", now=200.0) is None


def test_persistence_round_trip(tmp_path) -> None:
    """Round-trip a state file with explicit `now` to bypass real-clock pruning."""
    state = tmp_path / "flow_state.json"

    agg = FlowAggregator(
        window_sec=3600.0,
        min_trades=1,
        state_file=str(state),
        persist_every_n=1,
    )
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=40.0, ts=1000.0)
    agg.record_trade(condition_id="m1", taker_bought_yes=False, shares=10.0, ts=1100.0)
    agg.close()

    assert state.exists()

    reloaded = FlowAggregator(
        window_sec=3600.0,
        min_trades=1,
        state_file=str(state),
    )
    bias = reloaded.get_bias("m1", now=1200.0)
    assert bias is not None
    assert bias.trade_count == 2
    assert bias.taker_yes_shares == pytest.approx(40.0)
    assert bias.taker_no_shares == pytest.approx(10.0)


def test_persistence_round_trip_with_recent_clock(tmp_path) -> None:
    """A recently-written state file should be visible at wall-clock now."""
    import time as _time

    state = tmp_path / "flow_state.json"
    now = _time.time()

    agg = FlowAggregator(
        window_sec=3600.0,
        min_trades=1,
        state_file=str(state),
        persist_every_n=1,
    )
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=40.0, ts=now - 10)
    agg.record_trade(condition_id="m1", taker_bought_yes=False, shares=10.0, ts=now - 5)
    agg.close()

    reloaded = FlowAggregator(
        window_sec=3600.0,
        min_trades=1,
        state_file=str(state),
    )
    bias = reloaded.get_bias("m1")
    assert bias is not None
    assert bias.trade_count == 2


def test_persistence_drops_rows_outside_window(tmp_path) -> None:
    state = tmp_path / "flow_state.json"

    agg = FlowAggregator(
        window_sec=600.0,
        min_trades=1,
        state_file=str(state),
        persist_every_n=1,
    )
    # First trade is far in the past, won't fit in the loader's window
    # once "now" advances. The test exercises the cutoff in `_load_state`
    # by writing a stale row directly to disk and asserting it is
    # pruned on the next load.
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=20.0, ts=1.0)
    agg.close()

    # Patch the persisted state's saved_at so the freshly-loaded
    # aggregator considers all rows stale. We do this by mutating the
    # JSON directly to keep the test focused on the cutoff logic.
    import json

    payload = json.loads(state.read_text("utf-8"))
    # Push the only row to ts=1.0 — already there — but bump `window_sec`
    # to a value that excludes it once we add a follow-up "now".
    state.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = FlowAggregator(
        window_sec=10.0,  # 10s window — ts=1.0 is much older than time.time() - 10
        min_trades=1,
        state_file=str(state),
    )
    assert reloaded.get_bias("m1") is None


def test_persistence_tolerates_malformed_state(tmp_path) -> None:
    state = tmp_path / "flow_state.json"
    state.write_text("not valid json", encoding="utf-8")

    # Must not raise even though the file is corrupt.
    agg = FlowAggregator(window_sec=3600.0, min_trades=1, state_file=str(state))
    assert agg.snapshot() == {}


def test_strong_threshold_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        FlowAggregator(window_sec=60.0, strong_threshold=0.3)
    with pytest.raises(ValueError):
        FlowAggregator(window_sec=60.0, strong_threshold=1.5)


def test_snapshot_returns_all_active_markets() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    agg.record_trade(condition_id="m1", taker_bought_yes=True, shares=5.0, ts=100.0)
    agg.record_trade(condition_id="m2", taker_bought_yes=False, shares=8.0, ts=200.0)

    # Provide explicit `now` so the test's synthetic timestamps survive
    # the sliding-window cutoff.
    snapshot = agg.snapshot(now=200.0)
    assert set(snapshot.keys()) == {"m1", "m2"}


# ---------- FlowIngest -------------------------------------------------------


def _market(condition_id: str, *, yes_token: str, no_token: str) -> object:
    return SimpleNamespace(
        condition_id=condition_id,
        tokens=[
            SimpleNamespace(token_id=yes_token, outcome="Yes"),
            SimpleNamespace(token_id=no_token, outcome="No"),
        ],
    )


def test_flow_ingest_routes_buy_yes_to_taker_yes() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    ingest = FlowIngest(agg)
    ingest.register_markets([_market("m1", yes_token="yes-tok", no_token="no-tok")])

    ingest.on_trade(
        {
            "asset_id": "yes-tok",
            "side": "BUY",
            "size": "50",
            "timestamp": "1000",
        }
    )

    bias = agg.get_bias("m1", now=1000.0)
    assert bias is not None
    assert bias.taker_yes_shares == pytest.approx(50.0)
    assert bias.taker_no_shares == pytest.approx(0.0)


def test_flow_ingest_routes_sell_yes_to_taker_no() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    ingest = FlowIngest(agg)
    ingest.register_markets([_market("m1", yes_token="yes-tok", no_token="no-tok")])

    ingest.on_trade(
        {
            "asset_id": "yes-tok",
            "side": "SELL",
            "size": "25",
            "timestamp": "1000",
        }
    )

    bias = agg.get_bias("m1", now=1000.0)
    assert bias is not None
    assert bias.taker_yes_shares == pytest.approx(0.0)
    assert bias.taker_no_shares == pytest.approx(25.0)


def test_flow_ingest_routes_sell_no_to_taker_yes() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    ingest = FlowIngest(agg)
    ingest.register_markets([_market("m1", yes_token="yes-tok", no_token="no-tok")])

    ingest.on_trade(
        {
            "asset_id": "no-tok",
            "side": "SELL",
            "size": "40",
            "timestamp": "1000",
        }
    )

    bias = agg.get_bias("m1", now=1000.0)
    assert bias is not None
    assert bias.taker_yes_shares == pytest.approx(40.0)
    assert bias.taker_no_shares == pytest.approx(0.0)


def test_flow_ingest_ignores_unknown_tokens() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    ingest = FlowIngest(agg)
    ingest.register_markets([_market("m1", yes_token="yes-tok", no_token="no-tok")])

    ingest.on_trade({"asset_id": "other-tok", "side": "BUY", "size": "100"})

    assert agg.get_bias("m1") is None


def test_flow_ingest_handles_millisecond_timestamps() -> None:
    """Polymarket WS timestamps are in ms; the ingest should normalize."""
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    ingest = FlowIngest(agg)
    ingest.register_markets([_market("m1", yes_token="yes-tok", no_token="no-tok")])

    ingest.on_trade(
        {
            "asset_id": "yes-tok",
            "side": "BUY",
            "size": "10",
            "timestamp": "1740000000000",
        }
    )

    bias = agg.get_bias("m1", now=1_740_000_001.0)
    assert bias is not None
    assert bias.trade_count == 1


def test_flow_ingest_drops_zero_or_invalid_sizes() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    ingest = FlowIngest(agg)
    ingest.register_markets([_market("m1", yes_token="yes-tok", no_token="no-tok")])

    ingest.on_trade({"asset_id": "yes-tok", "side": "BUY", "size": "0"})
    ingest.on_trade({"asset_id": "yes-tok", "side": "BUY", "size": "abc"})

    assert agg.get_bias("m1") is None


def test_flow_ingest_register_markets_rebuilds_lookup() -> None:
    agg = FlowAggregator(window_sec=3600.0, min_trades=1)
    ingest = FlowIngest(agg)
    ingest.register_markets([_market("m1", yes_token="y1", no_token="n1")])

    # Now switch to a different market; the old token mapping must
    # no longer route trades.
    ingest.register_markets([_market("m2", yes_token="y2", no_token="n2")])

    ingest.on_trade({"asset_id": "y1", "side": "BUY", "size": "5"})
    ingest.on_trade({"asset_id": "y2", "side": "BUY", "size": "7"})

    assert agg.get_bias("m1") is None
    bias_m2 = agg.get_bias("m2")
    assert bias_m2 is not None
    assert bias_m2.taker_yes_shares == pytest.approx(7.0)
