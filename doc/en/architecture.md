[English](../en/architecture.md) · [中文](../zh/architecture.md)

# Architecture

This bot is a multi-tier signal → risk → execution pipeline for Polymarket
prediction markets. It is built around one constraint: **structural arbitrage
must be detected and executed in milliseconds, everything else can take
seconds.** That split determines the whole design.

Before reading this as a "how to make money" document, read
[research-findings.md](research-findings.md). Every tier described here was
tested and failed to produce positive expectancy on real data.

## Strategy tiers

Four execution tiers run in priority order, plus three optional quant signal
sources that only activate when you supply externally validated input.

| Tier | Strategy | Trigger | Latency budget |
|---|---|---|---|
| T0 | Structural arbitrage | WebSocket best bid/ask change | milliseconds |
| T1 | Cross-platform arbitrage | periodic scan | seconds |
| T2 | Statistical / model-driven | periodic scan | seconds |
| T3 | Market making | periodic scan | seconds |

### T0 — structural arbitrage

**Binary markets.** Exactly one of the Yes/No tokens settles at $1.00. When
`ask(Yes) + ask(No) < $1.00 - fee`, buying both sides locks in the difference.

```
fee_per_leg = fee_rate * price * (1 - price)
profit      = 1.00 - ask_yes - ask_no - Σ fee_per_leg
```

**Multi-outcome events.** An event (an election, say) has N mutually exclusive
markets. When `Σ ask_i < 1.00 - Σ fee_i`, buying every outcome locks in the
difference. For `neg_risk` events the detector picks the cheaper of
`min(ask_yes, 1 - bid_no)` per leg.

Detection is WebSocket-driven and verified against VWAP depth before sizing, so
a one-lot quote at a good price does not create a phantom opportunity.

