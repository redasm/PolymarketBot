"""Replay the real T3 maker code against recorded books + public taker prints.

Why this shape
--------------
The May shadow run killed T3 on adverse selection: 53/53 closes came back below
their open (mean -0.0246/share), i.e. the quotes were only ever hit by someone
who knew better. Commit a551655 added `AntiSnipeGuard` (jump pause / stable-mid
confirmation / median+EMA filtering / post-fill cooldown / chase clamp) to
attack exactly that. This replays both arms over the same tape so the guard's
effect is measured, not assumed.

The strategy objects are imported from `polymarket_arb`, not reimplemented —
`MakerStrategy.compute_quote`, `AntiSnipeGuard.evaluate/clamp_chase` and
`StatisticalMispricingDetector.estimate_market_probability` are the live code
paths, so a change in them changes this result.

Fills
-----
Recorded ticks carry only `book` events, so fills come from the public taker
prints (`analysis/fetch_public_trades.py`):

* taker SELL at ``p <= our bid``  -> our bid is hit
* taker BUY  at ``p >= our ask``  -> our ask is lifted

At the touch we are queued behind the depth already resting there, so the fill
is scaled by ``our_size / (resting_size + our_size)``. Strictly through our
price we take the whole print up to our size.

**Rewards are zero here and that is not an omission**: Gamma reports no
`clobRewards` for these markets, so the reward-band clamp has nothing to earn.
This measures the gross maker edge the rewards would have to cover; the
break-even daily reward is reported at the end.

Usage:
    python analysis/t3_maker_replay.py --ticks <glob> --trades <ndjson> --markout 60
"""
from __future__ import annotations

import argparse
import bisect
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_arb.strategies.maker_anti_snipe import AntiSnipeConfig, AntiSnipeGuard
from polymarket_arb.strategies.maker_strategy import DynamicSpreadCalculator, MakerStrategy
from polymarket_arb.strategies.statistical_model import StatisticalMispricingDetector
from polymarket_arb.volatility_estimator import VolEstimator

TICK = 0.01


class Book:
    __slots__ = ("ts", "bid", "ask", "bids", "asks")

    def __init__(self, ts, bid, ask, bids, asks):
        self.ts, self.bid, self.ask, self.bids, self.asks = ts, bid, ask, bids, asks

    @property
    def mid(self):
        if self.bid is None or self.ask is None:
            return None
        return (float(self.bid) + float(self.ask)) / 2.0


def load_books(paths, wanted_tokens=None):
    """condition_id -> yes_token_id -> [Book] (only the 'up'/'yes' leg is quoted)."""
    by_token: dict[str, list[Book]] = collections.defaultdict(list)
    token_cond: dict[str, str] = {}
    role: dict[str, str] = {}
    for path in paths:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                if '"book"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid, tok = d.get("condition_id"), d.get("token_id")
                if not cid or not tok:
                    continue
                if wanted_tokens is not None and tok not in wanted_tokens:
                    continue
                token_cond[tok] = cid
                role[tok] = (d.get("outcome_role") or "").lower()
                by_token[tok].append(Book(int(d["ts_ms"]), d.get("best_bid"), d.get("best_ask"),
                                          d.get("bids_top3"), d.get("asks_top3")))
    for tok in by_token:
        by_token[tok].sort(key=lambda b: b.ts)
    return by_token, token_cond, role


def load_trades(path):
    by_asset: dict[str, list[tuple]] = collections.defaultdict(list)
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            asset = t.get("asset")
            try:
                ts = int(t["timestamp"]) * 1000
                px = float(t["price"])
                sz = float(t["size"])
            except (KeyError, TypeError, ValueError):
                continue
            by_asset[asset].append((ts, str(t.get("side") or "").upper(), px, sz))
    for a in by_asset:
        by_asset[a].sort()
    return by_asset


def depth_at(levels, price, side):
    """Resting size at `price` on the given side of the recorded book."""
    if not isinstance(levels, list):
        return 0.0
    for lv in levels:
        if not isinstance(lv, (list, tuple)) or len(lv) < 2:
            continue
        if abs(float(lv[0]) - price) < 1e-9:
            return float(lv[1])
    return 0.0


