# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (Python >=3.10 per pyproject.toml; README recommends 3.11+)
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
pip install -r requirements.txt
# Requirements are split: requirements-base.txt (runtime), requirements-ai.txt
# (LLM providers), requirements-dev.txt (test/lint). The umbrella requirements.txt
# pulls in base + ai. Install requirements-dev.txt separately for contributing.

# Run bot (dry-run by default)
python run_arb_bot.py

# Run as module
python -m polymarket_arb.main_loop

# Research signal layer (no wallet required)
python run_research.py --limit 20 --show-markets
python run_research.py --query btc --json
python -m research_signal.refresh --limit 10

# Backtest runner
python -m research.backtest.run --dataset default

# Tests
pytest
pytest tests/test_fair_value_model.py -v
pytest tests/test_arbitrage_detector.py::TestBinaryArbDetection -v
```

## Architecture

The bot implements a **4-tier strategy system**, each tier executed in priority order:

| Tier | Strategy | Description |
|------|----------|-------------|
| T0 | Structural arbitrage | Binary/multi-outcome: buy all sides when `Σask < 1 - fee` |
| T1 | Cross-platform arbitrage | Polymarket vs Kalshi price divergence |
| T2 | Statistical/model-driven | Bayesian fair value vs market price (edge > threshold) |
| T3 | Market making | Maker orders around model fair value, earns liquidity rewards |

### Execution Flow

```
WebSocket push → OrderBookMirror → EnhancedBookStore (microprice/imbalance)
    │
    ▼ best bid/ask changed?
T0 detection (milliseconds) → VWAP depth verify → Kelly sizing → RiskManager → ExecutionEngine
    │
Periodic scan (5s)
    ├── EdgeEngine: BookStore + VolEstimator + FairValue → edge_bps
    ├── T1 cross-platform scan
    ├── T2 statistical model
    └── T3 market making
    │
