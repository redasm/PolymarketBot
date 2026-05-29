"""Per-cycle orchestration primitives lifted out of `main_loop.main()`.

These helpers own three orthogonal concerns of one scan cycle:

- `refresh_market_universe` — periodic re-fetch of active markets/events.
- `start_ws_feed` — bind primary market + spawn the WebSocket mirror.
- `scan_cycle` — sweep candidate markets/events for T0 arbitrage.
- `find_pending_signal` / `find_pending_signal_overlay` — small lookups
  used to attach research overlay metadata to a freshly submitted signal.

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
from polymarket_arb.main_helpers.dirty_market_tracker import DirtyMarketTracker
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
    # UPDOWN markets carry ~0 24h volume so the volume-filtered fetch above
    # drops them. When enabled, pull them via a dedicated slug-direct probe
    # and merge (dedup by condition_id) so the scan-pool boost has something
    # to promote. Additive + best-effort — never breaks the main refresh.
    if getattr(config, "t2_updown_enabled", False):
        try:
            symbols = [s.strip() for s in config.t2_updown_symbols.split(",") if s.strip()]
            windows = [int(w.strip()) for w in config.t2_updown_window_minutes.split(",") if w.strip()]
            updown = scanner.fetch_updown_markets(
                symbols=symbols,
                window_minutes=windows,
                slots_ahead=config.t2_updown_slots_ahead,
            )
            if updown:
                seen = {m.condition_id for m in markets}
                markets = markets + [m for m in updown if m.condition_id not in seen]
        except Exception:  # noqa: BLE001 - additive path, must not break refresh
            LOG.exception("UPDOWN 市场合并失败（忽略，不影响主 universe）")
    events = scanner.fetch_active_events(limit=max(50, config.hot_event_pool_size * 2))
    return markets, events, now, True


def start_ws_feed(
    targets: list[MarketInfo],
    enhanced_store: EnhancedBookStore,
    tick_recorder: TickRecorder | None = None,
    flow_ingest: FlowIngest | None = None,
    dirty_tracker: "DirtyMarketTracker | None" = None,
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

    ``dirty_tracker`` (P0-dirty) gets fed a callback that marks the
    owning condition_id dirty on every best-bid/ask change. The
    main loop drains the set at the top of the next cycle and uses
    ``dirty_tracker.wake_event`` to cut the inter-cycle sleep short
    when enough markets accumulate.
    """
    mirror = OrderBookMirror()
    if tick_recorder is not None and tick_recorder.is_enabled:
        tick_recorder.register_markets(targets)
        mirror.register_callback(tick_recorder.on_book_update)
    if dirty_tracker is not None:
        # Build the token → condition_id lookup eagerly so callbacks
        # don't need to walk MarketInfo objects each time.
        token_to_cond: dict[str, str] = {}
        for m in targets:
            for t in m.tokens:
                token_to_cond[t.token_id] = m.condition_id
        dirty_tracker.register_token_map(token_to_cond)

        def _on_book_change(token_id: str, _snap) -> None:
            dirty_tracker.mark_dirty(token_id)

        mirror.register_callback(_on_book_change)

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


def refresh_ws_subscription(
    *,
    feed: WebSocketFeed | None,
    mirror: OrderBookMirror | None,
    targets: list[MarketInfo],
    enhanced_store: EnhancedBookStore,
    tick_recorder: TickRecorder | None = None,
    flow_ingest: FlowIngest | None = None,
    dirty_tracker: "DirtyMarketTracker | None" = None,
) -> tuple[WebSocketFeed, OrderBookMirror]:
    """Refresh WS subscription targets without bouncing the connection.

    On first call (``feed is None``) this is equivalent to
    ``start_ws_feed``. On subsequent calls the dependent lookups
    (``tick_recorder.register_markets``, ``flow_ingest.register_markets``,
    ``dirty_tracker.register_token_map``, ``enhanced_store.set_market``)
    are rebuilt in place and the WS-level diff is shipped via
    ``feed.add_tokens`` / ``feed.remove_tokens`` (Dynamic Subscription).

    If Dynamic Subscription fails (e.g. the feed is mid-reconnect and
    ``_ws_handle`` is None) we fall back to a clean ``feed.stop()`` +
    ``start_ws_feed`` so the system never silently runs on a stale
    subscription set. Returns the (possibly new) ``(feed, mirror)``
    tuple the caller should keep for the next refresh.

    Callback registration on ``mirror`` (tick_recorder hook,
    dirty_tracker closure) is NOT repeated when reusing the mirror —
    those closures already hold the live tracker references and pick
    up new token→condition mappings via ``register_token_map``.
    """
    if not targets:
        if feed is not None and mirror is not None:
            return feed, mirror
        raise ValueError("refresh_ws_subscription: targets is empty on first call")

    if feed is None or mirror is None:
        return start_ws_feed(
            targets,
            enhanced_store,
            tick_recorder=tick_recorder,
            flow_ingest=flow_ingest,
            dirty_tracker=dirty_tracker,
        )

    new_token_ids: set[str] = set()
    for m in targets:
        for t in m.tokens:
            new_token_ids.add(t.token_id)

    if tick_recorder is not None and tick_recorder.is_enabled:
        tick_recorder.register_markets(targets)
    if flow_ingest is not None:
        flow_ingest.register_markets(targets)
        feed.set_trade_callback(flow_ingest.on_trade)
    if dirty_tracker is not None:
        token_to_cond: dict[str, str] = {}
        for m in targets:
            for t in m.tokens:
                token_to_cond[t.token_id] = m.condition_id
        dirty_tracker.register_token_map(token_to_cond)

    primary = targets[0]
    yes_token = next((t for t in primary.tokens if t.outcome.lower() == "yes"), primary.tokens[0])
    no_token = next((t for t in primary.tokens if t.outcome.lower() == "no"), primary.tokens[-1])
    enhanced_store.set_market(primary.condition_id, yes_token.token_id, no_token.token_id)

    current = feed.subscribed_tokens()
    to_add = new_token_ids - current
    to_remove = current - new_token_ids

    if not to_add and not to_remove:
        return feed, mirror

    add_ok = feed.add_tokens(to_add) if to_add else True
    remove_ok = feed.remove_tokens(to_remove) if to_remove else True

    if add_ok and remove_ok:
        LOG.info(
            "Dynamic Subscription 已应用: +%d / -%d, 现订阅 %d 个 token",
            len(to_add),
            len(to_remove),
            len(feed.subscribed_tokens()),
        )
        return feed, mirror

    LOG.warning(
        "Dynamic Subscription 失败 (add_ok=%s remove_ok=%s)，回退到 stop+restart",
        add_ok,
        remove_ok,
    )
    feed.stop()
    return start_ws_feed(
        targets,
        enhanced_store,
        tick_recorder=tick_recorder,
        flow_ingest=flow_ingest,
        dirty_tracker=dirty_tracker,
    )


