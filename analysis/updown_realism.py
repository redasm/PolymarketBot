"""Realistic re-pricing of the 6.20-6.26 UPDOWN shadow book.

The recorded shadow (`positions_lifecycle.ndjson`) books every position at the
book seen *at the decision instant* and charges a flat 0.005 taker rate. Two
things break that:

1. **Fee.** The archived market payload for the exact recorded markets carries
   ``feeSchedule = {rate: 0.07, exponent: 1, takerOnly: true}`` -- 14x the rate
   the shadow charged. Taker fee per share is ``rate * p * (1-p)``.
2. **Latency.** A decision at ``t`` can only be filled against the book at
   ``t + d``. Two fill semantics matter and differ a lot:
     * ``market`` -- pay whatever the book is at ``t+d`` (what latency_gate does).
     * ``limit``  -- the bot actually sends FOK/FAK at the decision price, so a
       book that moved away simply does not fill. Unfilled entries drop the
       position entirely; unfilled exits keep walking forward until the limit is
       reachable, else settle at the last observed bid.

Usage:
    python analysis/updown_realism.py --data-dir E:/PolymarketData/6.20-6.26/data
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import pickle
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.backtest.gates.latency_gate import window_minutes

_CLOSE_EVENTS = ("position_closed", "position_partially_closed")
PAD_BEFORE_MS = 2_000
PAD_AFTER_MS = 120_000


@dataclass
class Pos:
    token_id: str
    open_ts: float
    close_ts: float
    open_price: float
    close_price: float
    close_size: float
    fee: float
    realized_pnl: float
    window: int | None
    day: str


def load_positions(telemetry_dir: str) -> list[Pos]:
    out: list[Pos] = []
    for path in sorted(glob.glob(os.path.join(telemetry_dir, "*.positions_lifecycle.ndjson"))):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("event") not in _CLOSE_EVENTS:
                    continue
                ctx = d.get("decision_context") or {}
                vals = (d.get("open_price"), d.get("close_price"), d.get("close_size"),
                        d.get("open_ts"), d.get("close_ts"), d.get("token_id"))
                if any(v is None for v in vals):
                    continue
                out.append(Pos(
                    token_id=str(d["token_id"]),
                    open_ts=float(d["open_ts"]),
                    close_ts=float(d["close_ts"]),
                    open_price=float(d["open_price"]),
                    close_price=float(d["close_price"]),
                    close_size=float(d["close_size"]),
                    fee=float(d.get("fees") or 0.0),
                    realized_pnl=float(d.get("realized_pnl") or 0.0),
                    window=window_minutes(ctx.get("event_title")),
                    day=str(d.get("ts") or "")[:10],
                ))
    return out


def load_ticks(ticks_dir: str, spans: dict[str, list[tuple[int, int]]], cache: str | None):
    """Per-token [(ts_ms, bids_top3, asks_top3)] limited to the spans we need."""
    if cache and os.path.isfile(cache):
        with open(cache, "rb") as fh:
            return pickle.load(fh)
    merged: dict[str, list[tuple[int, int]]] = {}
    for tok, iv in spans.items():
        iv.sort()
        cur: list[list[int]] = []
        for lo, hi in iv:
            if cur and lo <= cur[-1][1]:
                cur[-1][1] = max(cur[-1][1], hi)
            else:
                cur.append([lo, hi])
        merged[tok] = [(a, b) for a, b in cur]

    series: dict[str, list[tuple]] = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(ticks_dir, "*.ndjson"))):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                if '"book"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tok = d.get("token_id")
                iv = merged.get(tok)
                if iv is None:
                    continue
                ts = int(d["ts_ms"])
                idx = bisect.bisect_right(iv, (ts, 1 << 62)) - 1
                if idx < 0 or ts > iv[idx][1]:
                    continue
                series[tok].append((ts, d.get("bids_top3"), d.get("asks_top3")))
    for tok in series:
        series[tok].sort(key=lambda x: x[0])
    series = dict(series)
    if cache:
        with open(cache, "wb") as fh:
            pickle.dump(series, fh, protocol=4)
    return series


def vwap(levels: Any, size: float) -> tuple[float | None, float]:
    """VWAP price for `size` shares walking `levels`; returns (price, filled)."""
    if not isinstance(levels, list) or size <= 0:
        return None, 0.0
    cost = filled = 0.0
    for lv in levels:
        if not isinstance(lv, (list, tuple)) or len(lv) < 2:
            continue
        px, sz = float(lv[0]), float(lv[1])
        take = min(sz, size - filled)
        if take <= 0:
            break
        cost += take * px
        filled += take
        if filled >= size - 1e-9:
            break
    if filled <= 0:
        return None, 0.0
    return cost / filled, filled


def idx_at(ser: list[tuple], ts_ms: int) -> int:
    """First index with ts >= ts_ms (-1 when none)."""
    lo, hi = 0, len(ser)
    while lo < hi:
        mid = (lo + hi) // 2
        if ser[mid][0] < ts_ms:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(ser) else -1


def fee_of(price: float, size: float, rate: float) -> float:
    p = max(0.0, min(1.0, price))
    return size * rate * p * (1.0 - p)


@dataclass
class Acc:
    n: int = 0
    filled: int = 0
    pnl: float = 0.0
    fees: float = 0.0
    wins: int = 0
    settled: int = 0
    by_day: dict = field(default_factory=lambda: defaultdict(float))


def simulate(positions, series, *, delay_s: float, rate: float, mode: str) -> Acc:
    acc = Acc()
    delay_ms = int(delay_s * 1000)
    for p in positions:
        acc.n += 1
        ser = series.get(p.token_id)
        if not ser:
            continue
        size = p.close_size
        # ---- entry (BUY, taker) ----
        i = idx_at(ser, int(p.open_ts * 1000) + delay_ms)
        if i < 0:
            continue
        ask_px, ask_fill = vwap(ser[i][2], size)
        if ask_px is None:
            continue
        if mode in ("limit", "mixed") and ask_px > p.open_price + 1e-9:
            continue  # FOK at the decision price would not fill; no position
        entry_px = ask_px
        size_eff = min(size, ask_fill)
        if size_eff <= 0:
            continue

        # ---- exit (SELL, taker) ----
        j = idx_at(ser, int(p.close_ts * 1000) + delay_ms)
        if j < 0:
            j = len(ser) - 1
        exit_px = None
        settled = False
        if mode == "limit":
            for k in range(j, len(ser)):
                bid_px, _ = vwap(ser[k][1], size_eff)
                if bid_px is not None and bid_px >= p.close_price - 1e-9:
                    exit_px = bid_px
                    break
            if exit_px is None:
                exit_px, _ = vwap(ser[-1][1], size_eff)
                settled = True
        else:
            # market / mixed: T2 exits go FAK, so they take whatever the bid is
            # at t_close + delay rather than waiting for the decision price.
            exit_px, _ = vwap(ser[j][1], size_eff)
        if exit_px is None:
            continue

        f = fee_of(entry_px, size_eff, rate) + fee_of(exit_px, size_eff, rate)
        pnl = (exit_px - entry_px) * size_eff - f
        acc.filled += 1
        acc.pnl += pnl
        acc.fees += f
        acc.wins += 1 if pnl > 0 else 0
        acc.settled += 1 if settled else 0
        acc.by_day[p.day] += pnl
    return acc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--delays", default="0,0.25,0.5,1,2")
    ap.add_argument("--rates", default="0.005,0.07")
    args = ap.parse_args()

    tele = os.path.join(args.data_dir, "telemetry")
    ticks = os.path.join(args.data_dir, "ticks")
    positions = load_positions(tele)
    print(f"loaded {len(positions)} close events", flush=True)

    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for p in positions:
        spans[p.token_id].append((int(p.open_ts * 1000) - PAD_BEFORE_MS,
                                  int(p.close_ts * 1000) + PAD_AFTER_MS))
    series = load_ticks(ticks, spans, args.cache)
    print(f"tick series for {len(series)} tokens, "
          f"{sum(len(v) for v in series.values())} rows", flush=True)

    delays = [float(x) for x in args.delays.split(",") if x.strip()]
    rates = [float(x) for x in args.rates.split(",") if x.strip()]

    for win in (5, 15, None):
        pos = [p for p in positions if (win is None or p.window == win)]
        if not pos:
            continue
        label = f"window={win}min" if win else "ALL windows"
        shadow = sum(p.realized_pnl for p in pos)
        print(f"\n=== {label} | n={len(pos)} | recorded shadow PnL ${shadow:+.2f} ===")
        print(f"{'mode':7}{'rate':>7}{'delay':>7}{'fills':>8}{'fill%':>7}"
              f"{'net PnL':>12}{'fees':>10}{'win%':>7}{'settled':>9}")
        for mode in ("market", "limit"):
            for rate in rates:
                for d in delays:
                    a = simulate(pos, series, delay_s=d, rate=rate, mode=mode)
                    fr = a.filled / a.n if a.n else 0
                    wr = a.wins / a.filled if a.filled else 0
                    print(f"{mode:7}{rate:>7}{d:>7}{a.filled:>8}{fr:>7.1%}"
                          f"{a.pnl:>12.2f}{a.fees:>10.2f}{wr:>7.1%}{a.settled:>9}",
                          flush=True)


if __name__ == "__main__":
    main()
