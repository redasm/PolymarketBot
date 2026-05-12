"""Shadow Mode daily report — roadmap §三-阶段 1 exit-criteria gate.

Reads ``data/telemetry/<date>.virtual_fills.ndjson`` and prints the
per-tier statistics operators need to decide whether the shadow run
has cleared the bar for stepping up to a $200–500 live verification:

- maker / taker ratio
- fill rate (filled vs pending vs failed)
- per-fill expected PnL (price - intended, net of fees)
- slippage distribution
- per-market top-N concentration

Usage::

    python scripts/shadow_daily_report.py                  # today (UTC)
    python scripts/shadow_daily_report.py --date 2026-05-12
    python scripts/shadow_daily_report.py --since 2026-05-10 --until 2026-05-12

The script is read-only and never re-runs the bot. It works directly
off the NDJSON stream so it can be cron'd or wired into a CI gate.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _iter_dates(since: str, until: str) -> Iterable[str]:
    start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    stop = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    cur = start
    while cur <= stop:
        yield cur.strftime("%Y-%m-%d")
        cur = cur + timedelta(days=1)


def _load_rows(telemetry_dir: Path, dates: list[str]) -> list[dict]:
    rows: list[dict] = []
    for date in dates:
        path = telemetry_dir / f"{date}.virtual_fills.ndjson"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[idx]


def _row_filled_size(row: dict) -> float:
    """Return the actually-filled size in a virtual_fill row.

    A row with ``status="partial"`` is real money — the maker quote
    crossed but only part of the visible opposing depth was taken.
    Earlier versions of this report keyed everything off
    ``status == "filled"`` and silently dropped partials from
    fee/notional/slippage totals; that under-reported actual fills
    and over-counted the partial as "not filled" in fill_rate.
    """
    result = row.get("result") or {}
    size = result.get("filled_size")
    try:
        return float(size) if size is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _row_has_any_fill(row: dict) -> bool:
    """A row counts as "any fill" when status indicates the order
    actually touched the book *and* a non-zero size traded. We
    accept both ``filled`` (complete) and ``partial`` (some size)
    so reports cover the full economic activity of the shadow run.
    """
    status = (row.get("result") or {}).get("status")
    if status not in {"filled", "partial"}:
        return False
    return _row_filled_size(row) > 0.0


def _summarize_per_tier(rows: list[dict]) -> dict[str, dict]:
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_tier[str(row.get("tier") or "UNKNOWN")].append(row)

    out: dict[str, dict] = {}
    for tier, tier_rows in sorted(by_tier.items()):
        total = len(tier_rows)
        # Filled = order completed; partial = order crossed but
        # opposing depth ran out (real fill, smaller than requested).
        # any_filled aggregates both so fee / notional / slippage
        # reflect the entire shadow PnL surface.
        filled = [r for r in tier_rows if (r.get("result") or {}).get("status") == "filled"]
        partial = [
            r for r in tier_rows
            if (r.get("result") or {}).get("status") == "partial"
            and _row_filled_size(r) > 0.0
        ]
        pending = [r for r in tier_rows if (r.get("result") or {}).get("status") == "pending"]
        failed = [r for r in tier_rows if (r.get("result") or {}).get("status") in {"failed", "cancelled"}]
        any_filled = filled + partial
        maker = [r for r in tier_rows if bool(r.get("is_maker"))]

        slippages = [float(r.get("slippage") or 0.0) for r in any_filled]
        fees = [float(r.get("fee") or 0.0) for r in any_filled]
        notional_fills = [
            _row_filled_size(r)
            * float((r.get("result") or {}).get("avg_fill_price") or 0.0)
            for r in any_filled
        ]

        out[tier] = {
            "total": total,
            "filled": len(filled),
            "partial": len(partial),
            "any_filled": len(any_filled),
            "pending": len(pending),
            "failed": len(failed),
            # ``complete_fill_rate`` answers "what fraction of
            # submitted orders fully filled?" and ``any_fill_rate``
            # answers "what fraction got at least some fill?". The
            # legacy ``fill_rate`` key is kept as an alias of
            # complete_fill_rate for callers that grep the old name.
            "complete_fill_rate": (len(filled) / total) if total else 0.0,
            "any_fill_rate": (len(any_filled) / total) if total else 0.0,
            "fill_rate": (len(filled) / total) if total else 0.0,
            "maker_ratio": (len(maker) / total) if total else 0.0,
            "total_notional_filled": round(sum(notional_fills), 4),
            "total_fee": round(sum(fees), 4),
            "slippage": {
                "count": len(slippages),
                "mean": round(statistics.fmean(slippages), 6) if slippages else None,
                "stdev": round(statistics.pstdev(slippages), 6) if len(slippages) > 1 else None,
                "p50": _percentile(slippages, 50),
                "p95": _percentile(slippages, 95),
                "max": max(slippages) if slippages else None,
                "min": min(slippages) if slippages else None,
            },
        }
    return out


def _top_markets(rows: list[dict], top_n: int = 10) -> list[dict]:
    """Rank markets by actual filled count (and notional) — not by
    submitted-quote count. Without this filter a market the maker
    keeps quoting on but never filling would dominate the ranking
    and mislead operators sizing T3 concentration.
    """
    counts: Counter[str] = Counter()
    notionals: dict[str, float] = defaultdict(float)
    for row in rows:
        if not _row_has_any_fill(row):
            continue
        market_id = str(row.get("market_id") or "")
        if not market_id:
            continue
        counts[market_id] += 1
        result = row.get("result") or {}
        try:
            price = float(result.get("avg_fill_price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        notionals[market_id] += _row_filled_size(row) * price
    return [
        {
            "market_id": market_id,
            "fills": count,
            "notional": round(notionals[market_id], 4),
        }
        for market_id, count in counts.most_common(top_n)
    ]


def _overall(rows: list[dict]) -> dict:
    filled = [r for r in rows if (r.get("result") or {}).get("status") == "filled"]
    partial = [
        r for r in rows
        if (r.get("result") or {}).get("status") == "partial"
        and _row_filled_size(r) > 0.0
    ]
    any_filled = filled + partial
    maker_filled = [r for r in any_filled if bool(r.get("is_maker"))]
    return {
        "rows": len(rows),
        "filled": len(filled),
        "partial": len(partial),
        "any_filled": len(any_filled),
        "maker_filled": len(maker_filled),
        "taker_filled": len(any_filled) - len(maker_filled),
        "maker_ratio_of_filled": (len(maker_filled) / len(any_filled)) if any_filled else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow Mode daily fill report")
    parser.add_argument("--telemetry-dir", default="data/telemetry")
    parser.add_argument("--date", help="single UTC date, YYYY-MM-DD (default: today)")
    parser.add_argument("--since", help="start UTC date, YYYY-MM-DD (inclusive)")
    parser.add_argument("--until", help="end UTC date, YYYY-MM-DD (inclusive)")
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args()

    if args.since and args.until:
        dates = list(_iter_dates(args.since, args.until))
    else:
        date = args.date or _utc_today()
        dates = [date]

    telemetry_dir = Path(args.telemetry_dir)
    if not telemetry_dir.is_absolute():
        telemetry_dir = PROJECT_ROOT / telemetry_dir

    rows = _load_rows(telemetry_dir, dates)
    payload = {
        "dates": dates,
        "telemetry_dir": str(telemetry_dir),
        "overall": _overall(rows),
        "by_tier": _summarize_per_tier(rows),
        "top_markets_by_fills": _top_markets(rows, top_n=args.top_n),
    }

    if not rows:
        payload["warning"] = (
            "no virtual_fills rows found — bot may not be in shadow mode, "
            "or telemetry directory is wrong"
        )

    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