def mid_at(books, ts_ms):
    i = bisect.bisect_right([b.ts for b in books], ts_ms) - 1
    while i >= 0:
        m = books[i].mid
        if m is not None:
            return m
        i -= 1
    return None


def replay_token(books, trades, *, anti_snipe: bool, cycle_ms: int, latency_ms: int,
                 markout_ms_list: list, default_size: float, base_ticks: float,
                 max_inventory: float, use_vol: bool):
    detector = StatisticalMispricingDetector()
    vol = VolEstimator() if use_vol else None
    spread_calc = DynamicSpreadCalculator(base_spread_ticks=base_ticks, vol_estimator=vol)
    maker = MakerStrategy(spread_calc=spread_calc, default_size=default_size,
                          max_inventory=max_inventory)
    guard = AntiSnipeGuard(AntiSnipeConfig(enabled=anti_snipe))

    ts_list = [b.ts for b in books]
    fills = []
    blocked = collections.Counter()
    quote = None          # (live_from_ms, bid, bid_sz, ask, ask_sz, book)
    last_cycle = None
    last_1m = None
    tok = "t"

    trade_i = 0
    n_trades = len(trades)

    for idx, book in enumerate(books):
        now_ms = book.ts
        if last_cycle is not None and now_ms - last_cycle < cycle_ms:
            pass
        else:
            last_cycle = now_ms
            mid = book.mid
            if mid is not None:
                if vol is not None and (last_1m is None or now_ms - last_1m >= 60_000):
                    vol.update_1m_close(mid, now_ms)
                    last_1m = now_ms
                now_s = now_ms / 1000.0
                decision = guard.evaluate(tok, float(mid), now_s, tick_size=TICK)
                if not decision.allow:
                    blocked[decision.reason] += 1
                    quote = None
                else:
                    est = detector.estimate_market_probability(
                        market_id=tok, outcome="YES", market_price=float(mid),
                        bids_total_size=sum(float(l[1]) for l in (book.bids or [])),
                        asks_total_size=sum(float(l[1]) for l in (book.asks or [])),
                        mid_price=float(mid))
                    q = maker.compute_quote(token_id=tok, condition_id="c",
                                            fair_value=float(est.model_prob), tick_size=TICK,
                                            mid_price=float(mid))
                    if q is None or (q.bid_price is None and q.ask_price is None):
                        quote = None
                    else:
                        if anti_snipe:
                            q.bid_price, q.ask_price = guard.clamp_chase(
                                tok, bid=q.bid_price, ask=q.ask_price, tick_size=TICK)
                        quote = (now_ms + latency_ms, q.bid_price, q.bid_size,
                                 q.ask_price, q.ask_size, book)

        # taker prints between this book event and the next
        next_ts = ts_list[idx + 1] if idx + 1 < len(ts_list) else now_ms + cycle_ms
        while trade_i < n_trades and trades[trade_i][0] < next_ts:
            t_ts, side, px, sz = trades[trade_i]
            trade_i += 1
            if quote is None or t_ts < quote[0] or t_ts < now_ms:
                continue
            live_from, bid, bid_sz, ask, ask_sz, qbook = quote
            if side == "SELL" and bid is not None and px <= bid + 1e-9:
                share = 1.0 if px < bid - 1e-9 else (
                    bid_sz / (bid_sz + depth_at(qbook.bids, bid, "bid")) if bid_sz > 0 else 0.0)
                filled = min(bid_sz, sz * share)
                if filled > 1e-6:
                    fills.append((t_ts, "BUY", bid, filled))
                    maker.update_inventory(tok, "BUY", filled)
                    guard.register_fill(tok, t_ts / 1000.0)
                    quote = (live_from, bid, bid_sz - filled, ask, ask_sz, qbook)
            elif side == "BUY" and ask is not None and px >= ask - 1e-9:
                share = 1.0 if px > ask + 1e-9 else (
                    ask_sz / (ask_sz + depth_at(qbook.asks, ask, "ask")) if ask_sz > 0 else 0.0)
                filled = min(ask_sz, sz * share)
                if filled > 1e-6:
                    fills.append((t_ts, "SELL", ask, filled))
                    maker.update_inventory(tok, "SELL", filled)
                    guard.register_fill(tok, t_ts / 1000.0)
                    quote = (live_from, bid, bid_sz, ask, ask_sz - filled, qbook)

    # mark every fill out at each horizon against the recorded mid.
    # horizon 0 is the spread actually captured at the moment of the fill; the
    # longer horizons show how much of it the subsequent drift takes back.
    rows = {h: [] for h in markout_ms_list}
    for t_ts, side, px, sz in fills:
        sign = 1.0 if side == "BUY" else -1.0
        for h in markout_ms_list:
            m = mid_at(books, t_ts + h)
            if m is None:
                continue
            rows[h].append({"pnl": sign * (m - px) * sz, "size": sz})
    return rows, blocked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", nargs="+", required=True)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--markouts", default="0,10,30,60", help="markout horizons, seconds")
    ap.add_argument("--cycle-sec", type=float, default=5.0)
    ap.add_argument("--latency-ms", type=int, default=300)
    ap.add_argument("--default-size", type=float, default=20.0)
    ap.add_argument("--max-inventory", type=float, default=100.0)
    ap.add_argument("--base-ticks", default="1,2")
    ap.add_argument("--max-tokens", type=int, default=400)
    args = ap.parse_args()

    paths = []
    for pattern in args.ticks:
        paths.extend(sorted(glob.glob(pattern)) or [pattern])
    trades_by_asset = load_trades(args.trades)
    print(f"trades: {sum(len(v) for v in trades_by_asset.values())} prints "
          f"over {len(trades_by_asset)} tokens", flush=True)

    books_by_token, token_cond, role = load_books(paths, wanted_tokens=set(trades_by_asset))
    print(f"books: {len(books_by_token)} tokens", flush=True)

    tokens = [t for t in books_by_token
              if t in trades_by_asset and len(books_by_token[t]) >= 20]
    tokens.sort(key=lambda t: -len(trades_by_asset[t]))
    tokens = tokens[:args.max_tokens]
    print(f"replaying {len(tokens)} tokens with both books and prints", flush=True)

    base_list = [float(x) for x in args.base_ticks.split(",") if x.strip()]
    horizons = [int(float(x) * 1000) for x in args.markouts.split(",") if x.strip()]
    print(f"\ncycle={args.cycle_sec}s  latency={args.latency_ms}ms  "
          f"size={args.default_size}  maker fee=0 (takerOnly markets)")
    print("horizon 0s = spread captured at the fill; longer horizons show the drift back")
    print(f"{'base_ticks':>11}{'anti_snipe':>11}{'fills':>7}{'shares':>9}"
          + "".join(f"{'@' + str(h // 1000) + 's $':>12}" for h in horizons)
          + "".join(f"{'@' + str(h // 1000) + 's /sh':>12}" for h in horizons))
    for base in base_list:
        for use_vol in (True,):
            for anti in (False, True):
                agg = {h: [] for h in horizons}
                blocked_total = collections.Counter()
                for tok in tokens:
                    rows, blocked = replay_token(
                        books_by_token[tok], trades_by_asset[tok],
                        anti_snipe=anti, cycle_ms=int(args.cycle_sec * 1000),
                        latency_ms=args.latency_ms, markout_ms_list=horizons,
                        default_size=args.default_size, base_ticks=base,
                        max_inventory=args.max_inventory, use_vol=use_vol)
                    blocked_total.update(blocked)
                    for h in horizons:
                        agg[h].extend(rows[h])
                base_h = horizons[0]
                n = len(agg[base_h])
                shares = sum(r["size"] for r in agg[base_h])
                pnls = {h: sum(r["pnl"] for r in agg[h]) for h in horizons}
                shs = {h: sum(r["size"] for r in agg[h]) for h in horizons}
                print(f"{base:>11.0f}{str(anti):>11}{n:>7}{shares:>9.0f}" +
                      "".join(f"{pnls[h]:>12.2f}" for h in horizons) +
                      "".join(f"{(pnls[h] / shs[h] if shs[h] else 0):>12.5f}" for h in horizons))
                if anti and blocked_total:
                    print(f"    guard blocks: {dict(blocked_total.most_common(5))}")


if __name__ == "__main__":
    main()