StrategyOrchestrator → priority sort → capital allocation → execute
```

**Critical**: T0 structural arbitrage bypasses AI and goes directly to RiskManager — millisecond latency required. AI only intervenes in T2/T3 where 1-3s delay is tolerable.

Before queueing directional signals, `StrategyOrchestrator` applies two adjustments:
- **Research resonance**: ≥3 same-direction signals from diverse sources with sufficient confidence get extra weight; strong cross-source conflict vetoes the signal. Telemetry tracks `applied / boosted / penalized / vetoed` counts under `strategy_status.meta.research_overlay`.
- **Tail-risk discount**: Markets flagged as geopolitical / ceasefire / war / single-decider auto-reduce recommended size and confidence so a Kelly bet on a "high-probability" contract isn't blown up by a black-swan tail.

### Key Modules

- **`config.py`** — `ArbConfig` frozen dataclass, all params loaded from env vars via `dotenv`
- **`main_loop.py`** — Top-level async orchestration; coordinates all subsystems
- **`strategies/strategy_orchestrator.py`** — Priority scheduling and capital allocation across T0-T3
- **`websocket_feed.py`** — WebSocket orderbook mirror; triggers T0 detection on every best bid/ask change
- **`book_store.py`** — `EnhancedBookStore`: maintains live orderbook with derived metrics (microprice, imbalance, spread_bps, depth)
- **`edge_engine.py`** — Unified decision interface; fuses BookStore + VolEstimator + FairValue signals with veto checks
- **`fair_value_model.py`** — Log-normal GBM pricing for spot-anchored UPDOWN markets; multi-signal general fair value
- **`volatility_estimator.py`** — Multi-scale vol: fast (1h) / slow (6h) / adaptive blend; drives T3 spread widening
- **`risk_manager.py`** — Hard guardrails: max positions, per-market exposure, global exposure, daily loss stop, consecutive failure circuit breaker
- **`execution_engine.py`** — Multi-leg atomic order submission with rollback on partial failure
- **`strategies/optimal_stopping.py`** — Finite-horizon Bellman/MDP recursion for exit thresholds and partial take-profit; consumed by T2/AI directional positions to avoid entry-only logic
- **`strategies/t2_exit_manager.py`** — Tracks every T2 fill and emits SELL orders when stop-loss / take-profit / time-stop / optimal-stopping triggers. Without it directional positions ride to settlement; the manager is what turns positive-EV entries into closed PnL
- **`ai_advisor.py`** — Optional LLM layer (`AIAdvisor`); market evaluation, execution decisions, dynamic risk adjustments — all still pass through RiskManager
- **`notifier.py` + `feishu_notifier.py`** — Unified notification routing (trade success/failure, fatal errors, PnL alerts, daily summary) via Feishu app-bot OpenAPI
- **`portfolio_sync.py`** — Low-frequency real-account sync (positions + daily realized PnL → dashboard/risk state); does not touch the high-frequency scan/execute path
- **`research_signal/`** — Collectors / normalizers / scorers package feeding the orchestrator's research overlay (RSS feeds curated by the `research-feeds-llm` worker into `data/quant_inputs/research_feeds.json`). The worker supports `--seed-feeds-file` to persist user-supplied seed feeds across cycles; seeds are validated through the same RSS probe as LLM proposals and survive LLM drop-out
- **`research/backtest/`** — Separate offline backtest runner package (distinct from `research_signal/`); driven by `python -m research.backtest.run`

### AI Layer

`AI_ENABLED=false` means zero overhead. When enabled, `AIAdvisor` uses a `LLMProvider` abstraction supporting OpenAI, Anthropic, DeepSeek, Gemini, and Ollama. Tracks daily cost via `AI_MAX_DAILY_COST_USD`; auto-degrades to read-only on cost overrun or consecutive losses. See `AI_CONFIGURATION.md` for full provider config.

### Capital Allocation (default $1000)

```
T0 structural:  $300 (30%)
T1 cross-platform: $200 (20%)
T2 statistical: $300 (30%)
T3 market making: $200 (20%)
```

### Data & Telemetry

- Tick recording: `TICK_RECORD_ENABLED=true` → `data/ticks/YYYY-MM-DD.ndjson` (rolls at 200MB)
- Strategy signals: `data/telemetry/*.strategy_signals.ndjson` — T1/T2/T3 signals written here even when no T0 arb fires. **Seeing "0 arbs" in T0 does not mean the other tiers have no signals; check this file too.** The cycle metric `arbs_found_total` is the orchestrator's directional-signal count (T0 + T1 + T2 + new logical/event/wallet tiers, not T3 maker quotes); the dedicated `t0_opportunities_total` field on the same row is what isolates T0 structural arbs.
- Cycle metrics: `data/telemetry/*.cycle_metrics.ndjson` — one row per scan cycle with `book_stats` (ws_hit / cache_hit / rest_fallback breakdown), `timing_stats`, exposure, position count.
- Strategy executions: `data/telemetry/*.strategy_executions.ndjson` — entries, exits, and orchestrator-skipped aggregates (`skip_reasons` includes `per_market_rate_cap`, `tier_budget_below_min_order`).
- Risk events: `data/telemetry/*.risk_events.ndjson` — currently also captures `cycle_summary` and `portfolio_sync` rows (i.e., it's a superset, not just trip events).
- Notification state: `data/telemetry/notification_state.json` — daily counts and last-sent timestamps; survives restart.
- Dashboard: FastAPI on `http://127.0.0.1:8077`. Loopback-only by design — do **not** rebind to `0.0.0.0`. Remote access is via SSH port-forward (e.g., `ssh -N -L 18077:127.0.0.1:8077 user@host`).

### Kelly Criterion

All strategies use Quarter-Kelly (`f = 0.25 × f*`) to reduce variance 75% while retaining 75% of expected return. T0/T1 use `win_prob=0.95/0.85`, T2 uses `0.55-0.70`.

## Cross-cutting design contracts

These rules span multiple files and aren't obvious from any single one. Past bugs hit each of them.

### Order type by side

`ExecutionEngine._resolve_execution_order_type` picks `FOK > FAK > GTC` as a fallback chain and uses the result for entries unless the caller passes an explicit order type.

- **T0 multi-leg entries**: must be FOK — partial fills break the structural-arb invariant.
- **T2 single-leg entries**: FOK is fine — a non-fill is just a missed opportunity.
- **T2 exits (`t2_exit_manager._issue_exit`)**: pass FAK explicitly. With FOK, any thin level on the bid side rejects the whole exit, leaving the position open and making partial exits unreachable.
- **T3 maker / GTC**: `_gtc_order_type` is resolved separately and passed through `order_type=` on the maker path.

### Position outcome must be resolved from the trade, not the signal

`t2_exit_manager._resolve_trade_outcome_label` looks up `market.tokens[token_id].outcome` first; the signal-payload `action` is only a fallback. A historical bug bypassed this and defaulted to `BUY_YES`, which silently registered every BUY NO position as a YES holding and made every exit FOK-fail (selling YES we never owned). Never short-circuit this with the signal alone.

### Risk state synchronization windows

- `RiskManager.record_execution` adds exposure for BUY fills and releases booked exposure for SELL fills.
- T2 exits update `pos.size_remaining` inside `T2ExitManager` and call `RiskManager.release_market_exposure` on confirmed SELL fills, so `RISK_MAX_OPEN_POSITIONS` and `RISK_MAX_TOTAL_EXPOSURE` no longer wait for the next portfolio sync to clear.
- `_maybe_reset_daily` zeroes `daily_pnl` at UTC midnight and immediately recomputes `total_pnl = unrealized_pnl`.

### Per-market signal rate cap and log noise

`StrategyOrchestrator._check_per_market_rate_cap` caps T2 (and other `_RATE_CAPPED_TIERS`) signals at `T2_MAX_SIGNALS_PER_MARKET_PER_HOUR=5/h` per market. The upstream T2 collector (`signal_collectors.collect_statistical_strategy_signals`) does **not** know about the cap and re-emits the same signal every scan cycle on stable orderbooks. `StrategySignalTelemetryCompressor` (`main_helpers/signal_telemetry.py`) deduplicates NDJSON writes, and the orchestrator throttles the matching per-market rate-cap log line to keep `arb_bot.log` readable.

### Config defaults vs canary defaults

There are three coexisting env templates:
- `.env.example` — full reference with broad observation defaults
- `polymarket_only_live.env.example` — go-live baseline
- `polymarket_only_canary_10usd.env.example` — $10 small-capital canary (very tight: `RISK_MAX_OPEN_POSITIONS=1`, `RISK_MAX_TOTAL_EXPOSURE=8.0`, `WS_MAX_MARKETS=10`, crypto-only focus keywords)

The canary template is intentionally restrictive — under it the bot can hold one position at a time, so any orphaned exit stalls the bot until manual cleanup or restart. Don't relax these caps without reason; don't assume defaults from `.env.example` apply.

## Configuration

Copy `.env.example` (or `polymarket_only_live.env.example` / `polymarket_only_canary_10usd.env.example`) to `.env`. Key flags:

| Variable | Default | Effect |
|----------|---------|--------|
| `ARB_DRY_RUN` | `true` | Scan only, no real orders |
| `PRIVATE_KEY` | (required for live) | Wallet private key |
| `POLYMARKET_FUNDER` | (required for live) | Proxy/Safe address or deposit wallet address (`POLYMARKET_DEPOSIT_WALLET` is an accepted alias) |
| `POLYMARKET_SIGNATURE_TYPE` | `2` | Use `3` for deposit wallet / `POLY_1271` |
| `ARB_MIN_EDGE_USD` | `0.005` | Minimum net profit threshold |
| `TICK_RECORD_ENABLED` | `false` | Record orderbook ticks for backtesting |
| `AI_ENABLED` | `false` | Enable LLM decision layer |
| `RESEARCH_SIGNAL_ENABLED` | `false` | Enable research signal aggregation |

For observation mode (before going live), broaden scanning with `ARB_MARKET_FOCUS_KEYWORDS=`, `ARB_HOT_MARKET_POOL_SIZE=150`, `T2_MIN_DEVIATION=0.01`. Full parameter reference in `CONFIGURATION.md`.

## Operational notes

- **Run ID and restart**: `arb_bot.log` and every telemetry row carry `run_id=run-<pid>-<UTC start>`. Code edits do not take effect until the bot is restarted; if you change a module and the run_id stays constant, the running process is still on the old code regardless of what's on disk.
- **`py-clob-client` is version-pinned**: `pyproject.toml` pins `py-clob-client==0.34.6` and `py-clob-client-v2>=1.0.0,<2.0.0` because `ExecutionEngine` reaches into the module-private `_http_client` via `_force_py_clob_http1`. Any client upgrade needs `tests/test_client_factory.py` re-validated.
- **Windows is the primary dev environment**: working directory is `E:\AppProject\PolymarketBot` on Windows; tmux instructions in `README.md` are for the Linux deployment target.

## Companion docs

- `CONFIGURATION.md` — full env-var reference
- `AI_CONFIGURATION.md` — LLM provider setup (OpenAI/Anthropic/DeepSeek/Gemini/Ollama)
- `LIVE_TRADING_BASELINE.md` — baseline parameters for going live with small capital
- `SERVER_OBSERVABILITY.md` — log/telemetry watch recommendations for unattended servers
- `EXTERNAL_REFERENCES.md` — external research / data source references
- `PENDING_VALIDATIONS.md` — checklist for graduating shadow-mode features (NearCertaintyRule, dynamic stop, barbell pool, on-chain signals) to live; lists data sources needed and the env flags to flip on each validation

## Testing

Tests live in `tests/` and cover all major modules with mocks for external APIs. The `conftest.py` provides shared fixtures. Run `pytest` from the project root — `pythonpath = ["."]` is set in `pyproject.toml` so no install needed.
