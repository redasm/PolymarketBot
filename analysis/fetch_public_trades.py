"""Pull public taker prints for the condition_ids present in a recorded tick day.

`data-api.polymarket.com/trades` is open (no key) and still serves closed
markets, which is the piece the recorded tick flow is missing: the ticks carry
only `book` events, so a maker fill can otherwise only be guessed at from book
deltas — and a book delta is indistinguishable between "someone traded through
my level" and "someone cancelled".

Output: one NDJSON per run, `{condition_id, side, price, size, timestamp, asset}`.
`side` is the taker's side, which is what decides whether a resting bid or a
resting ask would have been hit.

Usage:
    python analysis/fetch_public_trades.py \
        --ticks E:/PolymarketData/6.20-6.26/data/ticks/2026-06-23.ndjson \
        --out <dir>/trades_2026-06-23.ndjson
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time

import requests

DATA_API = "https://data-api.polymarket.com/trades"
PAGE = 500


def conditions_in_ticks(paths: list[str], max_conditions: int | None) -> dict[str, tuple[int, int]]:
    """condition_id -> (min_ts_ms, max_ts_ms) over the recorded window."""
    span: dict[str, list[int]] = {}
    for path in paths:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                if '"book"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = d.get("condition_id")
                ts = int(d.get("ts_ms") or 0)
                if not cid or not ts:
                    continue
                cur = span.get(cid)
                if cur is None:
                    span[cid] = [ts, ts]
                else:
                    if ts < cur[0]:
                        cur[0] = ts
                    if ts > cur[1]:
                        cur[1] = ts
    out = {k: (v[0], v[1]) for k, v in span.items()}
    if max_conditions and len(out) > max_conditions:
        # Keep the longest-observed conditions — they are the ones with enough
        # book history for a maker replay to say anything.
        ordered = sorted(out.items(), key=lambda kv: kv[1][0] - kv[1][1])
        out = dict(ordered[:max_conditions])
    return out


def fetch_condition(session: requests.Session, cid: str, max_pages: int) -> list[dict]:
    rows: list[dict] = []
    for page in range(max_pages):
        params = {"market": cid, "limit": PAGE, "offset": page * PAGE}
        for attempt in range(3):
            try:
                r = session.get(DATA_API, params=params, timeout=30)
                if r.status_code == 429:
                    time.sleep(2 + attempt * 3)
                    continue
                r.raise_for_status()
                batch = r.json()
                break
            except Exception:  # noqa: BLE001 - one bad market must not kill the pull
                time.sleep(0.5 + attempt)
                batch = None
        if not batch or not isinstance(batch, list):
            break
        rows.extend(batch)
        if len(batch) < PAGE:
            break
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", nargs="+", required=True, help="tick ndjson file(s) or globs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-conditions", type=int, default=None)
    ap.add_argument("--max-pages", type=int, default=6)
    ap.add_argument("--sleep", type=float, default=0.05)
    args = ap.parse_args()

    paths: list[str] = []
    for pattern in args.ticks:
        paths.extend(sorted(glob.glob(pattern)) or [pattern])
    print(f"scanning {len(paths)} tick file(s) for condition_ids", flush=True)
    spans = conditions_in_ticks(paths, args.max_conditions)
    print(f"  {len(spans)} conditions", flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    total = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, cid in enumerate(spans, 1):
            rows = fetch_condition(session, cid, args.max_pages)
            for t in rows:
                fh.write(json.dumps({
                    "condition_id": cid,
                    "asset": t.get("asset"),
                    "side": t.get("side"),
                    "price": t.get("price"),
                    "size": t.get("size"),
                    "timestamp": t.get("timestamp"),
                }, ensure_ascii=False) + "\n")
            total += len(rows)
            time.sleep(args.sleep)
            if i % 50 == 0:
                print(f"  {i}/{len(spans)} conditions, {total} trades", flush=True)
    print(f"wrote {total} trades -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
