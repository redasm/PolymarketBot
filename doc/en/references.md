[English](../en/references.md) · [中文](../zh/references.md)

# External references

Data sources, APIs and prior art referenced by this project, with notes on what
each was actually usable for.

## Polymarket

| Resource | URL | Notes |
|---|---|---|
| CLOB API | `https://clob.polymarket.com` | Order books, order submission, `/rewards/markets`, `/orders-scoring`, `/prices-history`. |
| Gamma API | `https://gamma-api.polymarket.com` | Market and event metadata, `?closed=true` for resolved markets. |
| Data API | `https://data-api.polymarket.com` | Positions, trades, `/closed-positions`. |
| Docs | <https://docs.polymarket.com> | Including the deposit-wallet guide used for signature type 3. |

Two facts about the venue that changed results materially:

- **Fee V2, 30 March 2026.** Taker fee is `rate · p · (1-p)`, maximised at
  `p = 0.5`. `rate` is per-market — crypto Up/Down carries
  `feeSchedule.rate = 0.07` against a common default of `0.005`.
- **CLOB V2 hard cutover, 28 April 2026.** Data recorded before this is not
  comparable to data after. Any change in account funds requires a
  balance-allowance update call (signature type 3).

Known free-tier limitations found while trying to validate historical claims
(2026-05-22):

- `clob.polymarket.com/prices-history` returns empty for closed tokens — only
  active markets retain history.
- `data-api.polymarket.com/trades` silently ignores per-market filters and
  returns the global feed.
- Goldsky `orderbook-subgraph/prod` statement-times-out on
  `orderBy: timestamp desc` for heavy markets; older markets are unindexed.

## Weather data

All Open-Meteo endpoints used, all free and keyless:

| Endpoint | Used for |
|---|---|
| `https://ensemble-api.open-meteo.com/v1/ensemble` | GFS ensemble forecasts driving the probability estimate |
| `https://historical-forecast-api.open-meteo.com/v1/forecast` | Historical forecast archive for backtests |
| `https://previous-runs-api.open-meteo.com/v1/forecast` | **Lead-honest** evaluation — the forecast vintage as it existed before the decision time |
| `https://geocoding-api.open-meteo.com/v1/search` | City name → coordinates |

The previous-runs endpoint is the one that matters for honest evaluation.
Without it, a weather backtest silently uses the best forecast for a date rather
than the forecast available at decision time, which is look-ahead bias. Making
that correction turned the strategy negative — see
[research-findings.md](research-findings.md#weather).

## News and sentiment

| Source | Auth | Notes |
|---|---|---|
| GDELT DOC 2.0 | none | Used for the information-layer audit. Aggressively rate limited: a 1s request interval produced false zeros; 7s plus backoff was needed. Its `enddatetime` is imprecise — filter on `seendate` again client-side. |
| alternative.me Fear & Greed | none | One sentiment row per crypto topic. Implemented as `CryptoMacroCollector`. |
| Manifold Markets | none | Crowd probability per topic; mainly useful for politics, geopolitics, sports and awards where news RSS is thin. |
| RSS feeds | none | Curated automatically by the `research-feeds-auto` worker, each probed with a real GET before being accepted. |

## Paid sources evaluated but not purchased

Needed for the validations in [pending-validations.md](pending-validations.md):

| Source | Purpose | Note |
|---|---|---|
| Dune Analytics | Historical Polymarket trade events, per-market max price | Free and ~$390/mo tiers; Polymarket dashboards already exist. |
| Goldsky paid tier | Same, without the statement timeout | — |
| Glassnode | MVRV Z-Score, SOPR | ~$30–40/mo. |
| CryptoQuant | Same metrics, free tier with a 1-day lag | Lag may be acceptable for daily-horizon markets. |
| SoSoValue / Coinglass | ETF netflow | Free tier around 60 req/min; workable with caching. |
| FRED | M2, Fed funds | Free with an API key. |

## Scoring models

- TypeSafe Jev — <https://docs.typesafe.ai>. A "System One" model returning
  typed, calibrated answers rather than text. Client implemented in
  `polymarket_arb/typesafe_provider.py` against `POST /v1/systemone`. Evaluated
  and found significantly worse than the order book; see
  [research-findings.md](research-findings.md#typesafe-jev--can-a-calibrated-scoring-model-beat-the-book).

## Prior art and related projects

Other open-source prediction-market bots and toolkits surveyed while building
this. Listing is not endorsement — none were benchmarked.

- <https://github.com/HarrierOnChain/Prediction-Markets-Trading-Bot-Toolkits>
- <https://github.com/warproxxx/poly-maker>
- <https://github.com/warproxxx/poly_data> — reads `OrderFilled` events directly
  on-chain, which is a useful reference for reconstructing V2 fills
- <https://github.com/MrFadiAi/Polymarket-bot>
- <https://github.com/suislanchez/polymarket-kalshi-weather-bot>
- <https://github.com/MoonsatProtocol/Polymarket-Weather-Bot>
- <https://github.com/yangyuan-zhen/PolyWeather>
- <https://github.com/nicolastinkl/hermes_weatherbot>
- <https://github.com/AruneshDev/Automated-Trading-System-Kalshi-Weather-Model>
- <https://github.com/bwjoke/BTC-Trading-Since-2020>
- <https://github.com/txbabaxyz/mlmodelpoly> — the fair-value / volatility /
  microprice ideas in T2 draw on this

## Academic and textbook results used

| Result | Where it is used |
|---|---|
| Kelly (1956) | `strategies/kelly.py`, Quarter-Kelly sizing |
| Snell envelope / optimal stopping (1965) | `strategies/optimal_stopping.py` |
| Kobylanski (2009), multiple-stopping dominates single-stopping | `T2_SCALE_OUT_TRANCHES` scale-out |
| Taleb, barbell allocation | `T2_BARBELL_ENABLED` (off, unvalidated) |
| Becker (2025), longshot/favourite tax | `T2_REJECT_PRICE_BELOW` / `_ABOVE`, and the T3 flow-bias work |
