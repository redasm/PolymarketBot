"""Tests for `polymarket_arb.main_helpers.ai_cycle.run_ai_cycle`.

Locks in the per-call contract:

- HOLD decisions are skipped.
- BUY/SELL decisions are turned into `StrategySignal`s and submitted.
- Submitted decisions are mirrored to dashboard + event recorder
  with the research overlay attached.
- `evaluate_markets` timeouts/exceptions surface a single dashboard
  error + event recorder entry and short-circuit (no signals submitted,
  no risk param adjustment, no status update).
- `adjust_risk_params` only runs when `config.ai_override_risk` is set,
  and its failures are also recorded but never crash the cycle.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from polymarket_arb.main_helpers import ai_cycle
from polymarket_arb.main_helpers.ai_cycle import run_ai_cycle


# ---------- shared stubs ------------------------------------------------------


class _FakeDashboardState:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.ai_decisions: list[dict] = []
        self.last_update: dict | None = None

    def append_error(self, payload: dict) -> None:
        self.errors.append(payload)

    def append_ai_decision(self, payload: dict) -> None:
        self.ai_decisions.append(payload)

    def update(self, **kwargs: Any) -> None:
        self.last_update = kwargs


class _FakeEventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write_event(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


class _FakeOrchestrator:
    def __init__(self, accept: bool = True, pending_overlay: dict | None = None) -> None:
        self.accept = accept
        self.submitted: list[Any] = []
        self.pending_overlay = pending_overlay or {}
        self._pending_signals: list[Any] = []
        self.status: dict = {"foo": 1}

    def submit_signal(self, signal, **_kwargs):
        self.submitted.append(signal)
        if self.accept:
            # Stash the signal so `find_pending_signal_overlay` can
            # locate it via the (market_id, signal_type, timestamp)
            # tuple. Inject the desired overlay into payload.
            signal.payload.setdefault("research_overlay", dict(self.pending_overlay))
            self._pending_signals.append(signal)
        return self.accept

    def get_status(self) -> dict:
        return self.status


class _FakeAIAdvisor:
    def __init__(
        self,
        *,
        decisions: list | None = None,
        eval_exc: Exception | None = None,
        adj_exc: Exception | None = None,
        adjustments: dict | None = None,
    ) -> None:
        self._decisions = decisions or []
        self._eval_exc = eval_exc
        self._adj_exc = adj_exc
        self._adjustments = adjustments

    async def evaluate_markets(self, _ctx):
        if self._eval_exc is not None:
            raise self._eval_exc
        return self._decisions

    async def adjust_risk_params(self, _ctx):
        if self._adj_exc is not None:
            raise self._adj_exc
        return self._adjustments

    def get_status(self) -> dict:
        return {"ai": "ok"}


class _FakeRiskMgr:
    def __init__(self) -> None:
        self.state: dict = {"exposure": 0.0}
        self.adjustments_applied: list[dict] = []

    def apply_ai_adjustment(self, adjustment: dict) -> None:
        self.adjustments_applied.append(adjustment)


def _decision(action: str, market_id: str = "mkt-1", **kwargs):
    """Build a fake decision matching the AIAdvisor return shape."""
    base = dict(
        action=action,
        market_id=market_id,
        reasoning="because reasons",
        confidence=0.7,
        recommended_size_pct=0.05,
        urgency=0.5,
    )
    base.update(kwargs)
    base["to_dict"] = lambda b=dict(base): {
        k: v for k, v in b.items() if k != "to_dict"
    }
    return SimpleNamespace(**base)


def _config(**overrides):
    base = dict(
        max_total_exposure=1000.0,
        ai_override_risk=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx_builder():
    """`MarketContextBuilder` stub: returns an opaque context object."""
    return SimpleNamespace(build=lambda **_: {"context": "ok"})


@pytest.fixture(autouse=True)
def _stub_helpers(monkeypatch):
    """Make `summarize_market_catalog` / `serialize_recent_trade` /
    `lookup_market_snapshot` returns deterministic so we can assert on
    the dashboard / event recorder payloads without hand-building
    market metadata.
    """
    monkeypatch.setattr(
        ai_cycle, "summarize_market_catalog",
        lambda markets: {"mkt-1": {"question": "Will X?", "yes_price": 0.42}},
    )
    monkeypatch.setattr(ai_cycle, "serialize_recent_trade", lambda t: {"trade": str(t)})
    monkeypatch.setattr(
        ai_cycle, "lookup_market_snapshot",
        lambda mid, catalog: catalog.get(mid, {"question": "", "yes_price": None}),
    )


# ---------- baseline / happy path --------------------------------------------


def _run(*, decisions=None, ai_kwargs=None, config=None, recorder=True, **overrides):
    advisor = _FakeAIAdvisor(decisions=decisions or [], **(ai_kwargs or {}))
    orchestrator = overrides.pop("orchestrator", _FakeOrchestrator())
    dash = _FakeDashboardState()
    risk = _FakeRiskMgr()
    rec = _FakeEventRecorder() if recorder else None

    run_ai_cycle(
        ai_advisor=advisor,
        ctx_builder=_ctx_builder(),
        active_markets=[],
        recent_trades=[],
        book_store=SimpleNamespace(),
        vol_estimator=SimpleNamespace(),
        edge_decision=None,
        risk_mgr=risk,
        orchestrator=orchestrator,
        dash_state=dash,
        config=config or _config(),
        event_recorder=rec,
        **overrides,
    )
    return SimpleNamespace(
        advisor=advisor,
        orchestrator=orchestrator,
        dash=dash,
        risk=risk,
        recorder=rec,
    )


def test_no_decisions_only_updates_status() -> None:
    out = _run(decisions=[])
    assert out.dash.errors == []
    assert out.dash.ai_decisions == []
    assert out.orchestrator.submitted == []
    assert out.dash.last_update == {
        "ai_status": {"ai": "ok"},
        "strategy_status": {"foo": 1},
    }


def test_hold_decisions_are_skipped() -> None:
    out = _run(decisions=[_decision("HOLD")])
    assert out.orchestrator.submitted == []
    assert out.dash.ai_decisions == []


def test_buy_decision_submitted_and_mirrored() -> None:
    out = _run(decisions=[_decision("BUY")])
    assert len(out.orchestrator.submitted) == 1
    sig = out.orchestrator.submitted[0]
    assert sig.market_id == "mkt-1"
    assert sig.signal_type == "ai_buy"
    assert sig.payload["action"] == "BUY"
    assert sig.recommended_size_usdc == pytest.approx(0.05 * 1000.0)

    assert len(out.dash.ai_decisions) == 1
    decision_log = out.dash.ai_decisions[0]
    assert decision_log["submitted"] is True
    assert decision_log["market_question"] == "Will X?"
    assert decision_log["decision_price"] == 0.42

    kinds = [k for k, _ in out.recorder.events]
    assert "ai_decisions" in kinds


def test_research_overlay_attached_to_decision_log() -> None:
    orchestrator = _FakeOrchestrator(pending_overlay={"applied": True, "boost": 1.3})
    out = _run(decisions=[_decision("BUY")], orchestrator=orchestrator)
    overlay = out.dash.ai_decisions[0]["research_overlay"]
    assert overlay == {"applied": True, "boost": 1.3}


# ---------- failure surfaces -------------------------------------------------


def test_evaluate_timeout_records_error_and_short_circuits() -> None:
    out = _run(ai_kwargs={"eval_exc": asyncio.TimeoutError()})

    assert len(out.dash.errors) == 1
    assert "evaluation_timeout" in out.dash.errors[0]["message"]
    assert out.orchestrator.submitted == []
    assert out.dash.ai_decisions == []
    # status NOT updated when bailing early
    assert out.dash.last_update is None
    kinds = [k for k, _ in out.recorder.events]
    assert "risk_events" in kinds
    payload = next(p for k, p in out.recorder.events if k == "risk_events")
    assert payload["event"] == "ai_evaluation_timeout"


def test_evaluate_exception_records_error_and_short_circuits() -> None:
    out = _run(ai_kwargs={"eval_exc": RuntimeError("boom")})
    assert len(out.dash.errors) == 1
    assert "AI error: boom" in out.dash.errors[0]["message"]
    assert out.orchestrator.submitted == []
    assert out.dash.last_update is None
    kinds = {k for k, _ in out.recorder.events}
    assert kinds == {"risk_events"}


def test_evaluate_failure_without_event_recorder_does_not_crash() -> None:
    out = _run(ai_kwargs={"eval_exc": RuntimeError("boom")}, recorder=False)
    assert len(out.dash.errors) == 1


# ---------- risk override ----------------------------------------------------


def test_risk_override_skipped_when_disabled() -> None:
    out = _run(
        decisions=[_decision("BUY")],
        ai_kwargs={"adjustments": {"max_position": 5}},
        config=_config(ai_override_risk=False),
    )
    assert out.risk.adjustments_applied == []


def test_risk_override_applied_when_enabled() -> None:
    out = _run(
        decisions=[_decision("BUY")],
        ai_kwargs={"adjustments": {"max_position": 5}},
        config=_config(ai_override_risk=True),
    )
    assert out.risk.adjustments_applied == [{"max_position": 5}]


def test_risk_override_failure_is_recorded_but_status_still_updates() -> None:
    out = _run(
        decisions=[_decision("BUY")],
        ai_kwargs={"adj_exc": asyncio.TimeoutError()},
        config=_config(ai_override_risk=True),
    )
    kinds = [k for k, _ in out.recorder.events]
    # one ai_decisions + one ai_risk_timeout
    assert "ai_decisions" in kinds and "risk_events" in kinds
    risk_events = [p for k, p in out.recorder.events if k == "risk_events"]
    assert any(p["event"] == "ai_risk_timeout" for p in risk_events)
    # Status update still runs at the end of the function
    assert out.dash.last_update is not None
