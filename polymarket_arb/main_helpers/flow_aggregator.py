"""Per-market taker-flow aggregator (minimum-viable Becker 2025 follow-up).

Goal: estimate, per condition_id, what fraction of recent taker volume
went into YES exposure vs NO exposure. The paper documents an "Optimism
Tax" where takers disproportionately buy YES at longshot prices, and
the structural maker edge comes from being the counterparty to that
asymmetric flow.

This module is the data plumbing only. It:

- Accepts trade observations via :meth:`FlowAggregator.record_trade` from
  the WebSocket ``last_trade_price`` stream.
- Maintains a sliding window of recent trades per market (cost-basis
  normalised, share-weighted).
- Persists state to JSON between restarts so a freshly-started bot can
  immediately read the prior window's bias.
- Exposes :meth:`FlowAggregator.get_bias` for collectors / telemetry.

It does NOT yet decide which side T3 should make on — that step
(actually biasing the quote) is intentionally left for a follow-up
once the dataset is large enough to validate. For now collectors emit
``flow_bias`` in the signal payload so we can post-hoc check whether
the inferred bias agrees with realised PnL.

Taker-direction mapping (binary token economics):

    side=BUY  on YES token  → taker long  YES  (paid `price` for 1 YES share)
    side=SELL on YES token  → taker short YES (= long NO)
    side=BUY  on NO  token  → taker long  NO
    side=SELL on NO  token  → taker short NO  (= long YES)

so ``taker_bought_yes`` ⇔ (yes_token AND BUY) OR (no_token AND SELL).
We count shares (not USD) because the paper's cost-basis Cb is share-
denominated; a 1¢ YES and 1¢ NO both count as 1¢ of "capital risked
per share".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Iterable, Optional

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowBias:
    """Snapshot of one market's taker-flow bias over the active window.

    ``taker_yes_share`` is the share-weighted fraction of taker volume
    that took YES exposure. Stable / strong flags surface whether the
    sample is large enough to act on (``is_stable``) and whether it
    diverges meaningfully from 50/50 (``is_strong``).
    """

    condition_id: str
    taker_yes_shares: float
    taker_no_shares: float
    trade_count: int
    window_sec: float
    last_trade_ts: float
    min_trades: int
    strong_threshold: float

    @property
    def total_shares(self) -> float:
        return self.taker_yes_shares + self.taker_no_shares

    @property
    def taker_yes_share(self) -> float:
        total = self.total_shares
        if total <= 0:
            return 0.5
        return self.taker_yes_shares / total

    @property
    def is_stable(self) -> bool:
        return self.trade_count >= self.min_trades and self.total_shares > 0

    @property
    def is_strong(self) -> bool:
        if not self.is_stable:
            return False
        share = self.taker_yes_share
        return share >= self.strong_threshold or share <= (1.0 - self.strong_threshold)

    @property
    def lean(self) -> str:
        """Coarse direction tag: ``yes`` / ``no`` / ``neutral``."""
        if not self.is_strong:
            return "neutral"
        return "yes" if self.taker_yes_share >= 0.5 else "no"

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "taker_yes_share": round(self.taker_yes_share, 4),
            "taker_yes_shares": round(self.taker_yes_shares, 4),
            "taker_no_shares": round(self.taker_no_shares, 4),
            "total_shares": round(self.total_shares, 4),
            "trade_count": self.trade_count,
            "window_sec": self.window_sec,
            "last_trade_ts": self.last_trade_ts,
            "is_stable": self.is_stable,
            "is_strong": self.is_strong,
            "lean": self.lean,
        }


class FlowAggregator:
    """Sliding-window per-market taker-flow accumulator.

    Each market keeps a ``deque`` of ``(ts, taker_bought_yes, shares)``
    tuples; entries older than ``window_sec`` are dropped on every
    record/read. State is mirrored to ``state_file`` on every record
    so a restart starts warm.

    Persistence is best-effort: a failed disk write logs a warning and
    leaves the in-memory state intact. Reads are cheap; writes do a
    temp-file + atomic ``os.replace`` so concurrent readers never see
    a truncated file.
    """

    _MAX_TRADES_PER_MARKET = 5000

    def __init__(
        self,
        *,
        window_sec: float = 3600.0,
        min_trades: int = 20,
        strong_threshold: float = 0.55,
        state_file: Optional[str] = None,
        persist_every_n: int = 10,
    ):
        if not (0.5 <= float(strong_threshold) <= 1.0):
            raise ValueError("strong_threshold must be in [0.5, 1.0]")
        self._window_sec = float(window_sec)
        self._min_trades = max(1, int(min_trades))
        self._strong_threshold = float(strong_threshold)
        self._state_file = Path(state_file) if state_file else None
        self._persist_every_n = max(1, int(persist_every_n))
        self._lock = threading.Lock()
        # Per-market deques of (ts, taker_bought_yes, shares). Bounded by
        # _MAX_TRADES_PER_MARKET so a runaway feed cannot exhaust memory
        # even if `window_sec` is huge.
        self._trades: dict[str, Deque[tuple[float, bool, float]]] = {}
        self._writes_since_persist = 0
        if self._state_file is not None:
            self._load_state()

    @property
    def window_sec(self) -> float:
        return self._window_sec

    @property
    def min_trades(self) -> int:
        return self._min_trades

    @property
    def strong_threshold(self) -> float:
        return self._strong_threshold

    def record_trade(
        self,
        *,
        condition_id: str,
        taker_bought_yes: bool,
        shares: float,
        ts: Optional[float] = None,
    ) -> None:
        """Append a trade observation to the market's sliding window.

        ``shares`` should be the trade size in tokens (cost-basis
        normalised — i.e. how many YES- or NO-equivalent shares the
        taker assumed exposure on). Zero / negative sizes are dropped.
        """
        if not condition_id or shares <= 0:
            return
        now = float(ts) if ts is not None else time.time()
        cutoff = now - self._window_sec
        with self._lock:
            bucket = self._trades.setdefault(condition_id, deque())
            bucket.append((now, bool(taker_bought_yes), float(shares)))
            self._trim_inplace(bucket, cutoff)
            if len(bucket) > self._MAX_TRADES_PER_MARKET:
                # FIFO eviction past hard cap; should only trigger if a
                # market is much hotter than window_sec is calibrated for.
                drop = len(bucket) - self._MAX_TRADES_PER_MARKET
                for _ in range(drop):
                    bucket.popleft()
            self._writes_since_persist += 1
            should_persist = self._writes_since_persist >= self._persist_every_n
        if should_persist:
            self._persist()

    def get_bias(
        self,
        condition_id: str,
        *,
        now: Optional[float] = None,
    ) -> Optional[FlowBias]:
        """Return the current bias snapshot, or None if the market has no data."""
        if not condition_id:
            return None
        cutoff = (float(now) if now is not None else time.time()) - self._window_sec
        with self._lock:
            bucket = self._trades.get(condition_id)
            if not bucket:
                return None
            self._trim_inplace(bucket, cutoff)
            if not bucket:
                # Pruned to empty after eviction; drop the key so memory
                # doesn't accumulate for dormant markets.
                self._trades.pop(condition_id, None)
                return None
            yes_shares = 0.0
            no_shares = 0.0
            for _, taker_yes, size in bucket:
                if taker_yes:
                    yes_shares += size
                else:
                    no_shares += size
            last_ts = bucket[-1][0]
            count = len(bucket)
        return FlowBias(
            condition_id=condition_id,
            taker_yes_shares=yes_shares,
            taker_no_shares=no_shares,
            trade_count=count,
            window_sec=self._window_sec,
            last_trade_ts=last_ts,
            min_trades=self._min_trades,
            strong_threshold=self._strong_threshold,
        )

    def snapshot(self, *, now: Optional[float] = None) -> dict[str, FlowBias]:
        """All currently-tracked markets keyed by condition_id."""
        with self._lock:
            ids = list(self._trades.keys())
        result: dict[str, FlowBias] = {}
        for cid in ids:
            bias = self.get_bias(cid, now=now)
            if bias is not None:
                result[cid] = bias
        return result

    def reset_market(self, condition_id: str) -> None:
        """Drop all recorded trades for a market (used by tests)."""
        with self._lock:
            self._trades.pop(condition_id, None)

    def close(self) -> None:
        """Flush state to disk on shutdown."""
        if self._state_file is None:
            return
        self._persist()

    @staticmethod
    def _trim_inplace(bucket: Deque[tuple[float, bool, float]], cutoff: float) -> None:
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()

    def _persist(self) -> None:
        if self._state_file is None:
            return
        with self._lock:
            payload = {
                "window_sec": self._window_sec,
                "min_trades": self._min_trades,
                "strong_threshold": self._strong_threshold,
                "saved_at": time.time(),
                "markets": {
                    cid: [(ts, bool(yes), float(size)) for ts, yes, size in bucket]
                    for cid, bucket in self._trades.items()
                    if bucket
                },
            }
            self._writes_since_persist = 0
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=self._state_file.name + ".",
                suffix=".tmp",
                dir=str(self._state_file.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp, separators=(",", ":"))
                os.replace(tmp_path, self._state_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            LOG.warning("flow_aggregator persist failed: %s", exc)

    def _load_state(self) -> None:
        assert self._state_file is not None
        if not self._state_file.exists():
            return
        try:
            with self._state_file.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("flow_aggregator state load failed: %s", exc)
            return
        markets = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(markets, dict):
            return
        loaded = 0
        # We do NOT prune by wall-clock here. The window cutoff is applied
        # lazily inside `get_bias` once the caller supplies its reference
        # time. Pruning at load time would mean a backtest replaying old
        # data could not warm-start from a persisted state file.
        for cid, rows in markets.items():
            if not isinstance(rows, list):
                continue
            bucket: Deque[tuple[float, bool, float]] = deque()
            for row in rows:
                ts, taker_yes, shares = _parse_state_row(row)
                if ts is None or shares <= 0:
                    continue
                bucket.append((ts, bool(taker_yes), float(shares)))
            # Sort by ts to keep the deque FIFO-ordered after a reload —
            # the persisted file may have been written out of order if
            # there were concurrent record_trade calls in flight.
            ordered = sorted(bucket, key=lambda row: row[0])
            if not ordered:
                continue
            if len(ordered) > self._MAX_TRADES_PER_MARKET:
                ordered = ordered[-self._MAX_TRADES_PER_MARKET:]
            self._trades[str(cid)] = deque(ordered)
            loaded += len(ordered)
        if loaded:
            LOG.info(
                "flow_aggregator state loaded: markets=%d trades=%d window=%.0fs",
                len(self._trades),
                loaded,
                self._window_sec,
            )


def _parse_state_row(row: object) -> tuple[Optional[float], bool, float]:
    """Tolerant decoder for the persisted ``(ts, taker_yes, shares)`` tuple."""
    if isinstance(row, (list, tuple)) and len(row) >= 3:
        try:
            ts = float(row[0])
            taker_yes = bool(row[1])
            shares = float(row[2])
        except (TypeError, ValueError):
            return None, False, 0.0
        return ts, taker_yes, shares
    return None, False, 0.0


def derive_taker_bought_yes(*, is_yes_token: bool, taker_side: str) -> bool:
    """Map ``(token role, taker side)`` → did the taker take on YES exposure?

    side is the taker's action against the resting order
    (``"BUY"`` or ``"SELL"``); see module docstring for the mapping.
    """
    side_upper = (taker_side or "").strip().upper()
    if side_upper not in {"BUY", "SELL"}:
        return False
    if is_yes_token:
        return side_upper == "BUY"
    return side_upper == "SELL"


@dataclass
class _TokenLookup:
    """Internal: condition_id + whether this token is the YES leg."""

    condition_id: str
    is_yes_token: bool


class FlowIngest:
    """Glue between WebSocket trade events and :class:`FlowAggregator`.

    Owns the ``token_id → (condition_id, is_yes_token)`` map so the
    WebSocket layer stays oblivious to market metadata. The map is
    rebuilt every time the WS feed re-targets its subscribed markets.
    """

    def __init__(self, aggregator: FlowAggregator):
        self._agg = aggregator
        self._tokens: dict[str, _TokenLookup] = {}
        self._lock = threading.Lock()

    def register_markets(self, markets: Iterable) -> None:
        """Rebuild the token-id lookup table from a list of MarketInfo."""
        new_map: dict[str, _TokenLookup] = {}
        for market in markets:
            condition_id = getattr(market, "condition_id", "") or ""
            tokens = getattr(market, "tokens", None) or []
            for token in tokens:
                token_id = getattr(token, "token_id", "") or ""
                outcome = (getattr(token, "outcome", "") or "").strip().lower()
                if not token_id or not condition_id:
                    continue
                new_map[token_id] = _TokenLookup(
                    condition_id=condition_id,
                    is_yes_token=(outcome == "yes"),
                )
        with self._lock:
            self._tokens = new_map

    def on_trade(self, event: dict) -> None:
        """WebSocket ``last_trade_price`` handler.

        Expected fields (Polymarket schema):

        - ``asset_id``: token_id whose orderbook absorbed the trade
        - ``side``: taker side, ``"BUY"`` or ``"SELL"``
        - ``size``: trade size in shares
        - ``timestamp`` (optional): ms unix; falls back to current time
        """
        token_id = str(event.get("asset_id", "") or "").strip()
        if not token_id:
            return
        with self._lock:
            lookup = self._tokens.get(token_id)
        if lookup is None:
            return
        try:
            size = float(event.get("size", 0) or 0)
        except (TypeError, ValueError):
            return
        if size <= 0:
            return
        taker_side = str(event.get("side", "") or "")
        taker_yes = derive_taker_bought_yes(
            is_yes_token=lookup.is_yes_token,
            taker_side=taker_side,
        )
        ts_raw = event.get("timestamp")
        ts = _parse_event_timestamp(ts_raw)
        self._agg.record_trade(
            condition_id=lookup.condition_id,
            taker_bought_yes=taker_yes,
            shares=size,
            ts=ts,
        )


def _parse_event_timestamp(raw: object) -> Optional[float]:
    """Normalize Polymarket's millisecond / second timestamps to UTC seconds."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Polymarket's WS feeds timestamps in milliseconds; legacy / mock
    # data may already be in seconds. Anything >= 10^11 we treat as ms.
    return value / 1000.0 if value >= 1e11 else value
