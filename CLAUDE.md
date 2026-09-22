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
- **`py-clob-client` is version-pinned** because `ExecutionEngine` reaches into
  the module-private `_http_client` via `_force_py_clob_http1`. Any upgrade
  needs `tests/test_client_factory.py` re-validated.

## Working with Claude

### Language and tone

- **Reply in Chinese.** Code, identifiers and log strings stay English;
  explanations and reports are Chinese.
- 不要开场白（"好的我来分析…"）。直接给结论，证据放后。分析报告用 markdown 表格而非散文。
- 不确定时直说"我不确定 X"或"需要看 Y 才能下结论"。**量化领域瞎猜的代价很高，宁可让用户多答一问。**

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

- 不要主动建议加依赖。新增 package 必须先论证标准库 / 已有依赖为什么不够用。
- 修 bug 时只动相关代码。**不要顺手重构无关模块。**
- 没看过实际数据分布就不要给阈值建议。先 summary，再调参。
- 不要假设市场状态。除非用户提供实时数据，不要写出"现在 BTC 在 $XX"这类断言。
- 不要在 dashboard 绑定 `0.0.0.0`（loopback only）。
- 不要把任何策略描述成"已验证可盈利"。全部四层已被实证证伪。

### Standard analysis flow

用户说"看一下 X 时段 / X 策略的表现"时，默认按此走：

1. **明确范围** — 时间窗 / 策略 tier / market / token；模糊就反问，**不要瞎猜**。
2. **跑 summary** — PnL 曲线、`skip_reason_counts` 分布、`book_stats` 比例、错误码统计。
3. **找异常点** — PnL 突变、skip 突增、`rest_fallback` 占比飙升、延迟尖峰。
4. **深挖样本** — 只针对异常窗口拉具体日志（带 `trace_id` / `run_id` 串联）。
5. **归因** — 区分**市场原因**（行情结构变化、流动性下降）vs **系统原因**（bug、延迟、限流、订阅丢失）。
6. **给建议** — 参数问题：给具体调整方向 + 预期影响 + 风险；bug：直接定位到 `file_path:line_number`。

## Testing

Tests live in `tests/` and cover all major modules with mocks for external APIs.
`conftest.py` provides shared fixtures. Run `pytest` from the repository root —
`pythonpath = ["."]` is set in `pyproject.toml`, so no install is needed.

## Documentation

`doc/en/` and `doc/zh/` have matching section structure. Change both, or state
in the PR which one is now behind. Start from
[doc/README.md](doc/README.md).
