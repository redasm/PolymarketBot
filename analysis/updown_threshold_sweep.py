"""Does raising T2_MIN_DEVIATION rescue UPDOWN once the real 0.07 fee applies?

`updown_realism.py` shows the recorded 6.20-6.26 shadow book nets ~0 at the
true taker rate. The obvious next question is whether the *model* has an edge
at all, or whether only the fee threshold is mis-set: if net PnL per position
climbs monotonically with the entry deviation, the fix is a higher bar; if it
stays flat/negative at every bar, the signal itself carries no edge.

`positions_lifecycle` rows carry an empty `signal_id` (telemetry gap), so the
entry deviation is recovered by joining `strategy_signals` on
(token_id, nearest signal ts <= open_ts).

Usage:
    python analysis/updown_threshold_sweep.py \
        --data-dir E:/PolymarketData/6.20-6.26/data --cache <pickle from updown_realism>
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.updown_realism import Pos, load_positions, load_ticks, simulate

MATCH_TOLERANCE_S = 3.0


def load_signal_devs(telemetry_dir: str) -> dict[str, list[tuple[float, float, float]]]:
    """token_id -> sorted [(ts, deviation, confidence)]."""
    out: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(telemetry_dir, "*.strategy_signals.ndjson"))):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = r.get("payload") or {}
                tok = payload.get("token_id")
                dev = payload.get("deviation")
                if not tok or dev is None:
                    continue
                try:
                    ts = datetime.fromisoformat(str(r["ts"])).timestamp()
                except (KeyError, ValueError):
                    continue
                out[str(tok)].append((ts, float(dev), float(r.get("confidence") or 0.0)))
    for tok in out:
        out[tok].sort()
    return dict(out)


def attach_dev(positions: list[Pos], devs) -> dict[int, tuple[float, float]]:
    """id(pos) -> (deviation, confidence) for positions we can match."""
    matched: dict[int, tuple[float, float]] = {}
    for p in positions:
        series = devs.get(p.token_id)
        if not series:
            continue
        i = bisect.bisect_right(series, (p.open_ts, float("inf"), float("inf"))) - 1
        if i < 0:
            continue
        ts, dev, conf = series[i]
        if p.open_ts - ts > MATCH_TOLERANCE_S:
            continue
        matched[id(p)] = (dev, conf)
    return matched


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--rate", type=float, default=0.07)
    ap.add_argument("--window", type=int, default=5)
    args = ap.parse_args()

    tele = os.path.join(args.data_dir, "telemetry")
    positions = [p for p in load_positions(tele) if p.window == args.window]
    devs = load_signal_devs(tele)
    matched = attach_dev(positions, devs)
    print(f"positions(window={args.window}m)={len(positions)} matched_to_signal={len(matched)}")

    series = load_ticks(os.path.join(args.data_dir, "ticks"), {}, args.cache)

    print(f"\ndelay={args.delay}s  fee_rate={args.rate}  fill=limit(FOK at decision price)")
    print(f"{'min_dev':>9}{'n':>7}{'fills':>7}{'net PnL':>11}{'per pos':>9}"
          f"{'fees':>10}{'gross':>10}{'win%':>7}")
    for min_dev in (0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25):
        subset = [p for p in positions
                  if id(p) in matched and matched[id(p)][0] >= min_dev]
        if not subset:
            continue
        a = simulate(subset, series, delay_s=args.delay, rate=args.rate, mode="limit")
        per = a.pnl / a.filled if a.filled else 0.0
        gross = a.pnl + a.fees
        wr = a.wins / a.filled if a.filled else 0.0
        print(f"{min_dev:>9.2f}{len(subset):>7}{a.filled:>7}{a.pnl:>11.2f}{per:>9.3f}"
              f"{a.fees:>10.2f}{gross:>10.2f}{wr:>7.1%}")

    # Fee-free control: is there any gross edge before costs?
    print(f"\nfee=0 control (same fills, isolates whether the model has raw edge)")
    print(f"{'min_dev':>9}{'fills':>7}{'net PnL':>11}{'per pos':>9}{'win%':>7}")
    for min_dev in (0.05, 0.08, 0.10, 0.15, 0.20):
        subset = [p for p in positions
                  if id(p) in matched and matched[id(p)][0] >= min_dev]
        if not subset:
            continue
        a = simulate(subset, series, delay_s=args.delay, rate=0.0, mode="limit")
        per = a.pnl / a.filled if a.filled else 0.0
        wr = a.wins / a.filled if a.filled else 0.0
        print(f"{min_dev:>9.2f}{a.filled:>7}{a.pnl:>11.2f}{per:>9.3f}{wr:>7.1%}")


if __name__ == "__main__":
    main()
