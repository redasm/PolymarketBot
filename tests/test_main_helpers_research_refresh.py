"""Tests for `polymarket_arb.main_helpers.research_refresh`.

The async refresh state machine used to live inline in `main_loop.py`
and could only be exercised through full-loop integration tests. Pinning
its transitions here so future tweaks (cool-down values, signature
strategy, error swallowing) can't silently break the orchestrator.
"""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from polymarket_arb.main_helpers.research_refresh import (
    RESEARCH_RESUBMIT_COOLDOWN_SEC,
    ResearchRefreshState,
    advance_research_refresh,
    research_market_sample,
    research_market_signature,
)
from polymarket_arb.models import MarketInfo, ResearchSignalReport, TokenInfo


def _market(cid: str, *, question: str = "Will it happen?", event_id: str = "evt") -> MarketInfo:
    return MarketInfo(
        condition_id=cid,
        question=question,
        slug=cid,
        tokens=[TokenInfo(token_id=f"{cid}-yes", outcome="Yes")],
        event_id=event_id,
    )


def _report(window_sec: int = 3600) -> ResearchSignalReport:
    return ResearchSignalReport(
        generated_at=0.0,
        window_sec=window_sec,
        market_count=0,
        row_count=0,
        topic_count=0,
    )


def _completed_future(value):
    fut: Future = Future()
    fut.set_result(value)
    return fut


def _failed_future(exc: Exception):
    fut: Future = Future()
    fut.set_exception(exc)
    return fut


class _StubExecutor:
    """Fake `ThreadPoolExecutor` that returns a future from a queue."""

    def __init__(self, futures: list[Future]):
        self._futures = list(futures)
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        if self._futures:
            return self._futures.pop(0)
        # Default: never-completing future so we can observe "in flight" state.
        return Future()


# --------- research_market_sample ----------


def test_research_market_sample_prefers_universe_over_scanned():
    universe = [_market(f"u{i}") for i in range(5)]
    scanned = [_market(f"s{i}") for i in range(2)]
    out = research_market_sample(universe_markets=universe, scanned_markets=scanned, max_items=3)
    assert [m.condition_id for m in out] == ["u0", "u1", "u2"]


def test_research_market_sample_falls_back_to_scanned():
    scanned = [_market(f"s{i}") for i in range(3)]
    out = research_market_sample(universe_markets=[], scanned_markets=scanned, max_items=2)
    assert [m.condition_id for m in out] == ["s0", "s1"]


def test_research_market_sample_no_limit_when_max_zero():
    universe = [_market(f"u{i}") for i in range(4)]
    out = research_market_sample(universe_markets=universe, scanned_markets=[], max_items=0)
    assert len(out) == 4


# --------- research_market_signature ----------


def test_research_market_signature_is_order_independent():
    a = _market("a", question="Q1")
    b = _market("b", question="Q2")
    sig_ab = research_market_signature([a, b])
    sig_ba = research_market_signature([b, a])
    assert sig_ab == sig_ba


def test_research_market_signature_changes_with_question():
    a = _market("a", question="Q1")
    a_renamed = _market("a", question="Q2")
    assert research_market_signature([a]) != research_market_signature([a_renamed])


# --------- advance_research_refresh ----------


def _stub_service():
    """Service stub: only `collect_report` is touched (passed to executor.submit)."""
    return type("Svc", (), {"collect_report": staticmethod(lambda markets, window_sec: None)})()


def test_advance_initial_refresh_submits_when_no_report_yet():
    state = ResearchRefreshState()
    executor = _StubExecutor(futures=[])
    out = advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=[_market("a")],
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=1000.0,
    )
    assert out is None  # no report yet
    assert state.pending_future is not None
    assert state.pending_signature == research_market_signature([_market("a")])
    assert state.last_submit_ts == 1000.0
    assert len(executor.calls) == 1


def test_advance_does_not_resubmit_while_pending():
    state = ResearchRefreshState(pending_future=Future(), last_submit_ts=999.0)
    executor = _StubExecutor(futures=[])
    advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=[_market("a")],
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=1000.0,
    )
    # The pending future is still there; no new submit recorded.
    assert state.last_submit_ts == 999.0
    assert executor.calls == []


