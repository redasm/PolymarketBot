[English](../en/backtesting.md) · [中文](../zh/backtesting.md)

# Recording and backtesting

Read [research-findings.md](research-findings.md#transferable-methodology)
before trusting any number this pipeline produces. Three of the four strategies
in this repository produced positive backtests that turned out to be
measurement artefacts.

## Recording ticks

Set `TICK_RECORD_ENABLED=true` and every WebSocket book update is appended to
NDJSON:

```
data/ticks/
├── 2026-04-11.ndjson      # rolls by UTC date
├── 2026-04-12.ndjson
└── 2026-04-12.1.ndjson    # and again past 200MB
```

One line per update:

```json
{
  "ts_ms": 1712345678000,
  "token_id": "0xabc...def",
  "event_type": "book",
  "best_bid": 0.52,
  "best_ask": 0.54,
  "bid_depth_5": 12500.0,
  "ask_depth_5": 8700.0,
  "imbalance_5": 0.18,
  "microprice": 0.5285,
  "spread_bps": 377.4,
  "bids_top3": [[0.52, 5000], [0.51, 4500], [0.50, 3000]],
  "asks_top3": [[0.54, 3200], [0.55, 2800], [0.56, 2700]]
}
```

**Ticks, not snapshots.** This is a continuous per-token stream. Any analysis
that periodically samples books and then joins across tokens will splice
together quotes that never coexisted, and will manufacture arbitrage that was
never there. That mistake produced 4,667 phantom T0 opportunities in this
project.

Retention is bounded by `DATA_TICKS_RETENTION_DAYS` and `DATA_TICKS_MAX_GB`.

## Replaying directly

The simplest form — feed ticks back through the live components:

```python
import json
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.edge_engine import EdgeEngine

store = EnhancedBookStore()
engine = EdgeEngine(min_edge_bps=100)

with open("data/ticks/2026-04-11.ndjson") as f:
    for line in f:
        tick = json.loads(line)
        bids = [(p, s) for p, s in tick["bids_top3"]]
        asks = [(p, s) for p, s in tick["asks_top3"]]
        store.update_by_token_id(tick["token_id"], bids, asks, tick["ts_ms"])
        decision = engine.evaluate(store)
        if decision.direction != "NONE":
            print(f"[{tick['ts_ms']}] {decision.direction} edge={decision.edge_bps:.0f}bps")
```

Reusing the production classes rather than reimplementing them is deliberate: a
replay that reimplements the decision logic tests the reimplementation.

## The backtest runner

```bash
# Build a dataset from recorded ticks
python scripts/build_backtest_datasets.py \
  --ticks-dir data/ticks --output-root data/backtest --prefix current

# Run
python -m research.backtest.run --dataset default
python -m research.backtest.run --strategy logical-constraint --dataset default
python -m research.backtest.run --strategy event-calendar     --dataset default
python -m research.backtest.run --strategy wallet-alpha       --dataset default
```

`research/backtest/` is a separate package from `research_signal/` — the former
is the offline runner, the latter is the live research-signal layer.

Full option set, as used for a real sensitivity run:

```bash
python -m research.backtest.run \
  --strategy logical-constraint \
  --dataset current_quant_sample \
  --dotenv-path data/backtest/current_quant_sample.env \
  --output-dir research/backtest/output/current_quant_sample/logical \
  --execution-model top \
  --holding-period-ms 300000 \
  --max-open-positions 5 \
  --max-total-exposure 100
```

The three optional quant strategies need extra inputs and will report zero
signals on plain tick data — that is expected, not a failure:

| Strategy | Requires |
|---|---|
| `logical-constraint` | `LOGICAL_CONSTRAINTS_JSON` or the file form |
| `event-calendar` | each snapshot row carrying `baseline_probability`, `confidence`, `time_to_event_sec` |
| `wallet-alpha` | `WALLET_ALPHA_PROFILES_JSON` plus `wallet_address` / `action` / `category` on snapshot rows |

There are also offline adapters that reuse the live strategy models to turn
snapshot or observation rows into `StrategySignal` objects, which is convenient
in a notebook before committing to a shadow configuration:
`research.backtest.adapters.LogicalConstraintBacktestAdapter`,
`EventCalendarBacktestAdapter`, `WalletAlphaBacktestAdapter`.

### A worked (negative) example

On `data/backtest/current_quant_sample` — 40 World Cup winner markets, 27 time
steps, with baselines, wallets and logical rules constructed offline and
therefore *not* representative of real alpha:

| Strategy | Fills | Net PnL |
|---|---|---|
| logical-constraint | 30 / 135 | −28.18 |
| event-calendar | 30 / 1080 | −51.80 |
| wallet-alpha | 10 / 400 | −17.27 |

All three are unusable in this sample. The run demonstrates that the runner and
the execution/markout pipeline work — nothing more. That is the correct thing to
conclude from a synthetic-input backtest.

## Making a backtest trustworthy

The controls below exist because their absence produced false positives in this
project.

### Inject latency

Re-run with entries delayed by a realistic amount (start at 1 second) and change
nothing else. Compare surviving PnL.

A strategy that keeps most of its profit is plausible. One that keeps 9% — as
the 15-minute UPDOWN strategy did — was reading prices it could never have
traded at. Make this a gate, not an optional check.

### Resolve fees per market

`BACKTEST_SLIPPAGE_BPS` covers slippage, not fees. Fees are
`rate · p · (1-p)` with a per-market `rate`; the default `0.005` understates
crypto Up/Down markets by 14×. Re-derive with
`scripts/verify_polymarket_fees.py` and make sure the backtest uses the
`for_market` rate.

### Exit at executable prices

Exits must be priced at the bid side / VWAP you could actually have hit, never
at mid. Pricing an exit at mid quietly credits half the spread on every trade.

### De-duplicate before scoring

If several rows share one settlement outcome — three variants of the same
market, several snapshots of one position — betting per row counts one bet many
times. Aggregate to the settlement unit first. In the TypeSafe evaluation only
the `dedup=market` rows were meaningful.

### Look at the distribution, not the total

A positive total built from one outlier is not an edge. For the TypeSafe run at
`dedup=market, θ=0.10`: 40 bets, 25% hit rate, median −0.0151 per unit, only 10
of 40 positive. Removing the single best trade cut the total from +1.77 to
+0.90, and the other variant went negative. Always report median per-trade
result and the fraction of winners alongside the sum.

### Sample size

Separating a real positive expectancy from noise takes on the order of 500–1000
independent trades. Below that, both the sign and the magnitude of your result
are noise.

## Weather sub-strategy

The weather path parses contract wording, pulls Open-Meteo GFS ensemble
forecasts, and estimates the probability of a temperature threshold or range. It
emits ordinary `BUY_YES` / `BUY_NO` signals into the T2 execution and exit
managers, with edge recomputed from book VWAP at entry.

```dotenv
WEATHER_STRATEGY_ENABLED=true
ARB_DRY_RUN=true
WEATHER_MIN_EDGE=0.10
WEATHER_MIN_CONFIDENCE=0.70
```

Any evaluation of it must use **lead-honest forecasts**: each decision may only
use the forecast vintage published before the decision time, never the current
best forecast for that date. Making this correction flipped the strategy from
apparently positive to negative, with a Brier score worse than the market's own
prices. Keep it in dry-run.
