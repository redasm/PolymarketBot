## External Reference Integration

This project incorporates ideas from four external sources, in priority order.

### 1. prediction-market-backtesting

Adopted into the project by strengthening `research/backtest/`:

- Added a concrete dataset reader for `market_snapshots.jsonl` and `orderbook_events.jsonl`
- Expanded backtest reporting with fill rate, latency, fee totals, notional, drawdown, and profit factor
- Enriched trade logs with binary market microstructure features
- Extended the CLI to compare execution models and fee/slippage/latency grids

Why:

- This is the highest-leverage path for validating whether detected opportunities are realistically tradable.

### 2. mlmodelpoly

Already partially integrated before this change and extended further here:

- Existing project modules already borrow from its fair-value, edge, and volatility ideas
- This change carries more of that research style into offline backtests via feature extraction

Why:

- The strongest reusable parts are feature engineering, market microstructure, and telemetry, not the Binance-specific plumbing.

### 3. polymarket-guide-lac.vercel.app

Integrated as a source-of-truth guideline rather than executable code:

- Use it as onboarding and domain-reference material for Polymarket concepts
- Do not treat it as the canonical source for fees, settlement, or API behavior
- For production behavior, prefer official Polymarket docs and observed API responses

Why:

- It is useful for terminology and workflow understanding, but not reliable enough to be the sole implementation spec.

### 4. public-apis

Integrated as an extensibility path for `research_signal`:

- Added a generic HTTP JSON collector so external APIs can be plugged into research aggregation without one-off code
- Collector configs can be supplied through `RESEARCH_SIGNAL_HTTP_JSON_SOURCES`

Example:

```json
[
  {
    "name": "json_news",
    "url": "https://example.com/news",
    "items_path": "articles",
    "summary_path": "headline",
    "link_path": "url",
    "published_path": "published_at"
  }
]
```

Why:

- `public-apis` is best used as a discovery index for candidate research sources, not as a trading-system design reference.
