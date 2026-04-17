# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
pip install -r requirements.txt

# Run bot (dry-run by default)
python run_arb_bot.py

# Run as module
python -m polymarket_arb.main_loop

# Research signal layer (no wallet required)
python run_research.py --limit 20 --show-markets
python run_research.py --query btc --json

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
- **`ai_advisor.py`** — Optional LLM layer (`AIAdvisor`); market evaluation, execution decisions, dynamic risk adjustments — all still pass through RiskManager

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
- Strategy signals: `data/telemetry/*.strategy_signals.ndjson` — T1/T2/T3 signals written here even when no T0 arb fires
- Dashboard: FastAPI on `http://127.0.0.1:8077` (SSH tunnel for remote access)

### Kelly Criterion

All strategies use Quarter-Kelly (`f = 0.25 × f*`) to reduce variance 75% while retaining 75% of expected return. T0/T1 use `win_prob=0.95/0.85`, T2 uses `0.55-0.70`.

## Configuration

Copy `.env.example` (or `polymarket_only_live.env.example`) to `.env`. Key flags:

| Variable | Default | Effect |
|----------|---------|--------|
| `ARB_DRY_RUN` | `true` | Scan only, no real orders |
| `PRIVATE_KEY` | (required for live) | Wallet private key |
| `POLYMARKET_FUNDER` | (required for live) | Proxy wallet address |
| `ARB_MIN_EDGE_USD` | `0.005` | Minimum net profit threshold |
| `TICK_RECORD_ENABLED` | `false` | Record orderbook ticks for backtesting |
| `AI_ENABLED` | `false` | Enable LLM decision layer |
| `RESEARCH_SIGNAL_ENABLED` | `false` | Enable research signal aggregation |

For observation mode (before going live), broaden scanning with `ARB_MARKET_FOCUS_KEYWORDS=`, `ARB_HOT_MARKET_POOL_SIZE=150`, `T2_MIN_DEVIATION=0.01`. Full parameter reference in `CONFIGURATION.md`.

## Testing

Tests live in `tests/` and cover all major modules with mocks for external APIs. The `conftest.py` provides shared fixtures. Run `pytest` from the project root — `pythonpath = ["."]` is set in `pyproject.toml` so no install needed.