def test_advance_initial_refresh_signature_change_overrides_cooldown():
    """A first-ever submit fires immediately because the empty signature
    differs from the new sample's signature — the cool-down only bites
    when nothing about the candidate set has actually changed.

    Regression-pin: an earlier draft assumed cool-down throttled every
    initial retry; the actual contract is that signature change wins.
    """
    state = ResearchRefreshState(last_submit_ts=500.0)  # recent, but empty signature
    executor = _StubExecutor(futures=[])
    advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=[_market("a")],
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=500.0 + 0.1,  # well within cool-down
    )
    assert len(executor.calls) == 1  # signature changed → submitted despite cool-down


def test_advance_harvests_completed_future_and_publishes_report():
    expected = _report()
    sample = [_market("a")]
    sig = research_market_signature(sample)
    state = ResearchRefreshState(
        pending_future=_completed_future(expected),
        pending_signature=sig,
        # Recent submit so periodic refresh isn't immediately due after harvest.
        last_submit_ts=1990.0,
    )
    executor = _StubExecutor(futures=[])
    out = advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=sample,
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=2000.0,  # only 10s after last submit → below 60s interval
    )
    assert out is expected
    assert state.last_report is expected
    assert state.last_report_signature == sig
    # Harvest cleared the pending future and no resubmit fired (interval not due).
    assert state.pending_future is None
    assert state.pending_signature == ()
    assert executor.calls == []


def test_advance_swallows_failed_future_but_clears_state(caplog):
    state = ResearchRefreshState(pending_future=_failed_future(RuntimeError("boom")))
    executor = _StubExecutor(futures=[])
    with caplog.at_level("ERROR", logger="main_loop"):
        out = advance_research_refresh(
            research_signal_service=_stub_service(),
            research_executor=executor,
            state=state,
            universe_markets=[_market("a")],
            scanned_markets=[],
            max_items=10,
            window_sec=3600,
            refresh_interval_sec=60.0,
            now_ts=2000.0,
        )
    assert out is None  # no successful report
    assert state.last_report is None
    assert state.pending_future is not None  # immediately resubmitted
    assert any("Research signal 刷新失败" in r.getMessage() for r in caplog.records)


def test_advance_resubmits_when_signature_changes():
    sample_a = [_market("a")]
    sig_a = research_market_signature(sample_a)
    state = ResearchRefreshState(
        last_report=_report(),
        last_report_signature=sig_a,
        last_submit_ts=1000.0,
    )
    executor = _StubExecutor(futures=[])
    advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=[_market("b")],  # signature changed
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=1001.0,  # well within refresh interval
    )
    assert len(executor.calls) == 1


def test_advance_resubmits_periodically_when_interval_elapses():
    sample_a = [_market("a")]
    sig_a = research_market_signature(sample_a)
    state = ResearchRefreshState(
        last_report=_report(),
        last_report_signature=sig_a,
        last_submit_ts=1000.0,
    )
    executor = _StubExecutor(futures=[])
    # within interval: no submit
    advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=sample_a,
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=1059.0,
    )
    assert executor.calls == []
    # past interval: submit
    advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=sample_a,
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=1061.0,
    )
    assert len(executor.calls) == 1


def test_advance_returns_none_when_signature_does_not_match_published_report():
    """Mid-transition: report is for sample A, current sample is B → suppress overlay."""
    sample_a = [_market("a")]
    state = ResearchRefreshState(
        last_report=_report(),
        last_report_signature=research_market_signature(sample_a),
    )
    executor = _StubExecutor(futures=[])
    out = advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=[_market("b")],
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
        now_ts=2000.0,
    )
    assert out is None  # don't apply stale overlay during transition


def test_advance_skips_submit_when_no_service_or_executor():
    state = ResearchRefreshState()
    out = advance_research_refresh(
        research_signal_service=None,
        research_executor=None,
        state=state,
        universe_markets=[_market("a")],
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
    )
    assert out is None
    assert state.pending_future is None


def test_advance_skips_submit_when_sample_empty():
    state = ResearchRefreshState()
    executor = _StubExecutor(futures=[])
    advance_research_refresh(
        research_signal_service=_stub_service(),
        research_executor=executor,
        state=state,
        universe_markets=[],
        scanned_markets=[],
        max_items=10,
        window_sec=3600,
        refresh_interval_sec=60.0,
    )
    assert executor.calls == []