def scan_cycle(
    detector: ArbitrageDetector,
    config: ArbConfig,
    candidate_markets: list[MarketInfo],
    candidate_events: list[Any],
    universe_market_count: int,
    universe_refreshed: bool,
    progress_cb: Any | None = None,
    priority_condition_ids: set[str] | None = None,
) -> list[ArbOpportunity]:
    """Sweep the hot pool for T0 (binary + multi-outcome) arbitrage.

    `progress_cb` is an opt-in dashboard ping; it's invoked in two
    "phase" segments (`scanning_books`, `scanning_events`) plus a
    throttled per-25-markets pulse so the UI shows progress without
    drowning the main loop in callback overhead.

    ``priority_condition_ids`` (P0-dirty): markets whose books were
    just touched by a WS delta. When provided we reorder the iteration
    so these are scanned *first* inside the cycle — same total work,
    but if the cycle is mid-flight when a delta arrives the dirty
    market won't sit at the tail of a 150-market loop. The cold tail
    still runs because periodic full sweeps catch markets whose
    books happen not to move between cycles (e.g. low-volume events
    that briefly cross into profit because of fee changes).

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

    ordered_markets = _prioritize_dirty(candidate_markets, priority_condition_ids)
    for idx, market in enumerate(ordered_markets, start=1):
        if len(market.tokens) == 2:
            opp = detector.scan_binary_market(market)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)
        if progress_cb is not None and (idx == 1 or idx % 25 == 0 or idx == len(ordered_markets)):
            progress_cb(
                phase="scanning_books",
                scanned_markets=len(ordered_markets),
                scanned_orderbooks=idx,
                universe_markets=universe_market_count,
                universe_refreshed=universe_refreshed,
                opportunities_found=len(opportunities),
            )

    if progress_cb is not None:
        progress_cb(
            phase="scanning_events",
            scanned_markets=len(ordered_markets),
            scanned_events=len(candidate_events),
            universe_markets=universe_market_count,
            universe_refreshed=universe_refreshed,
            opportunities_found=len(opportunities),
        )
    ordered_events = _prioritize_dirty_events(candidate_events, priority_condition_ids)
    for event in ordered_events:
        if len(event.markets) >= 2:
            opp = detector.scan_multi_outcome_event(event)
            if opp is not None and opp.is_profitable:
                opportunities.append(opp)

    opportunities.sort(key=lambda o: o.net_edge, reverse=True)
    return opportunities


def _prioritize_dirty(
    markets: list[MarketInfo],
    priority: set[str] | None,
) -> list[MarketInfo]:
    """Return ``markets`` with priority condition_ids first; stable order otherwise."""
    if not priority:
        return markets
    dirty: list[MarketInfo] = []
    cold: list[MarketInfo] = []
    for m in markets:
        (dirty if m.condition_id in priority else cold).append(m)
    return dirty + cold


def _prioritize_dirty_events(
    events: list[Any],
    priority: set[str] | None,
) -> list[Any]:
    if not priority:
        return events
    dirty: list[Any] = []
    cold: list[Any] = []
    for event in events:
        if any(m.condition_id in priority for m in event.markets):
            dirty.append(event)
        else:
            cold.append(event)
    return dirty + cold


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
