[English](../en/operations.md) · [中文](../zh/operations.md)

# Operations

## Install

Python 3.10+ (3.11 or newer recommended).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # base + AI extras
```

Requirements are split so you can install only what you need:

| File | Contents |
|---|---|
| `requirements-base.txt` | Runtime: CLOB clients, websockets, aiohttp, FastAPI |
| `requirements-ai.txt` | LLM providers (`openai`, `anthropic`, `httpx`) |
| `requirements-dev.txt` | Test tooling (`pytest`) |
| `requirements-legacy-v1.txt` | Optional legacy V1 CLOB client (fallback only) |
| `requirements.txt` | Umbrella: base + AI |

`py-clob-client-v2` is range-pinned (`>=1.0.0,<2.0.0`) because
`client_factory._force_py_clob_http1` replaces the SDK's module-private
`_http_client`. Any client upgrade requires re-validating
`tests/test_client_factory.py`. The V1 `py-clob-client` is no longer installed by
default — Polymarket hard-cut to CLOB V2 on 2026-04-28 — and lives in
`requirements-legacy-v1.txt` (or `pip install .[legacy-v1]`) for the fallback
path only.

## Configure

```bash
cp .env.example .env
$EDITOR .env
```

See [configuration.md](configuration.md). The minimum for a dry run is nothing
at all — the defaults scan without a wallet.

## Run

```bash
# Dry run (default): scan, never submit
python run_arb_bot.py

# Equivalent as a module
python -m polymarket_arb.main_loop

# Research signal layer alone — no wallet required
python run_research.py --limit 20 --show-markets
python run_research.py --query btc --json
python -m research_signal.refresh --limit 10

# Minimal backtest runner
python -m research.backtest.run --dataset default
```

`run_research.py` is the fastest way to check the research layer in isolation:
it pulls active markets, filters by `--query`, and prints the aggregate report
with source distribution and cache-hit status. `--json` emits structured output
for offline analysis.

## Sidecar workers

The recommended steady state is four processes running continuously — shadow,
live, scanner, promoter — with live only ever consuming validated data.

One command brings up the whole chain:

```bash
python scripts/run_automated_quant_pipeline.py --dotenv-path .env
```

This starts a single bot main loop plus several sidecar data processes. It does
not copy your `.env`. LLM calls happen only in the sidecars.

| Process | Role |
|---|---|
| `bot` | Main loop. Reads `.env`; in live mode runs an internal shadow-only lane for unpromoted wallets. |
| `logical-rules-auto` | Pulls same-event candidate relations from Gamma, has the LLM keep only genuine containment/upper-bound relations, refreshes `logical_constraints.json`. |
| `event-baselines-auto` | Pulls time-bounded events from Gamma, estimates independent baselines, refreshes `event_baselines.json`. |
| `wallet-scanner` | Discovers active wallets from the Polymarket Data API, refreshes `wallet_observations.json`. |
| `wallet-markout-scanner` | Builds wallet markout samples from shadow telemetry. |
| `wallet-promoter` | Promotes only wallets passing min-trades / ROI / concentration / drawdown into `wallet_profiles.json`. |
| `research-feeds-auto` | Proposes RSS feeds for current hot markets, probes each with a real GET, writes the survivors to `RESEARCH_SIGNAL_FEEDS_FILE`. Supports `--seed-feeds-file` so user-supplied seeds persist across cycles and survive LLM drop-out. |

For just the bot plus the two LLM input sidecars:

```bash
python run_bot_with_llm_inputs.py --dotenv-path .env
```

Running pieces individually:

```bash
# Discover wallets continuously
python scripts/scan_quant_strategy_inputs.py auto-wallet-observations \
  --min-trades 3 --min-notional 100 --max-wallets 25 \
  --repeat-interval-sec 120 --repeat-count 0 \
  --output data/quant_inputs/wallet_observations.json

# Promote only validated wallets
python scripts/scan_quant_strategy_inputs.py auto-promote-wallet-profiles \
  --telemetry-dir data/telemetry --lookback-days 7 \
  --min-trades 30 --min-lagged-roi 0.04 \
  --max-concentration 0.35 --max-drawdown 0.35 \
  --repeat-interval-sec 300 --repeat-count 0 \
  --output data/quant_inputs/wallet_profiles.json
