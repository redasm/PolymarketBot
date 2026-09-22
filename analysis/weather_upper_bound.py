"""Upper-bound test: is there any edge in Polymarket daily-temperature markets?

Deliberately generous to the strategy at every step, because the point is a
*ceiling*: if the strategy cannot clear its costs even here, no better forecast
input can save it.

Generosity, itemised:
  * The forecast is Open-Meteo's **archived same-day model run** — strictly more
    informed than the ensemble a trader could have had 24h before resolution.
    (The live provider's 31-member ensemble simply is not archived: past dates
    return null on `ensemble-api` for gfs/ecmwf/icon/gem alike.)
  * The forecast error sigma is fitted **in-sample** over the whole period.
  * Entry is at the recorded `prices-history` print, not at an ask, unless a
    haircut is passed.
  * Positions are held to resolution, so only the entry taker fee is charged
    (`feeSchedule.rate = 0.05`, taker-only, fee = rate * p * (1-p) per share).

The bucket ladder is mutually exclusive and exhaustive, so per-event model
probabilities are normalised across the 11 buckets before being compared to the
market.

Usage:
    python analysis/weather_upper_bound.py --dataset <weather_dataset_lead24h.json>
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import statistics

FEE_RATE = 0.05


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(lo, hi, mu, sigma):
    if sigma <= 0:
        sigma = 0.5
    # Buckets are stated on whole degrees and the observation is rounded, so the
    # real interval for "80-81" is [79.5, 81.5).
    lo_edge = (lo - 0.5) if lo is not None else None
    hi_edge = (hi + 0.5) if hi is not None else None
    p_hi = norm_cdf((hi_edge - mu) / sigma) if hi_edge is not None else 1.0
    p_lo = norm_cdf((lo_edge - mu) / sigma) if lo_edge is not None else 0.0
    return max(0.0, p_hi - p_lo)


def realized_from_winner(lo, hi):
    """Point estimate of the realized max from the winning bucket."""
    if lo is not None and hi is not None:
        return (lo + hi) / 2.0
    if hi is not None:
        return hi - 1.0
    if lo is not None:
        return lo + 1.0
    return None


def fee_of(price: float) -> float:
    p = max(0.0, min(1.0, price))
    return FEE_RATE * p * (1.0 - p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", nargs="+", required=True)
    ap.add_argument("--lead-forecasts", default=None,
                    help="lead_forecasts.json from weather_lead_forecasts.py")
    ap.add_argument("--lead", default=None,
                    help="which lead (days) to price off; requires --lead-forecasts")
    ap.add_argument("--debias", action="store_true",
                    help="subtract each city's in-sample mean forecast error before pricing")
    ap.add_argument("--min-edges", default="0.05,0.10,0.15,0.20")
    ap.add_argument("--haircuts", default="0,0.01,0.02", help="cents paid over the print")
    ap.add_argument("--size", type=float, default=100.0, help="shares per position")
    args = ap.parse_args()

    events, forecasts, prices, lead = [], {}, {}, None
    for path in args.dataset:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        events.extend(d["events"])
        forecasts.update(d["forecasts"])
        prices.update(d["prices"])
        lead = d["lead_hours"]
    data = {"lead_hours": lead}

    # Swap the same-day archived run for the run issued `--lead` days earlier.
    # Without this the forecast is a nowcast and the whole exercise is only an
    # upper bound; with it the forecast is one a trader actually had in hand.
    if args.lead_forecasts and args.lead:
        with open(args.lead_forecasts, encoding="utf-8") as fh:
            leadfc = json.load(fh)
        swapped = {}
        for key, row in leadfc.items():
            picked = row.get(str(args.lead)) or {}
            if picked.get("F") is not None or picked.get("C") is not None:
                swapped[key] = picked
        forecasts = swapped
        print(f"pricing off the run issued {args.lead}d earlier "
              f"({len(forecasts)} city-date rows)")

    # ---- stage 1: forecast calibration against the resolved bucket ----
    resid_by_city = collections.defaultdict(list)
    usable = []
    for ev in events:
        fc = forecasts.get(f"{ev['city']}|{ev['date']}") or {}
        winner = next((m for m in ev["markets"] if m["resolved_yes"] >= 0.99), None)
        if winner is None:
            continue
        unit = winner["unit"]
        mu = fc.get(unit)
        if mu is None:
            continue
        realized = realized_from_winner(winner["lo"], winner["hi"])
        if realized is None:
            continue
        resid_by_city[ev["city"]].append(mu - realized)
        usable.append((ev, mu, unit, realized))

    print(f"events={len(events)}  usable(resolved+forecast)={len(usable)}")
    print(f"\n{'city':>14}{'n':>6}{'bias':>8}{'sigma':>8}  (archived same-day forecast minus realized)")
    sigma_by_city = {}
    for city, rs in sorted(resid_by_city.items()):
        sd = statistics.pstdev(rs) if len(rs) > 1 else 1.0
        sigma_by_city[city] = max(0.3, sd)
        print(f"{city:>14}{len(rs):>6}{statistics.mean(rs):>8.2f}{sd:>8.2f}")

    # Station-vs-grid offset is real (Open-Meteo geocodes a city centre, the
    # market settles on one airport station), so an uncorrected forecast can
    # carry a several-degree bias. Removing it in-sample is generous to the
    # strategy, which is the point of an upper bound.
    bias_by_city = ({c: statistics.mean(rs) for c, rs in resid_by_city.items()}
                    if args.debias else collections.defaultdict(float))

    # ---- stage 2: model vs market ----
    positions = []
    for ev, mu, unit, realized in usable:
        mu = mu - bias_by_city[ev["city"]]
        sigma = sigma_by_city[ev["city"]]
        raw = []
        for m in ev["markets"]:
            if m["unit"] != unit:
                continue
            raw.append((m, bucket_prob(m["lo"], m["hi"], mu, sigma)))
        total = sum(p for _, p in raw)
        if total <= 0:
            continue
        for m, p in raw:
            model = p / total
            row = prices.get(m["condition_id"])
            if not row:
                continue
            mkt = float(row["p"])
            if not 0.0 < mkt < 1.0:
                continue
            positions.append({
                "city": ev["city"], "date": ev["date"], "model": model, "mkt": mkt,
                "resolved_yes": m["resolved_yes"], "volume": m.get("volume") or 0.0,
            })
    print(f"\nscored markets with a T-{data['lead_hours']:.0f}h price: {len(positions)}")

    brier_model = statistics.mean((p["model"] - p["resolved_yes"]) ** 2 for p in positions)
    brier_mkt = statistics.mean((p["mkt"] - p["resolved_yes"]) ** 2 for p in positions)
    print(f"Brier score  model={brier_model:.4f}  market={brier_mkt:.4f}  "
          f"({'model better' if brier_model < brier_mkt else 'MARKET BETTER'})")

    min_edges = [float(x) for x in args.min_edges.split(",") if x.strip()]
    haircuts = [float(x) for x in args.haircuts.split(",") if x.strip()]

    print(f"\nsize={args.size:.0f} shares/position, hold to resolution, "
          f"entry fee = {FEE_RATE} * p * (1-p)")
    print(f"{'min_edge':>9}{'haircut':>9}{'n':>6}{'net PnL':>11}{'per pos':>9}"
          f"{'fees':>9}{'hit%':>7}{'no-fee PnL':>12}")
    for min_edge in min_edges:
        for hc in haircuts:
            n = 0
            pnl = fees = gross = 0.0
            hits = 0
            for p in positions:
                edge = p["model"] - p["mkt"]
                if abs(edge) < min_edge:
                    continue
                if edge > 0:                      # market too cheap -> buy YES
                    price = min(0.999, p["mkt"] + hc)
                    payout = p["resolved_yes"]
                else:                             # market too rich -> buy NO
                    price = min(0.999, 1.0 - p["mkt"] + hc)
                    payout = 1.0 - p["resolved_yes"]
                f = fee_of(price) * args.size
                n += 1
                hits += 1 if payout > 0.5 else 0
                gross += (payout - price) * args.size
                fees += f
                pnl += (payout - price) * args.size - f
            if n:
                print(f"{min_edge:>9.2f}{hc:>9.2f}{n:>6}{pnl:>11.2f}{pnl / n:>9.3f}"
                      f"{fees:>9.2f}{hits / n:>7.1%}{gross:>12.2f}")
            else:
                print(f"{min_edge:>9.2f}{hc:>9.2f}{0:>6}{'-':>11}")

    print("\nper-city at min_edge=0.10, haircut=0.01:")
    for city, (n, pnl) in sorted(_per_city(positions, args.size, 0.10, 0.01).items()):
        print(f"  {city:>14}{n:>6}{pnl:>11.2f}{pnl / n if n else 0:>9.3f}")


def _per_city(positions, size, min_edge, haircut):
    agg = collections.defaultdict(lambda: [0, 0.0])
    for p in positions:
        edge = p["model"] - p["mkt"]
        if abs(edge) < min_edge:
            continue
        if edge > 0:
            price, payout = min(0.999, p["mkt"] + haircut), p["resolved_yes"]
        else:
            price, payout = min(0.999, 1.0 - p["mkt"] + haircut), 1.0 - p["resolved_yes"]
        row = agg[p["city"]]
        row[0] += 1
        row[1] += (payout - price) * size - fee_of(price) * size
    return agg


if __name__ == "__main__":
    main()