**The single most important implementation detail**: a crossed book
(`bid > ask`, which Polymarket's feed produces transiently) trivially satisfies
`Σ ask < 1` without being an arbitrage at all. See
[research-findings.md](research-findings.md#t0--structural-arbitrage) — on
seven days of real tick data, *every* apparent T0 signal was a crossed book.

### T1 — cross-platform arbitrage

The same real-world event is often priced differently on Polymarket and Kalshi,
because the user bases differ and moving capital between them has friction.

```
poly_yes_ask  = 0.55
kalshi_no_ask = 0.40
total         = 0.95   → 5% gross
```

Risks are structural, not statistical: differing resolution criteria, capital
locked on two venues, and counterparty risk on both. T1 requires an explicit
pairing table (`CROSS_PLATFORM_PAIRS_JSON`); the bot never guesses that two
markets are the same event. A pair-entity consistency check can veto a pair but
never creates one.

### T2 — statistical arbitrage

Rather than waiting for a structural mispricing, T2 fuses several signals into a
model probability and compares it to the market:

```
FairValueModel (log-normal GBM)   ─┐
Order book microprice + imbalance ─┼→ compute_general_fair_value() → model_prob
Price momentum + cross constraints ┘

model_prob = 0.72  vs  market_price = 0.60  →  edge → Kelly sizing
```

Signal sources: order book imbalance over 5 levels, microprice, short-horizon
momentum, cross-market logical constraints, spot anchoring for UPDOWN markets
(BTC/ETH "above X in N minutes"), event-calendar baselines, and wallet-alpha
observations.

Entries are FOK single-leg. Exits are managed by `T2ExitManager`, which is what
turns a positive-expectancy entry into realised PnL — without it, directional
positions simply ride to settlement.

### T3 — market making

Resting limit orders on both sides of model fair value:

- Maker fee is 0%, versus the taker fee, so every filled pair starts ahead.
- Quoting inside the incentive band `[mid - δ, mid + δ]` earns Polymarket
  liquidity rewards.
- Spread is driven by `VolEstimator`: `base + sigma_blend × 2 + inventory_skew`,
  so it widens automatically in volatile regimes.

T3 also ships anti-sniping protections (jump pause, stability confirmation,
filtering, post-fill cooldown, chase limits) and a `/orders-scoring` audit that
reports whether resting orders are actually earning rewards.

## Execution flow

```
WebSocket order book push
        │
        ▼
OrderBookMirror update ──→ EnhancedBookStore (microprice / imbalance / depth)
        │
        ▼
  best bid/ask changed?
        │ yes
        ▼
T0 detection (milliseconds)
  ├─ binary:        ask_yes + ask_no < 1 - fee?
  └─ multi-outcome: Σ ask_i      < 1 - Σ fee?
        │
        └─ hit → VWAP depth verify → Kelly sizing → RiskManager → ExecutionEngine

Periodic scan (default 5s)
  ├─ EdgeEngine: BookStore + VolEstimator + FairValue → edge_bps → veto checks
  ├─ T1 cross-platform spread
  ├─ T2 statistical deviation vs threshold
  └─ T3 maker quote refresh
        │
        ▼
StrategyOrchestrator → priority sort → capital allocation → execute
        │
        ▼
Dashboard + notifications
```

T0 goes **directly** to `RiskManager`. No tier consults an LLM on the hot path.

## Module map

```
polymarket_arb/
├── config.py                  # ArbConfig frozen dataclass, all env-driven
├── client_factory.py          # CLOB read-only / trading client factory
├── models.py                  # ArbOpportunity, OrderBookSnapshot, ...
├── utils_time.py              # ms timestamps, interval alignment
├── book_store.py              # EnhancedBookStore: microprice/imbalance/depth
├── fair_value_model.py        # log-normal GBM + multi-signal fair value
├── volatility_estimator.py    # fast 1h / slow 6h / adaptive blend sigma
├── edge_engine.py             # unified edge decision + veto checks
├── market_scanner.py          # Gamma API market/event universe
├── orderbook_analyzer.py      # VWAP execution price from depth
├── arbitrage_detector.py      # T0 binary + multi-outcome + neg_risk
├── websocket_feed.py          # order book mirror, drives T0 on every change
├── user_feed.py               # user channel WS: own fills without polling
├── execution_engine.py        # multi-leg atomic submit with rollback
├── risk_manager.py            # exposure / loss / circuit-breaker guardrails
├── portfolio_sync.py          # low-frequency real-account reconciliation
├── rtds_feed.py, spot_feed.py # spot price sources for UPDOWN pricing
├── typesafe_provider.py       # TypeSafe Jev scoring client (sidecar only)
├── ai_provider.py             # LLMProvider abstraction (sidecar only)
├── quant_input_store.py       # hot-reload of data/quant_inputs/*.json
├── notifier.py                # notification routing
├── feishu_notifier.py         # Feishu app-bot OpenAPI transport
├── dashboard_api.py           # FastAPI monitoring backend (loopback only)
├── main_loop.py               # top-level async orchestration
└── strategies/
    ├── kelly.py                    # Quarter-Kelly sizing
    ├── optimal_stopping.py         # Bellman exit thresholds / scale-out
    ├── cross_platform.py           # T1
    ├── statistical_model.py        # T2
    ├── t2_exit_manager.py          # T2 exits: stop / target / time / Bellman
    ├── logical_constraints.py      # optional: containment / upper-bound rules
    ├── event_calendar_model.py     # optional: external baseline probabilities
    ├── wallet_alpha.py             # optional: validated-wallet following
    ├── sniper_gate.py              # high-confidence / low-correlation filter
    ├── maker_strategy.py           # T3
    └── strategy_orchestrator.py    # priority scheduling + capital allocation

research/backtest/    offline backtest runner (runner, execution model, reports)
research_signal/      research signal collectors / normalizers / scorers
analysis/             one-off empirical studies (see analysis/README.md)
scripts/              sidecar workers and verification entrypoints
```

## Core design decisions

### WebSocket push, not REST polling

```
REST polling: mean latency = scan_interval / 2 ≈ 2.5s
WebSocket:    latency ≈ network RTT ≈ 10-50ms
```

`OrderBookMirror` keeps an in-memory copy of the book and fires a callback
whenever best bid or ask moves, which goes straight into T0 detection. A 2.5s
average delay means any structural opportunity is long gone.

### Quarter-Kelly sizing

Position size is computed, not fixed:

```
f* = (p·b - q) / b          classic Kelly
f  = 0.25 × f*              what we actually use
```

Quarter-Kelly cuts variance ~75% while keeping ~75% of expected growth.

| Strategy | win_prob | kelly_fraction | max_bet_pct |
|---|---|---|---|
| Structural (T0) | 0.95 | 0.25 | 10% |
| Cross-platform (T1) | 0.85 | 0.25 | 10% |
| Statistical (T2) | 0.55-0.70 | 0.25 | 5% |

### Optimal stopping

`strategies/optimal_stopping.py` solves a finite-horizon Bellman recursion for
the exit boundary:

```
V_τ(m) = max( m, E[ V_{τ-1}(m') ] )
```

Given remaining time, current token price, the model's terminal probability, and
a Markov transition matrix, it returns HOLD/STOP plus scale-out thresholds. This
is what keeps T2/T3 directional positions from being entry-only logic.

### Orchestrator overlays

Before a directional signal is queued, `StrategyOrchestrator` applies two
adjustments:

- **Research resonance** — three or more same-direction signals from diverse
  sources with sufficient confidence get extra weight; a strong cross-source
  conflict vetoes the signal. Counts are reported under
  `strategy_status.meta.research_overlay` as `applied / boosted / penalized /
  vetoed`.
- **Tail-risk discount** — markets classified as geopolitical, ceasefire, war,
  or single-decider automatically get reduced size and confidence, so a Kelly
  bet on a "97% certain" contract is not sized as if the tail did not exist.

### Capital allocation

Default split of a $1000 book (configurable; T1 is off for a Polymarket-only
deployment):

```
T0 structural     35%
T1 cross-platform 10%
T2 statistical    35%
T3 market making  20%
```

## Risk controls

| Rule | Parameter | Effect |
|---|---|---|
| Max open positions | `RISK_MAX_OPEN_POSITIONS` | no new entries past the cap |
| Per-market exposure | `RISK_MAX_EXPOSURE_PER_MARKET` | USDC cap per `condition_id` |
| Global exposure | `RISK_MAX_TOTAL_EXPOSURE` | total cost basis cap |
| Daily loss stop | `RISK_MAX_DAILY_LOSS` | halts trading for the day |
| Consecutive failure breaker | `RISK_MAX_CONSECUTIVE_FAILURES` | halts after N failed executions |
| Market cooldown | 60s | no repeat execution on the same event |
| Leg rollback | automatic | a failed leg triggers cancellation of submitted legs |

## Cross-cutting contracts

These span multiple files and are not visible from any one of them. Each one
corresponds to a bug that actually happened.

### Order type is a function of side, not of convenience

`ExecutionEngine._resolve_execution_order_type` resolves `FOK > FAK > GTC` as a
fallback chain and uses the result for entries unless the caller passes an
explicit type.

- **T0 multi-leg entries must be FOK.** A partial fill breaks the structural
  invariant — you end up long three of five outcomes.
- **T2 single-leg entries** are fine as FOK; a non-fill is just a missed trade.
- **T2 exits must pass FAK explicitly** (`t2_exit_manager._issue_exit`). Under
  FOK, one thin level on the bid side rejects the entire exit, the position
  stays open, and partial exits become unreachable.
- **T3 maker / GTC** resolves separately via `_gtc_order_type` and is passed
  through `order_type=` on the maker path.

### Position outcome comes from the trade, not the signal

`t2_exit_manager._resolve_trade_outcome_label` looks up
`market.tokens[token_id].outcome` first; the signal payload's `action` is only a
fallback. A historical bug skipped this and defaulted to `BUY_YES`, which
silently recorded every BUY NO position as a YES holding — so every exit was an
attempt to sell YES tokens the account never held, and FOK-failed. Never
short-circuit this with the signal alone.

### Risk state synchronisation windows

- `RiskManager.record_execution` adds exposure on BUY fills and releases booked
  exposure on SELL fills.
- T2 exits update `pos.size_remaining` inside `T2ExitManager` and call
  `RiskManager.release_market_exposure` on confirmed SELL fills, so
  `RISK_MAX_OPEN_POSITIONS` and `RISK_MAX_TOTAL_EXPOSURE` clear immediately
  rather than waiting for the next portfolio sync.
- `_maybe_reset_daily` zeroes `daily_pnl` at UTC midnight and immediately
  recomputes `total_pnl = unrealized_pnl`.

### Per-market signal rate cap

`StrategyOrchestrator._check_per_market_rate_cap` caps T2 (and other rate-capped
tiers) at `T2_MAX_SIGNALS_PER_MARKET_PER_HOUR` per market. The upstream T2
collector does not know about the cap and re-emits the same signal every scan
cycle on a stable book, so `StrategySignalTelemetryCompressor`
(`main_helpers/signal_telemetry.py`) deduplicates the NDJSON writes and the
orchestrator throttles the matching log line. Otherwise both the telemetry file
and `arb_bot.log` become unreadable within hours.

## Telemetry

| File | Contents |
|---|---|
| `data/ticks/YYYY-MM-DD.ndjson` | Raw book updates, rolls at 200MB (`TICK_RECORD_ENABLED`) |
| `data/telemetry/*.cycle_metrics.ndjson` | One row per scan cycle: `book_stats` (ws_hit / cache_hit / rest_fallback), `timing_stats`, exposure, position count |
| `data/telemetry/*.strategy_signals.ndjson` | T1/T2/T3 and quant-tier signals, written even when T0 finds nothing |
| `data/telemetry/*.strategy_executions.ndjson` | Entries, exits, and orchestrator skip aggregates (`skip_reasons`) |
| `data/telemetry/*.risk_events.ndjson` | Risk trips plus `cycle_summary` and `portfolio_sync` rows |
| `data/telemetry/notification_state.json` | Daily counts and last-sent timestamps, survives restart |

Two metrics are routinely confused: `arbs_found_total` on a cycle row is the
orchestrator's *directional* signal count (T0 + T1 + T2 + logical/event/wallet
tiers, excluding T3 maker quotes), while `t0_opportunities_total` on the same
row isolates T0 structural arbitrage. Seeing "0 arbs" in T0 does not mean the
other tiers are silent — check `strategy_signals` too.

Every log line and telemetry row carries `run_id=run-<pid>-<UTC start>`. Code
edits do not take effect until the process restarts; if you change a module and
`run_id` is unchanged, the running process is still on the old code regardless
of what is on disk.