```

Observation alone never promotes a wallet. `WALLET_ALPHA_PROFILES_FILE` must
come from the shadow-validated promotion path. Do not enable
`WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED` on a live instance.

Generating input templates and CSV conversion:

```bash
python scripts/quant_strategy_config_template.py --format env

python scripts/build_quant_strategy_inputs.py logical-constraints --input data/logical_constraints.csv
python scripts/build_quant_strategy_inputs.py event-baselines     --input data/event_baselines.csv
python scripts/build_quant_strategy_inputs.py wallet-observations --input data/wallet_observations.csv
```

CSV column contracts:

- `logical-constraints`: `subject_market_id,bound_market_id,relation_type,min_violation_bps,tags,max_size_usdc`
- `event-baselines`: `condition_id,baseline_probability,confidence,time_to_event_sec`
  (the script stamps `generated_at` and the runtime subtracts elapsed time;
  supplying `resolution_at` / `resolution_ts` directly is better)
- `wallet-observations`: `wallet_address,market_id,category,action,observed_size_usdc`

## Dry-run checklist

Work through this in order the first time you bring up the pipeline.

**1. Prepare `.env`**

```dotenv
ARB_DRY_RUN=true
RESEARCH_SIGNAL_ENABLED=true
TICK_RECORD_ENABLED=true
TELEMETRY_RECORD_ENABLED=true
AI_API_KEY=<provider key>
```

**2. Verify the research layer on its own**

```bash
python run_research.py --query btc --show-markets
python run_research.py --query btc --json
```

Expect `source_counts` in the output, and `cache_hit` flipping to `true` on the
second run.

**3. Start bot plus workers**

```bash
python run_bot_with_llm_inputs.py
```

Expect:

- a `Research Signals` card on the dashboard with a count and summary
- `strategy_status.meta.research_overlay` accumulating `applied / boosted /
  penalized / vetoed`
- `data/quant_inputs/research_feeds.json` populated after the first worker round
- `data/quant_inputs/research_feeds_status.json` showing `status=ok`

**4. Offline replay**

```bash
python -m research.backtest.run --dataset default
```

Expect it to run without a wallet key and to emit a report, a trade log, and
recommended parameters.

**5. Only then consider live**

Confirm that research is not systematically inverted, that the overlay is
mostly de-noising rather than vetoing everything, and that risk, execution and
dashboard state are stable. Then read
[research-findings.md](research-findings.md) again and decide whether live is
justified at all — on this codebase's own data it was not.

## Running unattended

### tmux

```bash
cd ~/PolymarketBot
source .venv/bin/activate
tmux new -s arb
python run_arb_bot.py
```

```bash
# Ctrl+b then d to detach; the bot keeps running
tmux ls                  # list sessions
tmux attach -t arb       # reattach
tmux kill-session -t arb # stop
```

### Dashboard access

The dashboard is disabled by default. When enabled it listens on loopback only:

```
http://127.0.0.1:8077
```

**Do not rebind it to `0.0.0.0`.** Reach it with an SSH tunnel from your local
machine:

```bash
ssh -N -L 18077:127.0.0.1:8077 -i /path/to/key.pem user@server
```

```powershell
# Windows PowerShell
ssh -N -L 18077:127.0.0.1:8077 -i C:\path\to\key.pem user@server
```

Then open `http://127.0.0.1:18077` locally.

If you see `channel ... open failed: connect failed: Connection refused`, SSH
worked but nothing is listening on 8077 server-side:

```bash
grep DASHBOARD_ENABLED .env
grep DASHBOARD_PORT .env
ss -lntp | grep 8077
tail -n 50 arb_bot.log
```

## Observability

### Never read the full logs

`arb_bot.log` and the NDJSON telemetry files range from hundreds of kilobytes to
hundreds of megabytes. Reading one whole is both slow and a worse way to find
anything. Always aggregate first, then drill into the window the aggregate
points at.

