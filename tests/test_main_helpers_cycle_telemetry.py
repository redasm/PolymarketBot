"""Tests for `polymarket_arb.main_helpers.cycle_telemetry`.

Pinning the per-cycle payload schema so dashboard / NDJSON consumers
can rely on field names and the legacy-key compatibility (long form +
short form) staying stable across refactors.
"""

from __future__ import annotations

import time

import pytest

from polymarket_arb.main_helpers.cycle_telemetry import (
    build_cycle_summary_payload,
    emit_cycle_metrics,
)


def _payload_kwargs(**overrides):
    base = dict(
        run_id="run-1",
        cycle=42,
        markets_scanned=10,
        universe_market_count=200,
        selected_event_count=5,
        theoretical_opportunities_total=3,
        live_successes_total=2,
        simulated_successes_total=1,
        live_submissions_total=4,
        simulated_submissions_total=2,
        ws_status={"connected": True, "subscribed_tokens": 7},
        research_count=12,
        daily_pnl=1.23,
        open_positions=4,
        unrealized_pnl=-0.25,
        total_pnl=0.98,
        current_position_value=12.5,
        focus_keywords=["btc", "election"],
        book_stats={
            "requests": 100,
            "ws_hit": 80,
            "cache_hit": 5,
            "rest_fallback": 10,
            "rest_success": 8,
            "rest_error": 2,
            "missing_orderbook": 0,
            "cooldown_skip": 0,
        },
        timing_stats={"scan_sec": 0.123456, "execute_sec": -0.001},
    )
    base.update(overrides)
    return base


def test_build_cycle_summary_payload_canonical_shape():
    payload = build_cycle_summary_payload(**_payload_kwargs())
    assert payload["event"] == "cycle_summary"
    assert payload["cycle_status"] == "ok"
    assert payload["run_id"] == "run-1"
    assert payload["cycle"] == 42

    # Legacy alias compatibility — both short and long names must be present
    # because older dashboards still key off the short ones.
    assert payload["arbs_found_total"] == payload["theoretical_opportunities_total"] == 3
    assert payload["arbs_executed_total"] == payload["live_successes_total"] == 2

    assert payload["ws_connected"] is True
    assert payload["ws_tokens"] == 7
    assert payload["research_count"] == 12
    assert payload["daily_pnl"] == pytest.approx(1.23)
    assert payload["realized_daily_pnl"] == pytest.approx(1.23)
    assert payload["unrealized_pnl"] == pytest.approx(-0.25)
    assert payload["total_pnl"] == pytest.approx(0.98)
    assert payload["current_position_value"] == pytest.approx(12.5)
    assert payload["focus_keywords"] == ["btc", "election"]


def test_build_cycle_summary_payload_book_stats_normalised_to_int():
    raw = {
        "requests": "55",
        "ws_hit": 50,
        "cache_hit": 1.0,
        # Missing keys default to 0
    }
    payload = build_cycle_summary_payload(**_payload_kwargs(book_stats=raw))
    bs = payload["book_stats"]
    assert bs["requests"] == 55
    assert bs["ws_hit"] == 50
    assert bs["cache_hit"] == 1
    assert bs["rest_fallback"] == 0
    assert bs["missing_orderbook"] == 0
    # Schema must always include these keys even when caller omits them.
    assert set(bs.keys()) == {
        "requests",
        "ws_hit",
        "cache_hit",
        "rest_fallback",
        "rest_success",
        "rest_error",
        "missing_orderbook",
        "cooldown_skip",
    }


def test_build_cycle_summary_payload_timing_clamps_negative_and_rounds():
    payload = build_cycle_summary_payload(
        **_payload_kwargs(timing_stats={"scan_sec": 0.123456789, "noise_sec": -1.5})
    )
    assert payload["timing"]["scan_sec"] == pytest.approx(0.1235)
    assert payload["timing"]["noise_sec"] == 0.0


def test_build_cycle_summary_payload_ws_status_missing_keys_default_safely():
    payload = build_cycle_summary_payload(**_payload_kwargs(ws_status={}))
    assert payload["ws_connected"] is False
    assert payload["ws_tokens"] == 0


def test_build_cycle_summary_payload_propagates_cycle_status():
    payload = build_cycle_summary_payload(**_payload_kwargs(cycle_status="degraded"))
    assert payload["cycle_status"] == "degraded"


# --------- emit_cycle_metrics ----------


class _StubAnalyzer:
    def __init__(self, stats: dict[str, int]):
        self._stats = stats
        self.snapshot_calls: list[bool] = []

    def snapshot_stats(self, reset: bool = False) -> dict[str, int]:
        self.snapshot_calls.append(reset)
        return dict(self._stats)


class _StubRecorder:
    is_enabled = True

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def write_event(self, event_name: str, payload: dict) -> None:
        self.events.append((event_name, payload))


class _DisabledRecorder(_StubRecorder):
    is_enabled = False


def test_emit_cycle_metrics_writes_to_recorder_and_resets_book_stats():
    analyzer = _StubAnalyzer({"requests": 99})
    recorder = _StubRecorder()
    perf_start = time.perf_counter() - 0.05

    payload = emit_cycle_metrics(
        event_recorder=recorder,
        ob_analyzer=analyzer,
        cycle_perf_start=perf_start,
        cycle_timing={"scan_sec": 0.01},
        run_id="run-2",
        cycle=7,
        markets_scanned=20,
        universe_market_count=100,
        selected_event_count=3,
        theoretical_opportunities_total=1,
        live_successes_total=1,
        simulated_successes_total=0,
        live_submissions_total=2,
        simulated_submissions_total=0,
        ws_status={"connected": True, "subscribed_tokens": 5},
        research_count=4,
        daily_pnl=0.0,
        open_positions=1,
        focus_keywords=[],
    )
    assert analyzer.snapshot_calls == [True]  # reset must be True
    assert len(recorder.events) == 1
    name, written = recorder.events[0]
    assert name == "cycle_metrics"
    assert written is payload
    # `total_cycle_sec` is stamped here from perf_counter, must be >= 0
    assert payload["timing"]["total_cycle_sec"] >= 0.0
    assert payload["book_stats"]["requests"] == 99


def test_emit_cycle_metrics_skips_write_when_recorder_disabled():
    analyzer = _StubAnalyzer({})
    recorder = _DisabledRecorder()
    payload = emit_cycle_metrics(
        event_recorder=recorder,
        ob_analyzer=analyzer,
        cycle_perf_start=time.perf_counter(),
        cycle_timing={},
        run_id="r",
        cycle=1,
        markets_scanned=0,
        universe_market_count=0,
        selected_event_count=0,
        theoretical_opportunities_total=0,
        live_successes_total=0,
        simulated_successes_total=0,
        live_submissions_total=0,
        simulated_submissions_total=0,
        ws_status={},
        research_count=0,
        daily_pnl=0.0,
        open_positions=0,
        focus_keywords=[],
    )
    assert recorder.events == []
    assert payload["event"] == "cycle_summary"  # still returned
