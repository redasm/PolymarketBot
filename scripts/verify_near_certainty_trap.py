"""Empirical check: does Polymarket systematically misprice 92-98¢ binary contracts?

STATUS (2026-05-22): BLOCKED ON DATA AVAILABILITY.

We probed three candidate data sources for the YES-token price history
of *resolved* markets — needed to compute "did this market touch 0.92
at any point in its lifetime" and pair that with the realised
resolution. All three are inadequate:

  1. **CLOB `/prices-history`** (https://clob.polymarket.com): returns
     {history:[]} for resolved tokens. Confirmed against the Trump 2024
     YES token (volume $1.5B, definitely traded above 0.92) — empty.
     Only active markets retain history here.

  2. **data-api.polymarket.com `/trades`**: filter params (market,
     asset, tokenId, conditionId, …) are silently ignored; returns
     global feed. Effectively unusable for per-market history queries.

  3. **Goldsky subgraph orderbook-subgraph/prod**: schema includes
     OrderFilledEvent with maker/taker amounts (price reconstructable),
     but `orderBy: timestamp desc` times out for heavy markets and
     returns empty for older markets (pre-2022 not indexed).

Implication
-----------
Article 4's central claim (realised YES rate among 92-98¢ markets is
materially below 92%) cannot be verified by free public APIs as of the
above date. Verification would require either:

  - A paid Dune/Goldsky tier with no statement-timeout for ad-hoc
    queries over the full orderFilledEvent table;
  - Bot tick history accumulated over months (only 2 days currently);
  - The Polymarket team publishing historical OHLC for resolved markets.

Decision applied
----------------
Stage C (`NearCertaintyRule`) is implemented in **shadow-mode-only**:
it logs *what it would have done* on each signal, but does not modify
production size/confidence. Once the bot accumulates enough live
samples of high-price entries, we re-evaluate from those records.

This script is kept in the repo as documentation of the verification
attempt — re-running it after fresh APIs become available is a single
command. The original methodology (now mostly dead code) is preserved
below for future re-runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

GAMMA_HOST = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com").rstrip("/")
CLOB_HOST = os.environ.get("CLOB_HOST", "https://clob.polymarket.com").rstrip("/")

CACHE_DIR = Path("data/research")
CACHE_FILE = CACHE_DIR / "near_certainty_cache.json"
REPORT_FILE = CACHE_DIR / "near_certainty_verification.json"

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
LOG = logging.getLogger("near_certainty")


# ---------------------------------------------------------------------------
# Polymarket API helpers
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "PolymarketBot/verify_near_certainty"})
    return s


def fetch_resolved_binary_markets(
    session: requests.Session, *, max_markets: int
) -> list[dict[str, Any]]:
    """Stream resolved binary markets from gamma-api in pages of 100."""
    markets: list[dict[str, Any]] = []
    offset = 0
    while len(markets) < max_markets:
        params = {
            "closed": "true",
            "limit": 100,
            "offset": offset,
            "order": "endDate",
            "ascending": "false",
        }
        try:
            resp = session.get(f"{GAMMA_HOST}/markets", params=params, timeout=20)
            resp.raise_for_status()
            rows = resp.json()
        except requests.RequestException as exc:
            LOG.warning("gamma /markets offset=%d failed: %s", offset, exc)
            break
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if _is_resolved_binary(row):
                markets.append(row)
                if len(markets) >= max_markets:
                    break
        offset += len(rows)
        if len(rows) < 100:
            break
        time.sleep(0.3)  # be polite to gamma
    LOG.info("Resolved binary markets pulled: %d", len(markets))
    return markets


def _is_resolved_binary(row: dict[str, Any]) -> bool:
    if not row.get("closed"):
        return False
    outcomes = row.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = []
    if not isinstance(outcomes, list) or len(outcomes) != 2:
        return False
    return _yes_token_id(row) is not None and _resolved_yes(row) is not None


def _yes_token_id(row: dict[str, Any]) -> str | None:
    raw_ids = row.get("clobTokenIds") or row.get("clob_token_ids")
    if isinstance(raw_ids, str):
        try:
            raw_ids = json.loads(raw_ids)
        except json.JSONDecodeError:
            return None
    raw_outcomes = row.get("outcomes") or []
    if isinstance(raw_outcomes, str):
        try:
            raw_outcomes = json.loads(raw_outcomes)
        except json.JSONDecodeError:
            return None
    if not (
        isinstance(raw_ids, list)
        and isinstance(raw_outcomes, list)
        and len(raw_ids) == len(raw_outcomes) == 2
    ):
        return None
    for tok_id, outcome in zip(raw_ids, raw_outcomes):
        if str(outcome).strip().lower() == "yes":
            return str(tok_id)
    return None


def _resolved_yes(row: dict[str, Any]) -> bool | None:
    """Return True/False for clean YES/NO resolution, None for ambiguous."""
    raw_prices = row.get("outcomePrices") or row.get("outcome_prices") or []
    if isinstance(raw_prices, str):
        try:
            raw_prices = json.loads(raw_prices)
        except json.JSONDecodeError:
            return None
    raw_outcomes = row.get("outcomes") or []
    if isinstance(raw_outcomes, str):
        try:
            raw_outcomes = json.loads(raw_outcomes)
        except json.JSONDecodeError:
            return None
    if not (
        isinstance(raw_prices, list)
        and isinstance(raw_outcomes, list)
        and len(raw_prices) == len(raw_outcomes) == 2
    ):
        return None
    try:
        prices = [float(p) for p in raw_prices]
    except (TypeError, ValueError):
        return None
    # Must be a clean 0/1 split — otherwise refunded / cancelled.
    if not all(p in (0.0, 1.0) for p in prices):
        return None
    if sum(prices) != 1.0:
        return None
    for outcome, price in zip(raw_outcomes, prices):
        if str(outcome).strip().lower() == "yes":
            return price == 1.0
    return None


def fetch_max_price(session: requests.Session, token_id: str) -> float | None:
    """Return max YES price over the market's lifetime, or None on failure."""
    params = {"market": token_id, "interval": "max", "fidelity": "60"}
    try:
        resp = session.get(
            f"{CLOB_HOST}/prices-history", params=params, timeout=20
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        LOG.debug("prices-history %s failed: %s", token_id[:12], exc)
        return None
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list) or not history:
        return None
    prices: list[float] = []
    for point in history:
        if not isinstance(point, dict):
            continue
        try:
            prices.append(float(point["p"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not prices:
        return None
    return max(prices)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache() -> dict[str, dict[str, Any]]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("cache read failed: %s", exc)
        return {}


def save_cache(cache: dict[str, dict[str, Any]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score CI for a binomial proportion."""
    if total <= 0:
        return (0.0, 0.0)
    p_hat = successes / total
    denom = 1 + z * z / total
    center = (p_hat + z * z / (2 * total)) / denom
    margin = (z * math.sqrt(p_hat * (1 - p_hat) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def summarise(
    samples: list[tuple[float, bool]], *, buckets: list[float]
) -> list[dict[str, Any]]:
    """For each bucket lower bound, report cohort size + realised YES rate."""
    out: list[dict[str, Any]] = []
    for bucket in buckets:
        cohort = [(mx, yes) for mx, yes in samples if mx >= bucket]
        total = len(cohort)
        yeses = sum(1 for _, yes in cohort if yes)
        rate = (yeses / total) if total else 0.0
        ci_low, ci_high = wilson_ci(yeses, total)
        # Article's claim: rate < bucket implies the market underestimates
        # the conditional NO risk after touching that price level. We
        # tag "mispricing_confirmed" only when the CI upper bound is
        # below the bucket (so the rate is < bucket *with confidence*).
        out.append(
            {
                "bucket_min_price": bucket,
                "cohort_size": total,
                "resolved_yes": yeses,
                "resolved_yes_rate": round(rate, 4),
                "ci_low": round(ci_low, 4),
                "ci_high": round(ci_high, 4),
                "implied_rate_for_bucket": bucket,
                "mispricing_confirmed": ci_high < bucket and total >= 30,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-markets", type=int, default=500,
                        help="cap on resolved binary markets to scan (default 500)")
    parser.add_argument("--sleep-sec", type=float, default=0.15,
                        help="pause between CLOB calls (default 0.15)")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cache, re-fetch all prices-history")
    args = parser.parse_args()

    session = _session()
    cache = {} if args.refresh else load_cache()
    LOG.info("cache size at start: %d", len(cache))

    markets = fetch_resolved_binary_markets(session, max_markets=args.max_markets)
    if not markets:
        LOG.error("No resolved binary markets pulled — abort.")
        return 1

    samples: list[tuple[float, bool]] = []
    new_lookups = 0
    fetch_failures = 0
    for i, row in enumerate(markets, start=1):
        market_id = row.get("conditionId") or row.get("condition_id") or row.get("id")
        if not market_id:
            continue
        market_id = str(market_id)
        resolved_yes = _resolved_yes(row)
        if resolved_yes is None:
            continue

        cached = cache.get(market_id)
        if cached and "max_price" in cached:
            max_price = float(cached["max_price"])
        else:
            token_id = _yes_token_id(row)
            if token_id is None:
                continue
            max_price = fetch_max_price(session, token_id) or float("nan")
            new_lookups += 1
            cache[market_id] = {
                "max_price": max_price,
                "resolved_yes": resolved_yes,
                "question": row.get("question", "")[:120],
            }
            if new_lookups % 25 == 0:
                save_cache(cache)
                LOG.info("progress: %d/%d markets, cache=%d", i, len(markets), len(cache))
            time.sleep(args.sleep_sec)

        if math.isnan(max_price):
            fetch_failures += 1
            continue
        samples.append((max_price, bool(resolved_yes)))

    save_cache(cache)
    LOG.info(
        "samples=%d new_lookups=%d fetch_failures=%d cache=%d",
        len(samples), new_lookups, fetch_failures, len(cache),
    )

    buckets = [0.50, 0.80, 0.90, 0.92, 0.95, 0.97, 0.98]
    summary_rows = summarise(samples, buckets=buckets)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_markets_scanned": len(markets),
        "samples_with_history": len(samples),
        "fetch_failures": fetch_failures,
        "buckets": summary_rows,
    }
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== Near-certainty empirical check ===")
    print(f"  markets scanned     : {len(markets)}")
    print(f"  samples with history: {len(samples)}")
    print(f"  fetch failures      : {fetch_failures}")
    print()
    print(f"  {'bucket':>7} | {'N':>5} | {'YES%':>7} | {'CI':>19} | confirm?")
    print(f"  {'-'*7} | {'-'*5} | {'-'*7} | {'-'*19} | {'-'*8}")
    for r in summary_rows:
        ci_str = f"[{r['ci_low']:.3f}, {r['ci_high']:.3f}]"
        flag = "YES" if r["mispricing_confirmed"] else ""
        print(
            f"  {r['bucket_min_price']:>7.2f} | {r['cohort_size']:>5} | "
            f"{r['resolved_yes_rate']*100:>6.2f}% | {ci_str:>19} | {flag}"
        )
    print(f"\nFull report: {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