```bash
# Error code distribution
grep -oE 'code=[A-Z_]+' arb_bot.log | sort | uniq -c | sort -rn

# Skip reasons, ranked
jq -r '.skip_reasons // {} | to_entries[] | .key' \
  data/telemetry/*.strategy_executions.ndjson | sort | uniq -c | sort -rn

# Hourly book-source mix: is WebSocket actually working?
jq -r '[.ts[0:13], .book_stats.ws_hit, .book_stats.cache_hit, .book_stats.rest_fallback] | @tsv' \
  data/telemetry/*.cycle_metrics.ndjson | awk -F'\t' '
    {h[$1]++; ws[$1]+=$2; c[$1]+=$3; r[$1]+=$4}
    END {for (k in h) printf "%s ws=%d cache=%d rest=%d\n", k, ws[k], c[k], r[k]}' | sort

# UPDOWN spot basis rows
jq 'select(.event=="updown_spot_basis")' data/telemetry/*.risk_events.ndjson | head
```

Only after a summary isolates a window or a `trace_id` should you pull raw
lines. One-off analysis scripts belong in `analysis/`.

### The four numbers to watch daily

| Signal | Where | Healthy |
|---|---|---|
| `book_stats.ws_hit` share | `cycle_metrics` | high; a rising `rest_fallback` share means WS subscriptions are being lost |
| `skip_reasons` distribution | `strategy_executions` | stable; a sudden spike in `per_market_rate_cap` or `tier_budget_below_min_order` means capital or cap settings are mismatched |
| `arbs_found_total` vs `t0_opportunities_total` | `cycle_metrics` | the first counts all directional signals, the second only T0 — do not read "0 arbs" as "no signals anywhere" |
| Latency spikes in `timing_stats` | `cycle_metrics` | flat; spikes usually mean REST fallback or rate limiting |

### Standard investigation flow

1. **Scope it** — time window, tier, market, token. If it is ambiguous, pin it
   down before analysing anything.
2. **Summarise** — PnL curve, `skip_reason_counts`, `book_stats` ratios, error
   code counts.
3. **Find the tail** — the hours where PnL jumped, skips spiked,
   `rest_fallback` climbed, or latency peaked.
4. **Drill in** — pull raw lines for that window only, joined by `trace_id` and
   `run_id`.
5. **Attribute** — separate market causes (liquidity dried up, regime change)
   from system causes (bug, latency, rate limit, dropped subscription).
6. **Act** — for parameters, state the change, the expected effect, and the
   risk; for bugs, name `file_path:line_number`.

### Restart semantics

`run_id=run-<pid>-<UTC start>` appears on every log line and telemetry row.
Editing a module changes nothing in a running process. If `run_id` is unchanged
after your edit, you are still looking at the old code.

## Going live

Live trading needs two independent confirmations: `ARB_DRY_RUN=false` **and**
`LIVE_TRADING_ACK=true`. Without both, the process exits at the safety check.

A sane first configuration:

- `LIVE_MAX_ORDER_SIZE_USDC` and `LIVE_MAX_TOTAL_EXPOSURE_USDC` small
- `MAKER_STRATEGY_ENABLED=false` — T3's post-only/GTC behaviour is an extra
  variable you do not want while validating plumbing
- `PORTFOLIO_SYNC_ENABLED=true` and `LIVE_REQUIRE_PORTFOLIO_SYNC=true`
- Recording on, so you can reconstruct anything afterwards

Understand what small capital can and cannot tell you: $10 validates that the
bot submits, cancels, reconnects and handles errors. It cannot validate whether
a strategy is profitable — see
[research-findings.md](research-findings.md#10-cannot-validate-a-strategy).

Two operational notes on V2:

- Polymarket hard-cut to CLOB V2 on 28 April 2026. After any change to account
  funds you must call balance-allowance update (signature type 3).
- Pre-V2 recorded data is not comparable to post-V2 data; anything calibrated on
  it needs re-deriving.

Also be aware that the very restrictive canary template (one position at a time,
`RISK_MAX_TOTAL_EXPOSURE` around $8) means a single orphaned exit stalls the bot
until manual cleanup or a restart. That is the intended trade-off, not a bug.
