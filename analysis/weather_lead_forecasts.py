"""Lead-time-honest archived forecasts for the weather dataset.

`historical-forecast-api` hands back the *best* archived run for a date, which
for a same-day query is effectively a nowcast — more informed than anything a
trader had 24h earlier. That only supports an upper bound.

`previous-runs-api.open-meteo.com` exposes `temperature_2m_previous_dayN`:
the value for that timestamp **as forecast by the model run N days earlier**.
Daily aggregates are not offered for those variables (they 400), so the hourly
series is pulled and the daily max computed here, in the market's local day —
which is also the right window, since these markets settle on the highest
station reading during the local calendar day.

Writes `{city|date: {"1": {"F": x, "C": y}, "2": {...}}}` so the analysis can
re-run at each lead without touching the network again.

Usage:
    python analysis/weather_lead_forecasts.py --dataset <ds.json> [...] --out <file>
"""
from __future__ import annotations

import argparse
import json
import os
import time

import requests

PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"


def fetch(session, lat, lon, tz, day, lead, unit_api):
    var = f"temperature_2m_previous_day{lead}"
    for attempt in range(3):
        try:
            r = session.get(PREV, params={
                "latitude": lat, "longitude": lon, "start_date": day, "end_date": day,
                "hourly": var, "temperature_unit": unit_api, "timezone": tz or "auto"},
                timeout=30)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            r.raise_for_status()
            vals = ((r.json() or {}).get("hourly") or {}).get(var) or []
            vals = [v for v in vals if v is not None]
            return max(vals) if vals else None
        except Exception:  # noqa: BLE001 - a missing run must not kill the pull
            time.sleep(0.5 + attempt)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--leads", default="1,2,3")
    args = ap.parse_args()

    coords = {}
    need = set()
    for path in args.dataset:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        coords.update(d["coords"])
        for e in d["events"]:
            need.add((e["city"], e["date"]))
    need = sorted(need)
    leads = [int(x) for x in args.leads.split(",") if x.strip()]
    print(f"{len(need)} (city,date) pairs x {len(leads)} leads", flush=True)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    out = {}
    for i, (city, day) in enumerate(need, 1):
        if city not in coords:
            continue
        lat, lon, tz = coords[city]
        row = {}
        for lead in leads:
            row[str(lead)] = {
                "F": fetch(session, lat, lon, tz, day, lead, "fahrenheit"),
                "C": fetch(session, lat, lon, tz, day, lead, "celsius"),
            }
        out[f"{city}|{day}"] = row
        if i % 50 == 0:
            print(f"  {i}/{len(need)}", flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    print("wrote", args.out, flush=True)


if __name__ == "__main__":
    main()
