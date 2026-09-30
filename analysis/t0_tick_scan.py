"""Stream the recorded tick flow and count genuine T0 structural arbs.

T0 fires when the full outcome set can be bought for less than $1 net of fees.
Four things make a naive ``sum(best_ask) < 1`` count useless, and each is a
separate filter stage below so the drop-off is visible:

* **Crossed books.** A stale/one-sided snapshot where ``best_bid > best_ask``
  produces a fake sub-$1 sum. Those legs are unfillable.
* **Stitched legs.** Holding "latest snapshot per token" and summing across
  tokens joins books recorded seconds apart. A leg that moved 30s ago against a
  leg quoted now is not a tradeable pair — it is a resampling artifact, and it
  is the single biggest source of phantom T0 signal in this dataset.
* **Depth.** The touch may hold 3 shares. Cost is a VWAP walk down
  ``asks_top3`` for the target size, and a leg that cannot fill it is out.
* **Fee.** The archived crypto UPDOWN markets carry ``feeSchedule.rate = 0.07``
  (taker-only); fee is ``rate * p * (1-p)`` per share. Both a zero-fee bound and
  the real rate are reported, so a "dead even at zero fee" result is unambiguous.

Usage:
    python analysis/t0_tick_scan.py --ticks-dir <DATA_ROOT>/6.20-6.26/data/ticks
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict


def vwap(levels, size: float):
    if not isinstance(levels, list) or size <= 0:
        return None
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
    if filled < size - 1e-9:
        return None  # not enough depth for the target size
    return cost / filled


def _chunk_order(path: str) -> tuple[str, int]:
    """Chronological order for rolled tick files.

    The recorder rolls ``2026-06-20.ndjson`` -> ``2026-06-20.1.ndjson`` -> ...,
    so plain filename sort puts the *second* chunk first and rewinds the stream
    by hours. That makes the cross-leg age check compare a book against a leg
    recorded in the future and lets stale pairs through the freshness filter.
    """
    name = os.path.basename(path)
    stem = name[: -len(".ndjson")] if name.endswith(".ndjson") else name
    day, _, chunk = stem.partition(".")
    return day, int(chunk) if chunk.isdigit() else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks-dir", required=True)
    ap.add_argument("--size", type=float, default=20.0, help="target shares per leg")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--min-edge-usd", type=float, default=0.01)
    ap.add_argument("--max-leg-age-ms", type=int, default=200,
                    help="max timestamp spread across the legs of one condition")
    ap.add_argument("--progress-every", type=int, default=2_000_000)
    args = ap.parse_args()

    # condition_id -> token_id -> (ts_ms, best_bid, best_ask, asks_top3)
    books: dict[str, dict[str, tuple]] = defaultdict(dict)
    rows = 0
    raw_hits = clean_hits = fresh_hits = depth_hits = 0
    net_free_hits = net_fee_hits = 0
    best_net_free = best_net_fee = -9.99
    age_hist: dict[str, int] = defaultdict(int)
    examples: list[tuple] = []
    fresh_dump: list[tuple] = []

    for path in sorted(glob.glob(os.path.join(args.ticks_dir, "*.ndjson")),
                       key=_chunk_order):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                if '"book"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("event_type") != "book":
                    continue
                rows += 1
                cond = d.get("condition_id")
                tok = d.get("token_id")
                if not cond or not tok:
                    continue
                ts = int(d.get("ts_ms") or 0)
                legs = books[cond]
                legs[tok] = (ts, d.get("best_bid"), d.get("best_ask"), d.get("asks_top3"))
                if len(legs) < 2:
                    continue
                vals = list(legs.values())
                asks = [v[2] for v in vals]
                if any(a is None for a in asks):
                    continue
                if sum(float(a) for a in asks) >= 1.0:
                    continue
                raw_hits += 1

                if any(v[1] is not None and v[2] is not None and float(v[1]) > float(v[2])
                       for v in vals):
                    continue
                clean_hits += 1

                age = ts - min(v[0] for v in vals)
                bucket = ("<=0.2s" if age <= 200 else "<=1s" if age <= 1000
                          else "<=10s" if age <= 10_000 else ">10s")
                age_hist[bucket] += 1
                if age > args.max_leg_age_ms:
                    continue
                # A leg quoted with no bid at all is a one-sided transient, not
                # a book we could have lifted; its "ask" is meaningless.
                if any(v[1] is None or float(v[1]) <= 0 for v in vals):
                    continue
                fresh_hits += 1
                if len(fresh_dump) < 40:
                    fresh_dump.append((ts, cond, d.get("question"),
                                       [(v[0], v[1], v[2], v[3]) for v in vals]))

                costs = [vwap(v[3], args.size) for v in vals]
                if any(c is None for c in costs):
                    continue
                depth_hits += 1

                gross = (1.0 - sum(costs)) * args.size
                if gross >= args.min_edge_usd:
                    net_free_hits += 1
                    best_net_free = max(best_net_free, gross)
                fee = sum(args.fee_rate * c * (1.0 - c) * args.size for c in costs)
                net = gross - fee
                best_net_fee = max(best_net_fee, net)
                if net >= args.min_edge_usd:
                    net_fee_hits += 1
                    if len(examples) < 5:
                        examples.append((ts, cond, round(sum(costs), 4), round(net, 4)))

                if rows % args.progress_every < 2:
                    print(f"  ..{rows} rows raw={raw_hits} clean={clean_hits} "
                          f"fresh={fresh_hits} depth={depth_hits} "
                          f"net0={net_free_hits} netfee={net_fee_hits}", flush=True)

    print("\n=== T0 structural arb scan ===")
    print(f"book rows scanned                 : {rows}")
    print(f"conditions seen                   : {len(books)}")
    print(f"raw  sum(best_ask) < 1            : {raw_hits}")
    print(f"  + no crossed leg                : {clean_hits}")
    print(f"  + legs within {args.max_leg_age_ms}ms of each other : {fresh_hits}")
    print(f"  + {args.size:.0f} shares of depth per leg   : {depth_hits}")
    print(f"  + gross >= ${args.min_edge_usd} at ZERO fee     : {net_free_hits} "
          f"(best ${best_net_free:.4f})")
    print(f"  + net   >= ${args.min_edge_usd} at fee {args.fee_rate}   : {net_fee_hits} "
          f"(best ${best_net_fee:.4f})")
    print("leg-age spread among non-crossed sub-$1 instants:")
    for k in ("<=0.2s", "<=1s", "<=10s", ">10s"):
        print(f"   {k:>7}: {age_hist.get(k, 0)}")
    if examples:
        print("examples (ts_ms, condition, sum_vwap_ask, net_usd):")
        for ex in examples:
            print("  ", ex)
    if fresh_dump:
        print("\nfull leg detail for every surviving fresh candidate:")
        for ts, cond, q, legs in fresh_dump:
            print(f"  ts={ts} {cond[:14]} {str(q)[:44]}")
            for lts, bb, ba, asks in legs:
                print(f"     age={ts-lts}ms bid={bb} ask={ba} asks={asks}")


if __name__ == "__main__":
    main()
