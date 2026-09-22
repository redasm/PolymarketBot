"""Open-data replay for the weather strategy.

This is deliberately a research test, not a claim of live profitability.  It
joins three public data sources:

* Polymarket Gamma: resolved weather events, contract buckets and winners;
* Polymarket CLOB: historical token trade-price samples;
* Open-Meteo GFS ensemble: a retrospective 30-member temperature distribution.

The Open-Meteo endpoint is not timestamp-vintaged.  Therefore the model
metrics are a hindcast, while the trading result is explicitly labelled a
sensitivity test (historical last-trade price + an assumed spread/slippage).
Run with ``--refresh`` to download a new snapshot.  Files are cached under
``data/open_data/weather_test`` so the result can be reproduced offline.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Allow ``python analysis/run_weather_open_data_test.py`` from the repository
# root, matching the way the other analysis entry points are invoked.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_arb.models import FeeStructure
from polymarket_arb.strategies.weather_strategy import CITY_CONFIG, CITY_ALIASES


DEFAULT_CACHE = ROOT / "data" / "open_data" / "weather_test"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
MONTHS = {name.lower(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1
)}


def _get_json(url: str, params: dict[str, Any], *, timeout: float = 45.0, tries: int = 3) -> Any:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt + 1 < tries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"request failed: {url}: {last}")


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_target_date(text: str) -> date | None:
    match = re.search(
        r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:,?\s*(\d{4}))?\b",
        text,
        re.I,
    )
    if not match:
        return None
    try:
        return date(int(match.group(3) or date.today().year), MONTHS[match.group(1).lower()], int(match.group(2)))
    except ValueError:
        return None


def city_key_for(text: str) -> str | None:
    low = text.lower()
    for alias, key in sorted(CITY_ALIASES.items(), key=lambda item: -len(item[0])):
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", low):
            return key
    return None


def parse_bucket(question: str) -> tuple[float, float | None, str] | None:
    """Return lower, upper-exclusive and metric/direction bucket semantics."""
    text = question.lower()
    range_match = re.search(r"(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*°?\s*f", text)
    if range_match:
        return (float(range_match.group(1)), float(range_match.group(2)) + 1.0, "range")
    numbers = re.findall(r"(-?\d+(?:\.\d+)?)\s*°?\s*f", text)
    if not numbers:
        return None
    low = float(numbers[0])
    if "below" in text or "or less" in text or "or lower" in text:
        return (-math.inf, low + 1.0, "below")
    if "higher" in text or "above" in text or "or more" in text:
        return (low, math.inf, "above")
    if len(numbers) >= 2:
        return (low, float(numbers[1]) + 1.0, "range")
    return (low, low + 1.0, "exact")


def is_target_event(event: dict[str, Any]) -> bool:
    title = str(event.get("title") or "")
    low = title.lower()
    return "temperature" in low and city_key_for(title) is not None and parse_target_date(title) is not None


def load_events(cache: Path, *, refresh: bool, max_pages: int) -> list[dict[str, Any]]:
    path = cache / "gamma_weather_events.json"
    if not refresh:
        cached = _read_json(path)
        if isinstance(cached, list) and cached:
            return cached
    events: list[dict[str, Any]] = []
    for page in range(max_pages):
        data = _get_json(
            f"{GAMMA}/events",
            {"tag_id": 84, "closed": "true", "limit": 100, "offset": page * 100,
             "order": "endDate", "ascending": "false"},
        )
        if not isinstance(data, list) or not data:
            break
        events.extend(event for event in data if is_target_event(event))
        if len(data) < 100:
            break
    # Keep deterministic order and only resolved events with binary buckets.
    events = sorted({str(e.get("id")): e for e in events}.values(), key=lambda e: str(e.get("endDate", "")))
    _write_json(path, events)
    return events


def load_ensemble(cache: Path, city: str, start: date, end: date, *, refresh: bool) -> dict[str, list[float]]:
    path = cache / "ensemble" / f"{city}_{start}_{end}.json"
    data = None if refresh else _read_json(path)
    if data is None:
        cfg = CITY_CONFIG[city]
        data = _get_json(
            ENSEMBLE,
            {"latitude": cfg["lat"], "longitude": cfg["lon"], "start_date": start.isoformat(),
             "end_date": end.isoformat(), "daily": "temperature_2m_max,temperature_2m_min",
             "temperature_unit": "fahrenheit", "models": "gfs_seamless"},
            timeout=60,
        )
        _write_json(path, data)
    daily = data.get("daily", {})
    result: dict[str, list[float]] = {}
    for idx, day in enumerate(daily.get("time", [])):
        values: list[float] = []
        for prefix in ("temperature_2m_max", "temperature_2m_min"):
            # The caller selects metric later; retain both using namespaced keys.
            for key, series in daily.items():
                if key.startswith(prefix + "_member") and isinstance(series, list) and idx < len(series) and series[idx] is not None:
                    values.append(float(series[idx]))
            result[f"{day}|max"] = [float(daily[key][idx]) for key in daily if key.startswith("temperature_2m_max_member") and idx < len(daily[key]) and daily[key][idx] is not None]
            result[f"{day}|min"] = [float(daily[key][idx]) for key in daily if key.startswith("temperature_2m_min_member") and idx < len(daily[key]) and daily[key][idx] is not None]
    return result


def token_history(cache: Path, token: str, start_ts: int, end_ts: int, *, refresh: bool) -> list[dict[str, Any]]:
    path = cache / "prices" / f"{token}.json"
    data = None if refresh else _read_json(path)
    if data is None:
        try:
            data = _get_json(f"{CLOB}/prices-history", {"market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": 60}, timeout=60)
        except RuntimeError:
            data = {"history": []}
        _write_json(path, data)
    return data.get("history", []) if isinstance(data, dict) else []


def _history_worker(args: tuple[Path, str, int, int, bool]) -> tuple[str, list[dict[str, Any]]]:
    cache, token, start_ts, end_ts, refresh = args
    return token, token_history(cache, token, start_ts, end_ts, refresh=refresh)


def model_probability(values: list[float], bucket: tuple[float, float | None, str]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    low, upper, _ = bucket
    probability = sum(low <= value < (upper if upper is not None else math.inf) for value in values) / len(values)
    agreement = max(probability, 1.0 - probability)
    confidence = min(0.95, max(0.50, agreement * min(1.0, len(values) / 31.0)))
    return probability, confidence


def fee_pnl(notional: float, ask: float, won: bool, fee_rate: float) -> tuple[float, float, float]:
    ask = max(0.001, min(0.999, ask))
    shares = notional / ask
    fee = FeeStructure(taker_fee_rate=fee_rate).estimate_price_fee(ask, size=shares)
    payout = shares if won else 0.0
    return payout - notional - fee, fee, shares


def run(args: argparse.Namespace) -> dict[str, Any]:
    cache = Path(args.cache).resolve()
    events = load_events(cache, refresh=args.refresh, max_pages=args.max_pages)
    events = events[-args.max_events:] if args.max_events else events
    if not events:
        raise SystemExit("No matching resolved US weather events found")
    dates = [parse_target_date(str(e.get("title"))) for e in events]
    start, end = min(d for d in dates if d), max(d for d in dates if d)
    ensembles: dict[str, dict[str, list[float]]] = {}
    for city in CITY_CONFIG:
        if any(city_key_for(str(e.get("title"))) == city for e in events):
            ensembles[city] = load_ensemble(cache, city, start, end, refresh=args.refresh)

    jobs: list[tuple[Path, str, int, int, bool]] = []
    market_meta: list[dict[str, Any]] = []
    for event in events:
        event_start = _ts(event.get("startDate"))
        event_end = _ts(event.get("endDate")) + 86400
        for market in event.get("markets", []):
            tokens = json.loads(market.get("clobTokenIds", "[]")) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
            if not isinstance(tokens, list) or len(tokens) != 2:
                continue
            question = str(market.get("question") or "")
            bucket = parse_bucket(question)
            city = city_key_for(question)
            target = parse_target_date(question)
            if bucket is None or city is None or target is None:
                continue
            prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])
            if not isinstance(prices, list) or len(prices) != 2 or all(float(x) in (0.0, 1.0) for x in prices) is False:
                # Resolved markets normally expose [0,1] or [1,0].
                pass
            yes_won = float(prices[0]) > 0.5 if prices else None
            fee_schedule = market.get("feeSchedule") or {}
            fallback_fee_rate = float(args.fee_rates[0])
            fee_rate = float(fee_schedule.get("rate", fallback_fee_rate)) if isinstance(fee_schedule, dict) else fallback_fee_rate
            market_meta.append({"event": event, "market": market, "tokens": tokens, "question": question,
                                "bucket": bucket, "city": city, "target": target, "yes_won": yes_won,
                                "start_ts": event_start, "end_ts": event_end, "fee_rate": fee_rate})
            for token in tokens[:1]:  # YES history is sufficient; NO is its complement for sensitivity replay.
                jobs.append((cache, str(token), event_start, event_end, args.refresh))

    histories: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(_history_worker, job) for job in jobs]
        for future in as_completed(futures):
            token, history = future.result()
            histories[token] = history

    rows: list[dict[str, Any]] = []
    for item in market_meta:
        history = histories.get(str(item["tokens"][0]), [])
        if not history:
            continue
        day_key = f"{item['target'].isoformat()}|{'min' if 'lowest' in item['question'].lower() else 'max'}"
        values = ensembles.get(item["city"], {}).get(day_key, [])
        p_model, confidence = model_probability(values, item["bucket"])
        entry = sorted((h for h in history if isinstance(h, dict) and h.get("p") is not None), key=lambda h: h.get("t", 0))
        if not entry:
            continue
        yes_price = max(0.001, min(0.999, float(entry[0]["p"])))
        deviation = p_model - yes_price
        action = "BUY_YES" if deviation > 0 else "BUY_NO"
        if abs(deviation) < args.min_edge or confidence < args.min_confidence:
            action = "SKIP"
        rows.append({"event_id": str(item["event"].get("id")), "market_id": str(item["market"].get("id")),
                     "target_date": item["target"].isoformat(), "city": item["city"], "question": item["question"],
                     "model_prob": p_model, "confidence": confidence, "yes_price": yes_price,
                     "yes_won": bool(item["yes_won"]), "deviation": deviation, "action": action,
                     "fee_rate": item["fee_rate"], "history_ts": entry[0].get("t"), "values_n": len(values)})

    # Model-layer metrics use every bucket with a resolved winner.
    brier = statistics.mean((r["model_prob"] - float(r["yes_won"])) ** 2 for r in rows) if rows else None
    logloss = statistics.mean(-math.log(max(1e-6, min(1 - 1e-6, r["model_prob"] if r["yes_won"] else 1 - r["model_prob"]))) for r in rows) if rows else None
    event_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        event_groups.setdefault(row["event_id"], []).append(row)
    top_correct = sum(max(group, key=lambda r: r["model_prob"])["yes_won"] for group in event_groups.values())

    sensitivity: list[dict[str, Any]] = []
    for spread_bps in args.spread_bps:
        for fee_rate in args.fee_rates:
            trades = [r for r in rows if r["action"] != "SKIP"]
            pnl = fees = 0.0
            for r in trades:
                midpoint = r["yes_price"] if r["action"] == "BUY_YES" else 1.0 - r["yes_price"]
                ask = midpoint * (1.0 + (spread_bps + args.slippage_bps) / 10_000.0)
                net, paid_fee, _ = fee_pnl(args.notional, ask, r["yes_won"] if r["action"] == "BUY_YES" else not r["yes_won"], fee_rate)
                pnl += net
                fees += paid_fee
            sensitivity.append({"spread_bps": spread_bps, "slippage_bps": args.slippage_bps,
                                "fee_rate": fee_rate, "trades": len(trades), "wins": sum(
                                    (r["yes_won"] if r["action"] == "BUY_YES" else not r["yes_won"]) for r in trades),
                                "pnl_usdc": pnl, "fees_usdc": fees,
                                "return_pct_on_traded_notional": pnl / (len(trades) * args.notional) * 100 if trades else None})

    result = {"source": {"gamma": GAMMA, "clob": CLOB, "open_meteo": ENSEMBLE,
                          "note": "Open-Meteo historical ensemble is retrospective, not timestamp-vintaged; CLOB prices-history is sampled last trade, not bid/ask."},
              "sample": {"events": len(event_groups), "markets_with_price": len(rows), "date_start": start.isoformat(), "date_end": end.isoformat()},
              "model": {"brier_score": brier, "log_loss": logloss, "top_bucket_accuracy": top_correct / len(event_groups) if event_groups else None,
                        "signals": sum(r["action"] != "SKIP" for r in rows), "min_edge": args.min_edge, "min_confidence": args.min_confidence},
              "sensitivity": sensitivity}
    cache.mkdir(parents=True, exist_ok=True)
    _write_json(cache / "weather_test_result.json", result)
    with (cache / "weather_test_rows.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["event_id"])
        writer.writeheader()
        writer.writerows(rows)
    return result


def _ts(value: Any) -> int:
    text = str(value or "")
    if not text:
        return int(time.time())
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--max-pages", type=int, default=20, help="Gamma pages (100 events/page)")
    parser.add_argument("--max-events", type=int, default=30, help="Newest matching events; 0 means all")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--notional", type=float, default=10.0)
    parser.add_argument("--min-edge", type=float, default=0.10)
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    parser.add_argument("--spread-bps", type=float, nargs="+", default=[0.0, 90.0, 180.0])
    parser.add_argument("--fee-rates", type=float, nargs="+", default=[0.005, 0.05])
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
