"""Async refresh state machine for the research signal service.

The research signal service runs on its own `ThreadPoolExecutor` because
its collectors hit slow / flaky external endpoints (RSS, Surf, JSONL).
The orchestrator must therefore:

- Keep a `last_report` so the current cycle can read research overlays
  without blocking on a refresh in flight.
- Skip submitting a new refresh while another one is running.
- Trigger a refresh whenever the candidate-market list shifts (new
  signature) or when it has been long enough since the last submit.

This module owns that state machine. The original implementation was
inline in `main_loop.py` and could not be tested without a real thread
pool. Extracted so the state transitions can be exercised with a fake
future + fake clock.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from polymarket_arb.models import MarketInfo, ResearchSignalReport

if TYPE_CHECKING:
    from research_signal.service import ResearchSignalService

LOG = logging.getLogger("main_loop")

# Initial submit cool-down for research_signal: avoids hammering the
# external sources from a hot reconnect loop while we're still waiting
# for the very first report to land.
RESEARCH_RESUBMIT_COOLDOWN_SEC = 30.0


@dataclass
class ResearchRefreshState:
    """Mutable state for the research-refresh state machine.

    Lives across cycles so an in-flight refresh's result is delivered
    on the next cycle, and the orchestrator never blocks on it.
    """

    last_report: ResearchSignalReport | None = None
    last_report_signature: tuple[str, ...] = ()
    pending_future: Future | None = None
    pending_signature: tuple[str, ...] = ()
    last_submit_ts: float = 0.0


def research_market_sample(
    *,
    universe_markets: list[MarketInfo],
    scanned_markets: list[MarketInfo],
    max_items: int,
) -> list[MarketInfo]:
    """Pick which markets get fed to the research signal collectors.

    Prefers the broader `universe_markets` so the research layer sees
    the same scope as the dashboard catalog; falls back to the smaller
    `scanned_markets` if the universe hasn't refreshed yet.
    """
    source_markets = universe_markets if universe_markets else scanned_markets
    if max_items <= 0:
        return list(source_markets)
    return list(source_markets[:max_items])


def research_market_signature(markets: list[MarketInfo]) -> tuple[str, ...]:
    """Stable order-independent signature for a market sample.

    Used to detect "the candidate set actually changed" so we don't
    burn an external API call every cycle when nothing moved. Sorted
    so reordering alone never flips the signature.
    """
    return tuple(
        sorted(
            f"{market.event_id}:{market.condition_id}:{(market.question or '').strip().lower()[:80]}"
            for market in markets
        )
    )


def advance_research_refresh(
    *,
    research_signal_service: "ResearchSignalService" | None,
    research_executor: ThreadPoolExecutor | None,
    state: ResearchRefreshState,
    universe_markets: list[MarketInfo],
    scanned_markets: list[MarketInfo],
    max_items: int,
    window_sec: int,
    refresh_interval_sec: float,
    now_ts: float | None = None,
) -> ResearchSignalReport | None:
    """Drive the refresh state machine one tick.

    Steps (in order):
    1. If a refresh is in flight and `done()`, harvest its result and
       publish it as `last_report`.
    2. Compute the current sample's signature.
    3. Submit a new refresh when one of these is true:
       - the signature changed (universe shifted), OR
       - we have a `last_report` and `refresh_interval_sec` has elapsed, OR
       - we have no report yet and the cool-down has elapsed.
    4. Return `last_report` if its signature still matches the current
       sample, else `None` so the orchestrator does not apply a stale
       overlay during a transition.
    """
    now_ts = float(now_ts if now_ts is not None else time.time())
    if state.pending_future is not None and state.pending_future.done():
        try:
            result = state.pending_future.result()
        except Exception as e:
            LOG.error("Research signal 刷新失败: %s", e, exc_info=True)
        else:
            state.last_report = result
            state.last_report_signature = state.pending_signature
        state.pending_future = None
        state.pending_signature = ()

    sample_markets = research_market_sample(
        universe_markets=universe_markets,
        scanned_markets=scanned_markets,
        max_items=max_items,
    )
    current_signature = research_market_signature(sample_markets)
    if (
        research_signal_service is not None
        and research_executor is not None
        and sample_markets
        and state.pending_future is None
    ):
        signature_changed = current_signature != state.last_report_signature
        periodic_refresh_due = (
            state.last_report is not None
            and not signature_changed
            and (now_ts - state.last_submit_ts) >= max(1.0, float(refresh_interval_sec))
        )
        initial_refresh_due = (
            state.last_report is None
            and (state.last_submit_ts <= 0 or (now_ts - state.last_submit_ts) >= RESEARCH_RESUBMIT_COOLDOWN_SEC)
        )
        if signature_changed or periodic_refresh_due or initial_refresh_due:
            state.pending_future = research_executor.submit(
                research_signal_service.collect_report,
                sample_markets,
                window_sec,
            )
            state.pending_signature = current_signature
            state.last_submit_ts = now_ts

    if state.last_report is None or state.last_report_signature != current_signature:
        return None
    return state.last_report
