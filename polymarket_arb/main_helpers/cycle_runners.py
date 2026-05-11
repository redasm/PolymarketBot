"""Per-cycle orchestration primitives lifted out of `main_loop.main()`.

These helpers own three orthogonal concerns of one scan cycle:

- `refresh_market_universe` — periodic re-fetch of active markets/events.
- `start_ws_feed` — bind primary market + spawn the WebSocket mirror.
- `scan_cycle` — sweep candidate markets/events for T0 arbitrage.
- `find_pending_signal` / `find_pending_signal_overlay` — small lookups
  the AI cycle uses to attach research overlay metadata to a freshly
  submitted signal.

Each function takes its dependencies explicitly (no module-level state)
so they can be unit-tested with stubs. The original implementations
were inline in `main_loop.py`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from polymarket_arb.arbitrage_detector import ArbitrageDetector
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.config import ArbConfig
from polymarket_arb.main_helpers.flow_aggregator import FlowIngest
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.models import ArbOpportunity, MarketInfo
from polymarket_arb.strategies.strategy_orchestrator import StrategyOrchestrator, StrategySignal
from polymarket_arb.tick_recorder import TickRecorder
from polymarket_arb.websocket_feed import OrderBookMirror, WebSocketFeed

LOG = logging.getLogger("main_loop")


def refresh_market_universe(
    *,
    scanner: MarketScanner,
    config: ArbConfig,
    cached_markets: list[MarketInfo],
    cached_events: list[Any],
    last_refresh_ts: float,
) -> tuple[list[MarketInfo], list[Any], float, bool]:
    """Re-fetch the active market+event universe when the cache is cold or stale.

    Returns `(markets, events, last_refresh_ts, refreshed)` — `refreshed`
    lets the caller distinguish "we just refreshed" (write a new
    snapshot to dashboard / event log) from "served from cache".

    Cold start (`cached_markets` / `cached_events` empty) always
    refreshes regardless of `last_refresh_ts` so the very first cycle
    has a complete universe instead of waiting an interval.
    """
    now = time.time()
    should_refresh = (
        not cached_markets
        or not cached_events
        or (now - last_refresh_ts) >= config.market_universe_refresh_sec
    )
    if not should_refresh:
        return cached_markets, cached_events, last_refresh_ts, False

    markets = scanner.fetch_active_markets(
        min_liquidity=config.min_liquidity,
        min_volume_24h=config.min_volume_24h,
    )
    events = scanner.fetch_active_events(limit=max(50, config.hot_event_pool_size * 2))
    return markets, events, now, True


def start_ws_feed(
    targets: list[MarketInfo],
    enhanced_store: EnhancedBookStore,
    tick_recorder: TickRecorder | None = None,
    flow_ingest: FlowIngest | None = None,
) -> tuple[WebSocketFeed, OrderBookMirror]:
    """Spin up the WS mirror, bind the primary market, and start the feed.

    The first market in `targets` is treated as primary: its YES/NO
    tokens drive `EnhancedBookStore` for the EdgeEngine. Every other
    market's tokens are still subscribed to the mirror so the per-cycle
    scan path can read fresh books from any of them — but `EnhancedBookStore`
    is single-market by design (see `book_store.py`).

    `flow_ingest`, when provided, also rebuilds its token-id lookup
    for the new target set and is wired in as the WS feed's trade
    consumer so `last_trade_price` events feed the FlowAggregator.
    """
    mirror = OrderBookMirror()
    if tick_recorder is not None and tick_recorder.is_enabled:
        tick_recorder.register_markets(targets)
        mirror.register_callback(tick_recorder.on_book_update)

    primary = targets[0]
    yes_token = next((t for t in primary.tokens if t.outcome.lower() == "yes"), primary.tokens[0])
    no_token = next((t for t in primary.tokens if t.outcome.lower() == "no"), primary.tokens[-1])
    enhanced_store.set_market(primary.condition_id, yes_token.token_id, no_token.token_id)

    all_token_ids: list[str] = []
    for m in targets:
        for t in m.tokens:
            all_token_ids.append(t.token_id)

    trade_callback = None
    if flow_ingest is not None:
        flow_ingest.register_markets(targets)
        trade_callback = flow_ingest.on_trade

    feed = WebSocketFeed(
        mirror=mirror,
        enhanced_store=enhanced_store,
        trade_callback=trade_callback,
    )
    feed.subscribe(all_token_ids)
    feed.start()

    LOG.info(
        "WebSocket 已启动: 主市场=%s (%s), 共订阅 %d 个 token",
        primary.condition_id[:12],
        primary.question[:40],
        len(all_token_ids),
    )
    return feed, mirror


def scan_cycle(
    detector: ArbitrageDetector,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    candidate_events: list[Any],
    universe_market_count: int,
    universe_refreshed: bool,
    progress_cb: Any | None = None,
) -> list[ArbOpportunity]:
    """Sweep the hot pool for T0 (binary + multi-outcome) arbitrage.

    `progress_cb` is an opt-in dashboard ping; it's invoked in two
    "phase" segments (`scanning_books`, `scanning_events`) plus a
    throttled per-25-markets pulse so the UI shows progress without
    drowning the main loop in callback overhead.

    Sorted by `net_edge` descending so the orchestrator's downstream
    capital-allocation step always sees the best opportunity first.
    """
    # `config` is in the signature for forward compatibility (e.g. fee
    # gates per arb type). `_ = config` stops linters from flagging it
    # as unused without changing the call signature consumers rely on.
    _ = config
    opportunities: list[ArbOpportunity] = []
    if progress_cb is not None:
        progress_cb(
            phase="scanning_books",
            scanned_markets=len(candidate_markets),
            universe_markets=universe_market_count,
            universe_refreshed=universe_refreshed,
        )

    for idx, market in enumerate(candidate_markets, start=1):
        if len(market.tokens) == 2:
            opp = detector.scan_binary_market(market)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)
        if progress_cb is not None and (idx == 1 or idx % 25 == 0 or idx == len(candidate_markets)):
            progress_cb(
                phase="scanning_books",
                scanned_markets=len(candidate_markets),
                scanned_orderbooks=idx,
                universe_markets=universe_market_count,
                universe_refreshed=universe_refreshed,
                opportunities_found=len(opportunities),
            )

    if progress_cb is not None:
        progress_cb(
            phase="scanning_events",
            scanned_markets=len(candidate_markets),
            scanned_events=len(candidate_events),
            universe_markets=universe_market_count,
            universe_refreshed=universe_refreshed,
            opportunities_found=len(opportunities),
        )
    for event in candidate_events:
        if len(event.markets) >= 2:
            opp = detector.scan_multi_outcome_event(event)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)

    opportunities.sort(key=lambda o: o.net_edge, reverse=True)
    return opportunities


def find_pending_signal(
    orchestrator: StrategyOrchestrator,
    signal: StrategySignal,
) -> StrategySignal | None:
    """Locate the orchestrator's queued copy of `signal` for overlay reads.

    Matches on (market_id, signal_type, timestamp) — the timestamp
    approximation tolerates the ~µs gap between submit and lookup.
    Reads the orchestrator's private `_pending_signals` list because
    that's the storage the lookup is meant to inspect; the alternative
    would be to expose another iteration accessor on the orchestrator.
    """
    for pending in getattr(orchestrator, "_pending_signals", []):
        if (
            pending.market_id == signal.market_id
            and pending.signal_type == signal.signal_type
            and abs(float(pending.timestamp) - float(signal.timestamp)) < 1e-6
        ):
            return pending
    return None


def find_pending_signal_overlay(
    orchestrator: StrategyOrchestrator,
    signal: StrategySignal,
) -> dict[str, Any]:
    """Return the research overlay payload attached to the queued signal, or {}."""
    pending = find_pending_signal(orchestrator, signal)
    if pending is not None:
        return dict(pending.payload.get("research_overlay", {}))
    return {}
