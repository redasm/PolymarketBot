"""Delay-injection look-ahead gate for recorded shadow positions.

Background
---------
A shadow / dry-run strategy records, for each closed position, the price it
*assumed* it could trade at (``open_price`` = best_ask at decision time,
``close_price`` = best_bid at decision time). Those prices are read from the same
tick the signal fired on, so the shadow implicitly trades at the decision instant
with zero latency. Real execution always lags the decision by some latency
``d`` (network + matching), so the realizable price is the book at ``t + d``,
not at ``t``.

If a strategy's edge comes from being on the right side of a price move that
happens *within* that latency window, the shadow books the profit but live
trading never can. Re-pricing every position at ``t + d`` and watching the net
PnL collapse is the fastest way to expose this (see memory
``ref-validation-methodology``: delay-injection is the primary look-ahead test).

What this gate does
-------------------
1. Loads closed/partially-closed positions from ``positions_lifecycle.ndjson``
   (filtered to a market-window size, e.g. 15-minute UPDOWN).
2. Streams the recorded ``ticks/*.ndjson`` and keeps, per token, the time series
   of ``(ts_ms, best_bid, best_ask, bids_top3)``.
3. Rebuilds a same-snapshot baseline (book strictly at-or-before the decision
   instant) and asserts it reproduces the recorded shadow PnL — this guards
   against the re-pricing logic itself being wrong.
4. For each delay ``d`` in ``--delays``, re-prices entries at the first tick
   strictly after ``t_open + d`` (BUY pays best_ask) and exits at the first tick
   after ``t_close + d`` (SELL hits best_bid), and reports the surviving net PnL
   and retention %.
5. Decomposes the headline delay into entry-only and exit-only legs so the leak
   side is unambiguous.

Exit code is non-zero when retention at ``--fail-delay`` drops below
``--min-retention`` — i.e. the gate FAILS and the strategy must not go live.

Usage
-----
    python -m research.backtest.gates.latency_gate \\
        --data-dir <DATA_ROOT>/6.20-6.26/data \\
        --window-minutes 15 \\
        --delays 0.5,1,2,5 \\
        --fail-delay 1.0 --min-retention 0.5

``--data-dir`` must contain ``telemetry/*.positions_lifecycle.ndjson`` and
``ticks/*.ndjson`` (the standard layout produced by the live/shadow bot).
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any

_TITLE_RE = re.compile(r"(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)")
_CLOSE_EVENTS = ("position_closed", "position_partially_closed")


def window_minutes(title: str | None) -> int | None:
    """Return the market window length in minutes parsed from an event title.

    e.g. "Bitcoin Up or Down - June 20, 7:45AM-8:00AM ET" -> 15.
    Returns None when the title does not carry a HH:MM-HH:MM range.
    """
    if not title:
        return None
    m = _TITLE_RE.search(title)
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()

    def to_min(h: str, mi: str, ap: str) -> int:
        hh = int(h) % 12
        if ap == "PM":
            hh += 12
        return hh * 60 + int(mi)

    delta = to_min(h2, m2, ap2) - to_min(h1, m1, ap1)
    return delta + 1440 if delta < 0 else delta


@dataclass
class PositionRow:
    token_id: str
    open_ts: float
    close_ts: float
    open_price: float
    close_price: float
    close_size: float
    fee: float
    realized_pnl: float


def load_positions(
    telemetry_dir: str, window: int | None
) -> list[PositionRow]:
    """Load closed/partially-closed shadow positions, optionally window-filtered."""
    rows: list[PositionRow] = []
    pattern = os.path.join(telemetry_dir, "*.positions_lifecycle.ndjson")
    for path in sorted(glob.glob(pattern)):
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
                if window is not None and window_minutes(ctx.get("event_title")) != window:
                    continue
                op = d.get("open_price")
                cp = d.get("close_price")
                csz = d.get("close_size")
                ots = d.get("open_ts")
                cts = d.get("close_ts")
                if None in (op, cp, csz, ots, cts):
                    continue
                rows.append(
                    PositionRow(
                        token_id=d.get("token_id"),
                        open_ts=float(ots),
                        close_ts=float(cts),
                        open_price=float(op),
                        close_price=float(cp),
                        close_size=float(csz),
                        fee=float(d.get("fees") or 0.0),
                        realized_pnl=float(d.get("realized_pnl") or 0.0),
                    )
                )
    return rows


# tick tuple layout: (ts_ms, best_bid, best_ask, bids_top3)
TickSeries = dict[str, list[tuple[int, float | None, float | None, Any]]]


def load_tick_series(ticks_dir: str, tokens: set[str]) -> TickSeries:
    """Stream tick files and keep per-token book time series for ``tokens``."""
    series: TickSeries = {}
    pattern = os.path.join(ticks_dir, "*.ndjson")
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                # cheap prefilter before JSON parse
                if '"book"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tok = d.get("token_id")
                if tok not in tokens or d.get("event_type") != "book":
                    continue
                series.setdefault(tok, []).append(
                    (
                        int(d["ts_ms"]),
                        d.get("best_bid"),
                        d.get("best_ask"),
                        d.get("bids_top3"),
                    )
                )
    for tok in series:
        series[tok].sort(key=lambda x: x[0])
    return series


def _book_at(
    series: TickSeries, token: str, ts_ms: int, mode: str
) -> tuple[int, float | None, float | None, Any] | None:
    """Return the book snapshot for ``token`` relative to ``ts_ms``.

    mode == "past":   most recent snapshot at-or-before ts_ms (what the decision
                      instant could actually observe).
    mode == "future": first snapshot strictly after ts_ms (what a delayed order
                      actually executes against). Clamped to the last snapshot.
    """
    s = series.get(token)
    if not s:
        return None
    times = [x[0] for x in s]
    if mode == "past":
        i = bisect.bisect_right(times, ts_ms) - 1
        if i < 0:
            return None
    else:
        i = bisect.bisect_right(times, ts_ms)
        if i >= len(s):
            i = len(s) - 1
    return s[i]


@dataclass
class DelayResult:
    delay_s: float
    net_pnl: float
    n: int
    retention: float  # net_pnl / baseline


@dataclass
class GateReport:
    window: int | None
    n_positions: int
    shadow_pnl: float
    baseline_pnl: float  # same-snapshot rebuild, should ~= shadow_pnl
    rebuild_error: float
    delays: list[DelayResult] = field(default_factory=list)
    entry_only_1s: float | None = None
    exit_only_1s: float | None = None
    entry_ask_drift_1s: float | None = None
    exit_bid_drift_1s: float | None = None

    @property
    def passed(self) -> bool:
        return self._passed

    _passed: bool = True


def _price_position(
    series: TickSeries,
    row: PositionRow,
    entry_delay: float,
    exit_delay: float,
) -> float | None:
    """Net PnL for one position with given entry/exit delays.

    delay == 0 uses the at-or-before snapshot (decision instant); delay > 0 uses
    the first snapshot after t + delay. BUY entry pays best_ask, SELL exit hits
    best_bid. Fee is carried over from the recorded fill (price moves are sub-cent
    so the fee delta is negligible).
    """
    e_mode = "past" if entry_delay == 0 else "future"
    x_mode = "past" if exit_delay == 0 else "future"
    eb = _book_at(series, row.token_id, int((row.open_ts + entry_delay) * 1000), e_mode)
    xb = _book_at(series, row.token_id, int((row.close_ts + exit_delay) * 1000), x_mode)
    if eb is None or xb is None:
        return None
    entry_px = eb[2] if eb[2] is not None else row.open_price
    exit_px = xb[1] if xb[1] is not None else row.close_price
    return (exit_px - entry_px) * row.close_size - row.fee


def run_gate(
    rows: list[PositionRow],
    series: TickSeries,
    delays: list[float],
    fail_delay: float,
    min_retention: float,
    window: int | None,
) -> GateReport:
    shadow_pnl = sum(r.realized_pnl for r in rows)

    # 1) same-snapshot rebuild baseline (must reproduce shadow PnL)
    baseline = 0.0
    base_n = 0
    for r in rows:
        v = _price_position(series, r, 0.0, 0.0)
        if v is not None:
            baseline += v
            base_n += 1

    report = GateReport(
        window=window,
        n_positions=len(rows),
        shadow_pnl=shadow_pnl,
        baseline_pnl=baseline,
        rebuild_error=abs(baseline - shadow_pnl),
    )

    # 2) delay sweep (both legs delayed)
    for d in delays:
        total = 0.0
        n = 0
        for r in rows:
            v = _price_position(series, r, d, d)
            if v is not None:
                total += v
                n += 1
        retention = total / baseline if baseline else 0.0
        report.delays.append(DelayResult(d, total, n, retention))

    # 3) leg decomposition at 1s
    entry_only = 0.0
    exit_only = 0.0
    for r in rows:
        ve = _price_position(series, r, 1.0, 0.0)
        vx = _price_position(series, r, 0.0, 1.0)
        if ve is not None:
            entry_only += ve
        if vx is not None:
            exit_only += vx
    report.entry_only_1s = entry_only
    report.exit_only_1s = exit_only

    # 4) price drift over 1s (diagnostic for which side leaks)
    e_drift: list[float] = []
    x_drift: list[float] = []
    for r in rows:
        e0 = _book_at(series, r.token_id, int(r.open_ts * 1000), "past")
        e1 = _book_at(series, r.token_id, int((r.open_ts + 1) * 1000), "future")
        x0 = _book_at(series, r.token_id, int(r.close_ts * 1000), "past")
        x1 = _book_at(series, r.token_id, int((r.close_ts + 1) * 1000), "future")
        if e0 and e1 and e0[2] is not None and e1[2] is not None:
            e_drift.append(e1[2] - e0[2])
        if x0 and x1 and x0[1] is not None and x1[1] is not None:
            x_drift.append(x1[1] - x0[1])
    report.entry_ask_drift_1s = statistics.mean(e_drift) if e_drift else None
    report.exit_bid_drift_1s = statistics.mean(x_drift) if x_drift else None

    # decide pass/fail at fail_delay
    fail_ret = next(
        (dr.retention for dr in report.delays if abs(dr.delay_s - fail_delay) < 1e-9),
        None,
    )
    report._passed = fail_ret is not None and fail_ret >= min_retention
    return report


def format_report(report: GateReport, fail_delay: float, min_retention: float) -> str:
    lines: list[str] = []
    lines.append(f"=== latency_gate (window={report.window}min) ===")
    lines.append(f"positions: {report.n_positions}")
    lines.append(f"shadow net PnL:        ${report.shadow_pnl:+.2f}")
    lines.append(
        f"same-snapshot rebuild: ${report.baseline_pnl:+.2f}  "
        f"(rebuild error ${report.rebuild_error:.2f})"
    )
    if report.shadow_pnl and report.rebuild_error / max(abs(report.shadow_pnl), 1e-9) > 0.02:
        lines.append("  WARNING: rebuild error >2% — re-pricing may be mis-aligned, treat results with care")
    lines.append("")
    lines.append("delay(s) | net PnL    | retention")
    for dr in report.delays:
        lines.append(f"{dr.delay_s:7.1f}  | ${dr.net_pnl:+9.2f} | {dr.retention * 100:5.0f}%")
    lines.append("")
    lines.append("leg decomposition @1s:")
    lines.append(f"  entry-only delay: ${report.entry_only_1s:+.2f}")
    lines.append(f"  exit-only  delay: ${report.exit_only_1s:+.2f}")
    if report.entry_ask_drift_1s is not None:
        lines.append(f"  best_ask drift +1s after entry: {report.entry_ask_drift_1s:+.5f} (>0 = buy gets worse)")
    if report.exit_bid_drift_1s is not None:
        lines.append(f"  best_bid drift +1s after exit:  {report.exit_bid_drift_1s:+.5f} (<0 = sell gets worse)")
    lines.append("")
    verdict = "PASS" if report.passed else "LOOK-AHEAD FAIL"
    lines.append(
        f"VERDICT: {verdict} "
        f"(retention @ {fail_delay:.1f}s must be >= {min_retention * 100:.0f}%)"
    )
    return "\n".join(lines)


def _parse_delays(raw: str) -> list[float]:
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(float(part))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delay-injection look-ahead gate for shadow positions.")
    parser.add_argument("--data-dir", required=True, help="Dir containing telemetry/ and ticks/ subdirs")
    parser.add_argument("--window-minutes", type=int, default=None, help="Filter to this market-window length (e.g. 15)")
    parser.add_argument("--delays", default="0.5,1,2,5", help="Comma-separated delay seconds to sweep")
    parser.add_argument("--fail-delay", type=float, default=1.0, help="Delay (s) at which the pass/fail threshold is checked")
    parser.add_argument("--min-retention", type=float, default=0.5, help="Min net-PnL retention to PASS at fail-delay")
    parser.add_argument("--json", action="store_true", help="Also emit machine-readable JSON to stdout")
    args = parser.parse_args(argv)

    telemetry_dir = os.path.join(args.data_dir, "telemetry")
    ticks_dir = os.path.join(args.data_dir, "ticks")
    if not os.path.isdir(telemetry_dir):
        print(f"ERROR: {telemetry_dir} not found", file=sys.stderr)
        return 2
    if not os.path.isdir(ticks_dir):
        print(f"ERROR: {ticks_dir} not found", file=sys.stderr)
        return 2

    delays = _parse_delays(args.delays)
    if args.fail_delay not in delays:
        delays.append(args.fail_delay)
        delays.sort()

    rows = load_positions(telemetry_dir, args.window_minutes)
    if not rows:
        print("ERROR: no matching positions found", file=sys.stderr)
        return 2
    tokens = {r.token_id for r in rows}
    series = load_tick_series(ticks_dir, tokens)

    report = run_gate(rows, series, delays, args.fail_delay, args.min_retention, args.window_minutes)
    print(format_report(report, args.fail_delay, args.min_retention))

    if args.json:
        payload = {
            "window": report.window,
            "n_positions": report.n_positions,
            "shadow_pnl": report.shadow_pnl,
            "baseline_pnl": report.baseline_pnl,
            "rebuild_error": report.rebuild_error,
            "passed": report.passed,
            "delays": [
                {"delay_s": dr.delay_s, "net_pnl": dr.net_pnl, "retention": dr.retention, "n": dr.n}
                for dr in report.delays
            ],
            "entry_only_1s": report.entry_only_1s,
            "exit_only_1s": report.exit_only_1s,
            "entry_ask_drift_1s": report.entry_ask_drift_1s,
            "exit_bid_drift_1s": report.exit_bid_drift_1s,
        }
        print(json.dumps(payload, ensure_ascii=False))

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
