"""Probe live Polymarket Gamma markets and print the fee-field distribution.

Run before flipping ARB_DRY_RUN=false to confirm the fee rate the bot will use
matches what's actually charged on-chain. Outputs:

  - feesEnabled true/false counts
  - fee rate distribution (resolved via models.resolve_polymarket_fee_rate)
  - feeSchedule shapes seen in the wild
  - any market whose resolved rate is >5% (suspicious)

Usage:
    python -m scripts.verify_polymarket_fees --limit 100
    python -m scripts.verify_polymarket_fees --condition-id 0xabc...
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

from polymarket_arb.models import resolve_polymarket_fee_rate


def fetch_markets(*, limit: int = 100, condition_id: str | None = None) -> list[dict]:
    base = "https://gamma-api.polymarket.com/markets"
    if condition_id:
        params = {"condition_ids": condition_id, "limit": 1}
    else:
        params = {"active": "true", "closed": "false", "limit": limit}
    url = f"{base}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-arb-fee-check"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--condition-id", default=None)
    parser.add_argument("--default-rate", type=float, default=0.02)
    parser.add_argument("--show-suspicious", action="store_true")
    args = parser.parse_args()

    markets = fetch_markets(limit=args.limit, condition_id=args.condition_id)
    if not markets:
        print("no markets returned", file=sys.stderr)
        return 1

    print(f"sampled {len(markets)} markets")

    enabled = Counter()
    schedule_shapes: Counter[str] = Counter()
    resolved_rates: Counter[float] = Counter()
    suspicious: list[tuple[str, float, dict]] = []

    for m in markets:
        enabled[bool(m.get("feesEnabled"))] += 1
        schedule = m.get("feeSchedule")
        schedule_shapes[json.dumps(schedule, sort_keys=True) if schedule else "<absent>"] += 1
        rate = resolve_polymarket_fee_rate(args.default_rate, market=m)
        resolved_rates[round(rate, 6)] += 1
        if rate > 0.05:
            suspicious.append((m.get("conditionId", m.get("condition_id", "?")), rate, m))

    print()
    print("feesEnabled distribution:")
    for k, v in sorted(enabled.items()):
        print(f"  {k!s:>5}: {v}")

    print()
    print("feeSchedule shapes:")
    for shape, count in schedule_shapes.most_common():
        print(f"  {count:>4}  {shape}")

    print()
    print("resolved rate distribution (decimal):")
    for rate, count in sorted(resolved_rates.items()):
        bps = rate * 10_000
        print(f"  {count:>4}  rate={rate:.6f}  ({bps:.1f} bps)")

    if suspicious and args.show_suspicious:
        print()
        print(f"suspicious markets (rate > 5%): {len(suspicious)}")
        for cid, rate, m in suspicious[:10]:
            print(f"  rate={rate:.4f}  cid={cid}  question={m.get('question', '')[:80]}")

    print()
    print("guidance:")
    if max(resolved_rates) <= 0.001:
        print("  most markets have feesEnabled=false → set POLYMARKET_TAKER_FEE_RATE=0 + LIVE_ALLOW_ZERO_TAKER_FEE=true")
    elif max(resolved_rates) <= 0.025:
        print("  resolved rates are at-or-below 2% → POLYMARKET_TAKER_FEE_RATE=0.02 is safe")
    else:
        max_rate = max(resolved_rates)
        print(f"  some markets resolve to {max_rate*100:.1f}% → set POLYMARKET_TAKER_FEE_RATE >= {max_rate:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
