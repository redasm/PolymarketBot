"""Harvest resolved Polymarket daily-temperature markets + archived forecasts.

Everything here comes from public endpoints, no key:

* Gamma ``/events?slug=highest-temperature-in-<city>-on-<month>-<day>-<year>``
  — deterministic slug, 11 bucket markets per event, resolved outcomes included.
  The resolved market itself is the settlement truth, which sidesteps the
  station-vs-gridded-reanalysis mismatch entirely (these markets settle on one
  specific NOAA station, e.g. London City Airport, not on a grid cell).
* CLOB ``/prices-history`` — per-token price series, so we can read the market's
  implied probability at a chosen lead time before resolution.
* Open-Meteo ``historical-forecast-api`` — the archived model run for that date.

**What this can and cannot support.** The live strategy prices off the 31-member
GFS *ensemble* (`ensemble-api`), and that API returns null for every past date
(verified across gfs/ecmwf/icon/gem), so the real provider cannot be replayed.
The archived deterministic forecast is *better* than what a live trader had at
entry time, so any edge measured against it is an **upper bound**: if it fails
to clear the 5% taker fee, the strategy is dead without needing the GEFS archive.

Usage:
    python analysis/weather_harvest.py --out <dir> --days 120
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

import requests

CITIES = {
    # slug -> (display, unit) ; lat/lon resolved through Open-Meteo geocoding
    "nyc": ("New York", "F"),
    "london": ("London", "C"),
    "tokyo": ("Tokyo", "C"),
    "miami": ("Miami", "F"),
    "los-angeles": ("Los Angeles", "F"),
    "paris": ("Paris", "C"),
}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GEO = "https://geocoding-api.open-meteo.com/v1/search"
HIST_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Two ladder shapes are live at once and they are NOT interchangeable:
#   Fahrenheit cities use two-degree ranges  -> "be between 80-81°F on ..."
#   Celsius cities use single-degree buckets -> "be 23°C on ..."
# and the open top end says "or above" in one and "or higher" in the other.
# Missing the single-degree form silently drops every °C city except its
# bottom bucket, which looks like "no winner" downstream rather than an error.
_BETWEEN = re.compile(r"between\s+(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CF])", re.I)
_OR_BELOW = re.compile(r"be\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s*or\s+(?:below|lower)", re.I)
_OR_ABOVE = re.compile(r"be\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s*or\s+(?:above|higher)", re.I)
_EXACT = re.compile(r"be\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s+on\b", re.I)


def parse_bucket(question: str):
    """-> (lo, hi, unit) with None meaning open-ended on that side."""
    m = _BETWEEN.search(question)
    if m:
        return float(m.group(1)), float(m.group(2)), m.group(3).upper()
    m = _OR_BELOW.search(question)
    if m:
        return None, float(m.group(1)), m.group(2).upper()
    m = _OR_ABOVE.search(question)
    if m:
        return float(m.group(1)), None, m.group(2).upper()
    m = _EXACT.search(question)
    if m:
        v = float(m.group(1))
        return v, v, m.group(2).upper()
    return None


class Http:
    def __init__(self, sleep: float = 0.05):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0"})
        self.sleep = sleep

    def json(self, url, params=None, tries=3, timeout=30):
        last = None
        for i in range(tries):
            try:
                r = self.s.get(url, params=params, timeout=timeout)
                if r.status_code == 429:
                    time.sleep(2 + i * 3)
                    continue
                r.raise_for_status()
                time.sleep(self.sleep)
                return r.json()
            except Exception as exc:  # noqa: BLE001 - harvest must not die on one row
                last = exc
                time.sleep(0.5 + i)
        print(f"    ! {url} {params} -> {last!r}"[:180], flush=True)
        return None


def harvest_events(http: Http, days: int, end: dt.date):
    out = []
    for city in CITIES:
        got = 0
        for back in range(1, days + 1):
            d = end - dt.timedelta(days=back)
            slug = f"highest-temperature-in-{city}-on-{d.strftime('%B').lower()}-{d.day}-{d.year}"
            rows = http.json(f"{GAMMA}/events", params={"slug": slug})
            if not rows:
                continue
            ev = rows[0]
            if not ev.get("closed"):
                continue
            markets = []
            for m in ev.get("markets", []):
                bucket = parse_bucket(m.get("question") or "")
                if bucket is None:
                    continue
                try:
                    prices = json.loads(m.get("outcomePrices") or "[]")
                    tokens = json.loads(m.get("clobTokenIds") or "[]")
                except (TypeError, ValueError):
                    continue
                if len(prices) < 2 or len(tokens) < 2:
                    continue
                markets.append({
                    "condition_id": m.get("conditionId"),
                    "question": m.get("question"),
                    "yes_token": tokens[0],
                    "lo": bucket[0], "hi": bucket[1], "unit": bucket[2],
                    "resolved_yes": float(prices[0]),
                    "fee_rate": (m.get("feeSchedule") or {}).get("rate"),
                    "volume": m.get("volumeNum"),
                    "end_date": m.get("endDate"),
                })
            if markets:
                out.append({"city": city, "date": d.isoformat(), "slug": slug,
                            "event_volume": ev.get("volume"), "markets": markets})
                got += 1
        print(f"  {city}: {got} resolved events", flush=True)
    return out


def geocode(http: Http):
    coords = {}
    for city, (name, _unit) in CITIES.items():
        j = http.json(GEO, params={"name": name, "count": 1})
        res = (j or {}).get("results") or []
        if res:
            coords[city] = (res[0]["latitude"], res[0]["longitude"], res[0].get("timezone"))
    return coords


def harvest_forecasts(http: Http, events, coords):
    """(city, date) -> archived daily max in both units."""
    need = sorted({(e["city"], e["date"]) for e in events})
    out = {}
    for city, day in need:
        if city not in coords:
            continue
        lat, lon, tz = coords[city]
        row = {}
        for unit, api_unit in (("F", "fahrenheit"), ("C", "celsius")):
            j = http.json(HIST_FC, params={
                "latitude": lat, "longitude": lon, "start_date": day, "end_date": day,
                "daily": "temperature_2m_max", "temperature_unit": api_unit,
                "timezone": tz or "auto"})
            vals = ((j or {}).get("daily") or {}).get("temperature_2m_max") or []
            row[unit] = vals[0] if vals else None
        out[f"{city}|{day}"] = row
    return out


def harvest_prices(http: Http, events, lead_hours: float):
    """condition_id -> YES price at ~lead_hours before the market's end."""
    out = {}
    total = sum(len(e["markets"]) for e in events)
    done = 0
    for e in events:
        # Resolution is the end of the target day, local-ish; use 00:00 UTC of
        # the following day as the anchor and read back `lead_hours` from it.
        day = dt.date.fromisoformat(e["date"])
        anchor = dt.datetime.combine(day + dt.timedelta(days=1),
                                     dt.time(0, 0), tzinfo=dt.timezone.utc).timestamp()
        for m in e["markets"]:
            done += 1
            if done % 250 == 0:
                print(f"    prices {done}/{total}", flush=True)
            start = int(anchor - lead_hours * 3600 - 3 * 3600)
            j = http.json(f"{CLOB}/prices-history", params={
                "market": m["yes_token"], "startTs": start,
                "endTs": int(anchor - lead_hours * 3600), "fidelity": 10})
            hist = (j or {}).get("history") or []
            if hist:
                out[m["condition_id"]] = {"p": hist[-1]["p"], "t": hist[-1]["t"], "n": len(hist)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--lead-hours", type=float, default=24.0)
    ap.add_argument("--cities", default=None, help="comma-separated subset of CITIES")
    args = ap.parse_args()
    if args.cities:
        keep = {c.strip() for c in args.cities.split(",") if c.strip()}
        for city in list(CITIES):
            if city not in keep:
                CITIES.pop(city)
    os.makedirs(args.out, exist_ok=True)
    http = Http()

    print("1/4 events", flush=True)
    events = harvest_events(http, args.days, dt.date.today())
    print(f"  total resolved events: {len(events)}, "
          f"markets: {sum(len(e['markets']) for e in events)}", flush=True)

    print("2/4 geocode", flush=True)
    coords = geocode(http)
    print(" ", coords, flush=True)

    print("3/4 archived forecasts", flush=True)
    forecasts = harvest_forecasts(http, events, coords)
    print(f"  {len(forecasts)} (city,date) forecast rows", flush=True)

    print(f"4/4 prices at T-{args.lead_hours}h", flush=True)
    prices = harvest_prices(http, events, args.lead_hours)
    print(f"  {len(prices)} markets with a price", flush=True)

    payload = {"events": events, "coords": coords, "forecasts": forecasts,
               "prices": prices, "lead_hours": args.lead_hours}
    tag = ("_" + "_".join(sorted(CITIES))) if args.cities else ""
    path = os.path.join(args.out, f"weather_dataset_lead{int(args.lead_hours)}h{tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    print("wrote", path, flush=True)


if __name__ == "__main__":
    sys.exit(main())
