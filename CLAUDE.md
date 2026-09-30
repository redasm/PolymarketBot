# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Read this first

Every strategy tier in this repository was empirically falsified — see
[doc/en/research-findings.md](doc/en/research-findings.md). Do not write
documentation, commit messages, or user-facing text that implies the bot is
profitable. When a change is justified by a performance claim, the claim has to
survive the checks in
[doc/en/backtesting.md](doc/en/backtesting.md#making-a-backtest-trustworthy).

## Commands

```bash
# Setup (Python >=3.10; 3.11+ recommended)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # base + ai; add requirements-dev.txt to contribute

# Run the bot (dry-run by default)
python run_arb_bot.py
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

## Architecture summary

Four execution tiers in priority order. Full detail in
[doc/en/architecture.md](doc/en/architecture.md).

| Tier | Strategy |
|---|---|
| T0 | Structural arbitrage — buy all sides when `Σask < 1 - fee` |
| T1 | Cross-platform arbitrage — Polymarket vs Kalshi |
| T2 | Statistical / model-driven — Bayesian fair value vs market price |
| T3 | Market making — maker quotes around model fair value |

```
WebSocket push → OrderBookMirror → EnhancedBookStore
    │ best bid/ask changed?
    ▼
T0 detection (ms) → VWAP verify → Kelly → RiskManager → ExecutionEngine

Periodic scan (5s) → EdgeEngine / T1 / T2 / T3
    → StrategyOrchestrator → priority sort → capital allocation → execute
```

**Critical**: T0 goes directly to `RiskManager` — millisecond latency required.
No tier consults an LLM on the hot path. LLM work happens out-of-process via
`scripts/scan_quant_strategy_inputs.py`, landing in the loop as hot-reloaded
quant-input JSON.

### Key modules

- `config.py` — `ArbConfig` frozen dataclass, all params from env via `dotenv`
- `main_loop.py` — top-level async orchestration
- `strategies/strategy_orchestrator.py` — priority scheduling, capital allocation, research-resonance and tail-risk overlays
- `websocket_feed.py` — order book mirror; triggers T0 on every best quote change
- `book_store.py` — `EnhancedBookStore`: microprice, imbalance, spread_bps, depth
- `edge_engine.py` — fuses BookStore + VolEstimator + FairValue, applies veto checks
- `fair_value_model.py` — log-normal GBM for spot-anchored UPDOWN; multi-signal general fair value
- `volatility_estimator.py` — fast 1h / slow 6h / adaptive blend
- `risk_manager.py` — positions, per-market and global exposure, daily loss stop, failure breaker
- `execution_engine.py` — multi-leg atomic submission with rollback
- `strategies/optimal_stopping.py` — finite-horizon Bellman exit thresholds and scale-out
- `strategies/t2_exit_manager.py` — turns positive-EV entries into closed PnL; without it directional positions ride to settlement
- `ai_provider.py` — `LLMProvider` abstraction. **Not wired into the trading loop**
- `portfolio_sync.py` — low-frequency real-account sync; never on the hot path
- `research_signal/` — collectors / normalizers / scorers feeding the research overlay
- `research/backtest/` — offline backtest runner, distinct from `research_signal/`

## Cross-cutting design contracts

These span multiple files and are not visible from any one of them. Each
corresponds to a bug that actually happened. Full text in
[doc/en/architecture.md](doc/en/architecture.md#cross-cutting-contracts).

- **Order type by side** — T0 multi-leg entries must be FOK; T2 exits must pass
  FAK explicitly (FOK rejects the whole exit on one thin bid level, stranding
  the position); T3 maker resolves `_gtc_order_type` separately.
- **Position outcome from the trade, not the signal** —
  `t2_exit_manager._resolve_trade_outcome_label` reads
  `market.tokens[token_id].outcome` first. Defaulting to the signal's `BUY_YES`
  silently recorded every BUY NO as a YES holding and FOK-failed every exit.
- **Risk state synchronisation** — T2 exits release exposure on confirmed SELL
  fills rather than waiting for the next portfolio sync; `_maybe_reset_daily`
  zeroes `daily_pnl` at UTC midnight.
- **Per-market rate cap and log noise** — the T2 collector re-emits the same
  signal every cycle on a stable book, so the telemetry compressor and the
  orchestrator log throttle are load-bearing, not cosmetic.

## Data and telemetry

- `data/ticks/YYYY-MM-DD.ndjson` — `TICK_RECORD_ENABLED=true`, rolls at 200MB
- `data/telemetry/*.strategy_signals.ndjson` — T1/T2/T3 signals, written even
  when T0 finds nothing. "0 arbs" in T0 does not mean other tiers are silent.
- `data/telemetry/*.cycle_metrics.ndjson` — one row per cycle with `book_stats`
  (ws_hit / cache_hit / rest_fallback), `timing_stats`, exposure, position count.
  `arbs_found_total` counts all directional signals; `t0_opportunities_total`
  isolates T0.
- `data/telemetry/*.strategy_executions.ndjson` — entries, exits, `skip_reasons`
- `data/telemetry/*.risk_events.ndjson` — risk trips plus `cycle_summary` and
  `portfolio_sync` rows
- Dashboard: FastAPI on `http://127.0.0.1:8077`, **loopback only by design**.
  Do not rebind to `0.0.0.0`; use an SSH port-forward.

## Operational notes

- **Run ID and restart**: every log line and telemetry row carries
  `run_id=run-<pid>-<UTC start>`. Code edits do not take effect until restart;
  if `run_id` is unchanged, the process is on the old code.
- **`py-clob-client-v2` is range-pinned (`<2.0`)** because
  `client_factory._force_py_clob_http1` replaces the SDK's module-private
  `_http_client`. Any upgrade needs `tests/test_client_factory.py` re-validated.
  Live orders go through the V2 client (`ExecutionEngine._submit_order_v2`); the
  V1 `py-clob-client` is an optional legacy extra (`pip install .[legacy-v1]`)
  and only reached if V2 is not installed.

## Working with Claude

Personal preferences (reply language, tone, analysis routine) belong in a
gitignored `CLAUDE.local.md`, not here.

### Never read full logs raw

`arb_bot.log` and the NDJSON telemetry files run from hundreds of KB to hundreds
of MB. Reading them whole burns context and produces shallower analysis.

- Filter and aggregate first with `grep`, `jq`, `pandas`, or a one-off script in
  `analysis/` (one-offs belong there — don't gate on a "proper home").
- Feed Claude **summaries** — error code distribution, hourly aggregates,
  typical and extreme samples joined by `trace_id`.
- Only pull raw lines once a summary points at a specific window or `trace_id`.

Anti-example: `cat data/telemetry/*.cycle_metrics.ndjson | head -2000`.
Correct: `python -c "import json…"` → summary table → drill into the anomaly.

### Plan before coding when it's not trivial

Use plan mode and wait for confirmation before any of:

- Touching signal / sizing / risk / execution core logic
- Multi-file changes (≥3 files)
- New dependencies or data-schema changes
- **Anything affecting order submission** — no exceptions

Trivial bug fixes, typos, single-function edits → just do it.

### Production-grade defaults

This is not a demo. Every change assumes:

- Exception coverage on network / API / partial-fill / reconnect paths
- Structured logs on hot paths with `trace_id` continuity
- Edit → unit tests → backtest (when applicable) → merge

### Default behaviors to AVOID

- Don't propose new dependencies unprompted. A new package needs a case for
  why the standard library and existing dependencies are not enough.
- When fixing a bug, touch only the relevant code. **No drive-by refactors.**
- Don't suggest thresholds without first looking at the actual data
  distribution. Summary first, then tune.
- Don't assume market state. Never assert live prices ("BTC is at $XX") unless
  the user supplied current data.
- Never bind the dashboard to `0.0.0.0` (loopback only).
- Never describe any strategy as validated or profitable. All four tiers were
  empirically falsified.

## Testing

Tests live in `tests/` and cover all major modules with mocks for external APIs.
`conftest.py` provides shared fixtures. Run `pytest` from the repository root —
`pythonpath = ["."]` is set in `pyproject.toml`, so no install is needed.

## Documentation

`doc/en/` and `doc/zh/` have matching section structure. Change both, or state
in the PR which one is now behind. Start from
[doc/README.md](doc/README.md).
