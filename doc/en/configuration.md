[English](../en/configuration.md) · [中文](../zh/configuration.md)

# Configuration reference

All configuration is environment variables, loaded from `.env` via
`python-dotenv` into a frozen `ArbConfig` dataclass (`polymarket_arb/config.py`).
Nothing is read from a config file at runtime, and nothing is mutated after
startup — **changing `.env` requires a restart**, with the exception of the
hot-reloaded quant input JSON files described below.

```bash
cp .env.example .env
$EDITOR .env
```

`.env.example` is the authoritative, exhaustively commented template (its inline
comments are in Chinese). This document is the English reference for the same
settings, grouped the same way.

> **Never commit `.env`.** It is in `.gitignore`, along with `.env.*` except the
> example. A leaked `PRIVATE_KEY` means a drained wallet, immediately and
> irreversibly.

## Verifying what is actually loaded

Every log line and telemetry row carries `run_id=run-<pid>-<UTC start>`. If you
change a variable and `run_id` has not changed, the running process has not
picked it up.

---

## Wallet and authentication

| Variable | Default | Meaning |
|---|---|---|
| `PRIVATE_KEY` | — | Wallet private key. Required for live trading. |
| `POLYMARKET_FUNDER` | — | Address holding the funds. Legacy users: the proxy/Safe address from Polymarket settings. Deposit-wallet users: the deposit wallet address. |
| `POLYMARKET_DEPOSIT_WALLET` | *(empty)* | Alias for `POLYMARKET_FUNDER`; used as funder when set. |
| `POLYMARKET_SIGNATURE_TYPE` | `2` | `0` EOA / `1` Magic-Email / `2` Browser or Gnosis Safe / `3` Deposit Wallet (`POLY_1271`). |

## CLOB / Gamma endpoints

| Variable | Default | Meaning |
|---|---|---|
| `CLOB_HOST` | `https://clob.polymarket.com` | CLOB API base. |
| `GAMMA_HOST` | `https://gamma-api.polymarket.com` | Market/event metadata API base. |
| `CHAIN_ID` | `137` | Polygon mainnet. |
| `POLYMARKET_CLOB_CLIENT_VERSION` | `auto` | `auto` prefers `py-clob-client-v2` and falls back to the v1 client. Deposit wallets require `auto` or `v2`. |
| `CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASSPHRASE` | *(empty)* | Optional pre-created L2 credentials. When empty the client derives or creates them from `PRIVATE_KEY`. |

## Arbitrage and scanning

| Variable | Default | Meaning |
|---|---|---|
| `ARB_DRY_RUN` | `true` | Scan only, never submit orders. |
| `LIVE_TRADING_ACK` | `false` | Second confirmation. Real orders require `ARB_DRY_RUN=false` **and** this `true`. |
| `ARB_MIN_EDGE_USD` | `0.005` | Minimum net profit per opportunity. |
| `ARB_MIN_EDGE_PCT` | `0.3` | Minimum net profit rate, percent. |
| `ARB_MAX_ORDER_SIZE_USDC` | `50.0` | Per-order ceiling. |
| `ARB_DEFAULT_ORDER_SIZE_USDC` | `10.0` | Default order size. |
| `ARB_SCAN_INTERVAL_SEC` | `5` | Periodic scan interval. |
| `DIRTY_MARKET_WAKE_THRESHOLD` | `1` | Number of markets with a best-quote change needed to interrupt the scan wait. `1` = wake on any change (lowest latency). |
| `ARB_MARKET_FETCH_LIMIT` | `100` | Gamma page size. |
| `ARB_MARKET_UNIVERSE_REFRESH_SEC` | `600` | Full-universe refresh interval; between refreshes only the hot pool is scanned. |
| `ARB_HOT_MARKET_POOL_SIZE` | `80` | Hot market pool size. |
| `ARB_HOT_EVENT_POOL_SIZE` | `30` | Hot event pool size. |
| `ARB_MARKET_FOCUS_KEYWORDS` | *(varies)* | Comma-separated topic filter. Empty = whole market. |
| `ARB_MIN_LIQUIDITY` | `1000` | Minimum market liquidity, USDC. |
| `ARB_MIN_VOLUME_24H` | — | Minimum 24h volume, USDC. |
| `ARB_MAX_MULTI_OUTCOME_LEGS` | `20` | Skip events with more legs than this. |

Keyword filtering has a sharp edge: crypto Up/Down markets report
`volume24hr ≈ 0` and are dropped by `ARB_MIN_VOLUME_24H` during fetch, so they
are pulled in by direct slug lookup instead (see the UPDOWN settings). A narrow
keyword list combined with volume filtering can silently exclude the exact
markets you intend to study.

### Live-trading safety gates

These only apply when `ARB_DRY_RUN=false`.

| Variable | Default | Meaning |
|---|---|---|
| `LIVE_MAX_ORDER_SIZE_USDC` | `10.0` | Hard per-order cap in live mode. |
| `LIVE_MAX_TOTAL_EXPOSURE_USDC` | `100.0` | Hard total exposure cap in live mode. |
| `LIVE_MIN_NET_EDGE_BPS` | `25` | Post-fee edge floor, basis points. |
| `LIVE_MIN_NET_EDGE_USD` | `0.0025` | Post-fee edge floor, per share. |
| `LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC` | `5` | Reject signals whose book snapshot is older than this. Evaluated **per signal token**, so one idle token cannot stall everything. |
| `LIVE_MIN_WS_HIT_RATIO` | `0.25` | Refuse to trade when recent WebSocket book-hit ratio falls below this. |
| `LIVE_REQUIRE_PORTFOLIO_SYNC` | `true` | Require a successful account sync before opening positions. |
| `LIVE_ALLOW_ZERO_TAKER_FEE` | `false` | Explicit acknowledgement required to run with a zero fee assumption. |

## Risk management

| Variable | Default | Meaning |
|---|---|---|
| `RISK_MAX_OPEN_POSITIONS` | `10` | Concurrent position cap. |
| `RISK_MAX_EXPOSURE_PER_MARKET` | `100.0` | Per-`condition_id` USDC cap. |
| `RISK_MAX_TOTAL_EXPOSURE` | `500` | Global cost-basis cap. |
| `RISK_MAX_DAILY_LOSS` | `50` | Daily loss stop. T0 BINARY/MULTI_OUTCOME bypasses this check; DIRECTIONAL signals do not. |
| `RISK_MAX_CONSECUTIVE_FAILURES` | — | Circuit breaker threshold. |
| `RISK_MARKET_COOLDOWN_SEC` | `60` | Minimum gap between two executions on the same event. |
| `RISK_PENDING_RESERVATION_SEC` | — | How long a pending order holds reserved exposure. Raise to 120–300 if you enable order types that rest for a long time. |
| `RISK_BREAKER_AUTO_RESET_SEC` | — | Seconds without a new failure before the breaker clears. `0` = never auto-clear. |

### Account synchronisation

Low-frequency reconciliation against the real account. It never touches the
high-frequency scan or execution path.

| Variable | Default | Meaning |
|---|---|---|
| `PORTFOLIO_SYNC_ENABLED` | `false` | Sync real positions and realised daily PnL into dashboard/risk state. |
| `PORTFOLIO_SYNC_INTERVAL_SEC` | `60` | Sync period. |
| `PORTFOLIO_SYNC_TIMEOUT_SEC` | `5` | Request timeout. |
| `PORTFOLIO_SYNC_USER_ADDRESS` | *(empty)* | Address to query; defaults to `POLYMARKET_FUNDER`. |
| `DATA_API_HOST` | `https://data-api.polymarket.com` | Polymarket Data API base. |

## Fees

| Variable | Default | Meaning |
|---|---|---|
| `POLYMARKET_TAKER_FEE_RATE` | `0.005` | Fallback taker rate. Market fee metadata takes precedence when present. |
| `KALSHI_TAKER_FEE_RATE` | `0.003` | Kalshi taker rate assumption for T1. |

Polymarket Fee V2 (since 30 March 2026) charges `rate · p · (1-p)`, peaking at
`p = 0.5`. The `rate` is per-market: crypto Up/Down markets carry
`feeSchedule.rate = 0.07`, fourteen times the default above. The bot resolves
the rate `for_market` on both T0 and T2, so the default is only a fallback — but
**any offline analysis that uses the default will understate costs badly**. See
[research-findings.md](research-findings.md#the-fee-model-was-wrong-by-14).

## Edge engine

| Variable | Default | Meaning |
|---|---|---|
| `EDGE_MIN_BPS` | `100` | Minimum edge to trigger, basis points. |
| `EDGE_MAX_SPREAD_BPS` | `500` | Maximum acceptable spread. |
| `EDGE_MIN_CONFIDENCE` | — | Confidence floor. |
| `EDGE_FULL_CONFIDENCE_BPS` | — | Edge at which confidence saturates. |
| `EDGE_OBI_WEIGHT` | — | Weight of order-book imbalance in confidence. |
| `EDGE_VOL_SPIKE_RATIO` / `EDGE_VOL_SPIKE_MULTIPLIER` | — | `fast/slow` ratio that counts as a spike, and the confidence penalty (<1). |
| `EDGE_VOL_CALM_RATIO` / `EDGE_VOL_CALM_MULTIPLIER` | — | Ratio that counts as calm, and the confidence bonus (>1). |

## Volatility estimation

| Variable | Default | Meaning |
|---|---|---|
| `VOL_FAST_MINUTES` | `60` | Fast window. |
| `VOL_SLOW_MINUTES` | `360` | Slow window. |
| `VOL_MIN_SAMPLES` | `20` | Minimum observations before the estimator is considered warm. |

## T2 — statistical arbitrage

### Quality gates

| Variable | Default | Meaning |
|---|---|---|
| `T2_MIN_DEVIATION` | `0.05` | Minimum absolute probability deviation. |
| `T2_MAX_SPREAD_BPS` | — | Maximum two-sided spread. |
| `T2_MIN_TOP_DEPTH` | — | Minimum top-of-book executable depth. |
| `T2_MAX_COMPLEMENT_ERROR_BPS` | — | Maximum Yes/No complement error. |
| `T2_MAX_SIGNALS_PER_MARKET_PER_HOUR` | `5` | Per-market rate cap; prevents one stable mispricing from re-firing every cycle. |
| `T2_MAX_SIGNALS_PER_CYCLE` | `30` | Per-cycle signal cap. |
| `T2_MAX_HORIZON_DAYS` | `180` | Reject markets resolving further out than this — long-tail positions have poor IRR. |
| `T2_LONG_HORIZON_DAYS` / `T2_LONG_HORIZON_MIN_NET_EDGE_BPS` | `30` / `200` | Markets past the horizon need a higher net edge. |
| `T2_NEAR_EFFICIENT_MIN_NET_EDGE_BPS` | `300` | Finance/Crypto categories have a maker-taker gap of only 0.17pp, so the taker fee eats ordinary edges. |
| `T2_REJECT_PRICE_BELOW` / `T2_REJECT_PRICE_ABOVE` | `0.10` / `0.90` | Longshot/favourite tax gate. Buying YES below 0.10 averaged −41% EV. |

### Exits

| Variable | Default | Meaning |
|---|---|---|
| `T2_STOP_LOSS_BPS` | `300` | Static stop. |
| `T2_TAKE_PROFIT_CAPTURE_PCT` | `0.6` | Fraction of modelled edge to capture before taking profit. |
| `T2_MAX_HOLD_SEC` | `21600` | Time stop (6h). |
| `T2_EXIT_EVAL_INTERVAL_SEC` | `30` | Exit evaluation cadence. |
| `T2_OPTIMAL_STOPPING_ENABLED` | `true` | Bellman exit thresholds. |
| `T2_SCALE_OUT_TRANCHES` | `3` | Take-profit and optimal-stopping triggers sell `size_remaining × (1/remaining)` each time. Stop-loss, time-stop and floor-dump always exit in full. `1` = old single-stop behaviour. |
| `T2_STOP_LOSS_DYNAMIC_ENABLED` | `false` | ATR-equivalent dynamic stop: `clamp(k × realised_vol_bps, min, max)`. Unvalidated — see [pending-validations.md](pending-validations.md). |
| `T2_STOP_LOSS_DYNAMIC_K` / `_MIN_BPS` / `_MAX_BPS` / `_WARMUP` | `2.0` / `100` / `1000` / `5` | Dynamic stop parameters. |
| `T2_POST_EXIT_COOLDOWN_SEC` | `86400` | Re-entry cooldown per market after an exit, persisted across restarts. Shorten to 1800 during shadow runs to accumulate samples faster. |
| `T2_RECENT_EXITS_STATE_FILE` | `data/telemetry/recent_exits.json` | Cooldown state file. |

### UPDOWN (spot-anchored short-horizon markets)

| Variable | Default | Meaning |
|---|---|---|
| `T2_UPDOWN_ENABLED` | `false` | Priority boost for spot-anchored markets in the scan pool and WS subscription selection. |
| `T2_UPDOWN_PRIORITY_BOOST` | `1.0` | Boost weight. |
| `T2_UPDOWN_SYMBOLS` | `btc,eth` | Symbols to probe. |
| `T2_UPDOWN_WINDOW_MINUTES` | `15` | Window length probed via `/events?slug={sym}-updown-{w}m-{slot}`. Polymarket moved these to 5-minute windows during 2026. |
| `T2_UPDOWN_SLOTS_AHEAD` | `4` | How many upcoming windows to pull in. |
| `T2_UPDOWN_RTDS_MODE` | `shadow` | `off` / `shadow` / `primary`. Shadow prices off Binance and only emits basis telemetry. |
| `T2_UPDOWN_RTDS_STALENESS_SEC` | `30` | Fall back to Binance past this age. |
| `T2_UPDOWN_BASIS_LOG_INTERVAL_SEC` | `300` | How often to write an `updown_spot_basis` row. |
| `T2_UPDOWN_BASIS_ALERT_BPS` | `50` | Alert when the two sources diverge by more than this. |

UPDOWN markets settle against Polymarket's own price feed, not Binance. The
basis between them is largest exactly when UPDOWN is most sensitive.

### Shadow-mode research rules

All default to observation only.

| Variable | Default | Meaning |
|---|---|---|
| `T2_NEAR_CERTAINTY_SHADOW_MODE` | `true` | Log `near_certainty.would_apply_*` without modifying size/confidence. |
| `T2_NEAR_CERTAINTY_HIGH_THRESHOLD` / `_LOW_THRESHOLD` | `0.92` / `0.08` | Bands treated as near-certain. |
| `T2_NEAR_CERTAINTY_SIZE_MULTIPLIER` / `_CONFIDENCE_DELTA` | `0.60` / `-0.08` | Adjustments applied once the rule graduates. |
| `T2_BARBELL_ENABLED` | `false` | Split T2 capital into data-driven (~80%) and tail (~15%) buckets, relaxing the tail-risk discount while the tail bucket has room. |
| `T2_BARBELL_TAIL_BUDGET_PCT` / `_TAIL_RELAXED_MULTIPLIER` | `0.15` / `0.85` | Barbell parameters. |

### Weather sub-strategy

| Variable | Default | Meaning |
|---|---|---|
| `WEATHER_STRATEGY_ENABLED` | `false` | Open-Meteo GFS ensemble pricing for temperature contracts. |
| `WEATHER_MIN_EDGE` / `WEATHER_MIN_CONFIDENCE` | `0.10` / `0.70` | Entry thresholds. |
| `WEATHER_MAX_SPREAD_BPS` / `WEATHER_MIN_TOP_DEPTH` | `180` / `25` | Book quality gates. |
| `WEATHER_FORECAST_TTL_SEC` / `WEATHER_REQUEST_TIMEOUT_SEC` / `WEATHER_MAX_MARKETS` | `900` / `10` / `40` | Forecast cache and limits. |

This strategy tested negative once forecasts were made lead-honest. Keep it off.

## T3 — market making

| Variable | Default | Meaning |
|---|---|---|
| `MAKER_STRATEGY_ENABLED` | `false` | Enable T3. Off by default: T3 lost on 53 of 53 closed shadow positions (see [research-findings](research-findings.md)). Keep it off for any live run — post-only/GTC behaviour also adds variables you do not want while validating engineering. |
| `MAKER_MAX_HOLD_SEC` | `21600` | Force-close timeout. |
| `MAKER_STOP_LOSS_BPS` / `MAKER_TAKE_PROFIT_BPS` | `300` / `200` | Exit thresholds. |
| `MAKER_EXIT_EVAL_INTERVAL_SEC` | `30` | Exit evaluation cadence. |

### Liquidity rewards band

| Variable | Default | Meaning |
|---|---|---|
| `MAKER_REWARDS_ENABLED` | `true` | Read `rewards_max_spread` per market and constrain quotes to land inside the scoring band. |
| `MAKER_REWARDS_TTL_SEC` / `_NEGATIVE_TTL_SEC` / `_TIMEOUT_SEC` | `900` / `300` / `5` | Cache and timeout. |
| `MAKER_REWARDS_PREFETCH_PER_CYCLE` | `20` | New markets warmed per cycle by a background thread. |
| `MAKER_REWARDS_ONLY` | `false` | Quote only in markets that have a rewards band. |

### Scoring audit

Resting inside the band is not the same as actually scoring.

| Variable | Default | Meaning |
|---|---|---|
| `MAKER_SCORING_AUDIT_ENABLED` | `true` | Poll `/orders-scoring` and write `maker_scoring_audit` rows into `risk_events`. |
| `MAKER_SCORING_AUDIT_INTERVAL_SEC` | `60` | Audit cadence. |
| `MAKER_SCORING_CANCEL_UNSCORED` | `false` | Cancel orders that stay unscored past the grace window. Observation-only by default. |
| `MAKER_SCORING_UNSCORED_GRACE_SEC` | `90` | Grace window separating transient from persistent non-scoring. |

### Anti-sniping

Thresholds are in **ticks, not basis points** — one tick at price 0.50 is 200bps
but 2000bps at 0.05, so a bps threshold would pause cheap markets permanently
and do nothing on expensive ones.

| Variable | Default | Meaning |
|---|---|---|
| `T3_ANTI_SNIPE_ENABLED` | `true` | Master switch. |
| `T3_ANTI_SNIPE_JUMP_TICKS` / `_JUMP_PAUSE_SEC` | `3` / `20` | Pause quoting on a token after a mid jump this large. |
| `T3_ANTI_SNIPE_STABLE_TICKS_REQUIRED` / `_STABLE_BAND_TICKS` | `2` / `1` | Consecutive in-band observations required to resume. |
| `T3_ANTI_SNIPE_EMA_ALPHA` / `_MID_HISTORY` | `0.3` / `7` | Quote anchor = median (outlier resistance) + EMA (jitter resistance). `alpha=0` disables the EMA. |
| `T3_ANTI_SNIPE_POST_FILL_COOLDOWN_SEC` | `15` | Cooldown after a fill — being hit suggests the counterparty knew something. |
| `T3_ANTI_SNIPE_MAX_CHASE_TICKS` | `2` | Per-update quote movement cap. `0` = unlimited. |

### Flow bias

| Variable | Default | Meaning |
|---|---|---|
| `T3_FLOW_BIAS_ENABLED` | `true` | Track per-market `taker_yes_share`. Currently telemetry-only. |
| `T3_FLOW_BIAS_WINDOW_SEC` / `_MIN_TRADES` / `_STRONG_THRESHOLD` | `3600` / `20` / `0.55` | Aggregation window and significance thresholds. |
| `T3_FLOW_BIAS_INVENTORY_WEIGHT` | `0.5` | Weight of aggregated flow as synthetic inventory pressure when steering quotes. `0.0` = telemetry only. |
| `T3_FLOW_STATE_FILE` | `data/telemetry/flow_state.json` | Persisted state. |

## T1 — cross-platform

| Variable | Default | Meaning |
|---|---|---|
| `CROSS_PLATFORM_PAIRS_JSON` | *(empty)* | Explicit Polymarket↔Kalshi pairing table. Empty disables T1 entirely; the bot never guesses a mapping. |
| `CROSS_PLATFORM_ENTITY_VETO_ENABLED` | `true` | Veto pairs whose question text disagrees on threshold/date/direction. Vetoes only — it never creates a pair, and passes through when either side lacks question text. |
| `CROSS_PLATFORM_MIN_TOKEN_OVERLAP` | `0` | Token Jaccard floor. `0` = off; cross-venue wording differs enough that this generates false vetoes easily. |

The dangerous failure mode of a hand-written pairing table is not a missing
pair, it is a **wrong** pair: two markets with different thresholds still make
`poly_yes + kalshi_no < 1` look like risk-free arbitrage.

## Quant input hot reload

These JSON files are re-read by the main loop on every scan cycle (atomic writes
from sidecar workers). If a file is temporarily corrupt or a fetch fails, the
last valid content stays in effect.

| Variable | Default |
|---|---|
| `LOGICAL_CONSTRAINTS_FILE` | `data/quant_inputs/logical_constraints.json` |
| `EVENT_BASELINES_FILE` | `data/quant_inputs/event_baselines.json` |
| `WALLET_ALPHA_PROFILES_FILE` | `data/quant_inputs/wallet_profiles.json` |
| `WALLET_ALPHA_OBSERVATIONS_FILE` | `data/quant_inputs/wallet_observations.json` |
| `RESEARCH_SIGNAL_FEEDS_FILE` | `data/quant_inputs/research_feeds.json` |

Inline JSON equivalents (`LOGICAL_CONSTRAINTS_JSON`, `EVENT_BASELINES_JSON`,
`WALLET_ALPHA_PROFILES_JSON`, `WALLET_ALPHA_OBSERVATIONS_JSON`) exist but require
a restart to change; prefer the file form.

| Variable | Default | Meaning |
|---|---|---|
| `WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED` | `false` | Allow unvalidated wallets to produce shadow signals. Shadow/dry-run only. |
| `WALLET_ALPHA_SHADOW_VALIDATION_ENABLED` | `false` | In-process shadow-only validation lane for candidate wallets during live runs. Never places real orders. Off by default: copy trading returned −28.6% ROI followed to settlement. |
| `WALLET_ALPHA_SHADOW_MAX_SIGNALS_PER_CYCLE` | `5` | Candidate budget per cycle. |
| `WALLET_ALPHA_SHADOW_MAX_EXEC_MS_PER_CYCLE` | `250` | Time budget per cycle. |

Only wallets promoted into `wallet_profiles.json` reach the live execution path.
Note that this whole strategy line tested at −28.6% ROI; see
[research-findings.md](research-findings.md#wallet-alpha--copy-trading).

## WebSocket

| Variable | Default | Meaning |
|---|---|---|
| `WS_ENABLED` | `true` | Use WebSocket book push instead of pure REST polling. |
| `WS_MAX_MARKETS` | `80` | Markets tracked concurrently (two tokens each). Keep it at least `ARB_HOT_MARKET_POOL_SIZE`. |
| `WS_REFRESH_CYCLES` | `200` | Scan cycles between re-selections of the tracked set. |
| `WS_VOL_FEED_INTERVAL_SEC` | `60` | Mid-price feed interval into `VolEstimator`. |
| `USER_WS_ENABLED` | `true` | User channel: own fills push straight into risk and exit management instead of waiting for the next REST poll. Needs L2 credentials; falls back to polling if unavailable. |
| `USER_WS_QUEUE_SIZE` / `USER_WS_MAX_EVENTS_PER_CYCLE` | `2000` / `500` | User-channel queue limits. |

Raising `WS_MAX_MARKETS` without also raising `ARB_HOT_MARKET_POOL_SIZE` wastes
subscription slots. The reverse is worse: every hot-pool market not on WS is
fetched over REST every cycle, which turns a seconds-long scan cycle into
minutes (the bot logs a warning at startup when `WS_MAX_MARKETS` is smaller). Polymarket removed its 100-token subscription cap in May
2025; the real constraint was client-side — the `websockets` library defaults to
`max_size=1MB`, and the server's `initial_dump=true` full-book dump exceeds that
with many tokens, causing the client to close with code 1009 (`MESSAGE_TOO_BIG`)
and reconnect forever. `websocket_feed.py` sets `max_size=16MB`.

## Order book fetching

| Variable | Default | Meaning |
|---|---|---|
| `ORDERBOOK_SNAPSHOT_TTL_SEC` | `0.5` | REST snapshot cache TTL. Keep between 0.2 and 0.8 — smaller returns to heavy REST load, larger makes the scanned book stale. |
| `ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC` | `10` | Age past which a WS snapshot is abandoned for REST — unless the feed is live (next row). |
| `ORDERBOOK_WS_LIVENESS_SEC` | `20` | Polymarket pushes only on change, so a quiet book is still current while the feed is connected. A mirror book refreshed during the current connection is used regardless of age as long as the feed processed any message (heartbeats included) within this window. Disconnects fall back to the age rule. The live `feed_health` gate still uses per-book age. |
| `ORDERBOOK_RETRY_COUNT` / `ORDERBOOK_RETRY_DELAY_SEC` | `2` / `0.15` | REST retry policy. |
| `ORDERBOOK_MISSING_COOLDOWN_SEC` | `300` | Cooldown for a token after CLOB explicitly returns "No orderbook exists". |

## Recording and telemetry

| Variable | Default | Meaning |
|---|---|---|
| `TICK_RECORD_ENABLED` / `TICK_RECORD_DIR` | `false` / `data/ticks` | Write every WS book update to NDJSON. Required for any tick-level backtest. |
| `TELEMETRY_RECORD_ENABLED` / `TELEMETRY_RECORD_DIR` | `false` / `data/telemetry` | Opportunity / trade / risk event recording. |
| `TELEMETRY_ASYNC_WRITE` | `true` | Background writer thread; the main loop pays only JSON encoding and enqueue, never `fsync`. Oldest events are dropped when the queue is full. |
| `TELEMETRY_ASYNC_QUEUE_SIZE` | `10000` | Writer queue depth. |
| `SHADOW_MAKER_FILL_LATENCY_SEC` | `2.0` | In dry-run, minimum time a simulated maker order must rest before it can fill — a crude queue-position penalty. |

Turn both recorders on for any unattended observation run. Without them you
cannot answer "why was there no signal" after the fact.

## Data retention

Long-running deployments fill disks.

| Variable | Default |
|---|---|
| `DATA_CLEANUP_ENABLED` | `true` |
| `DATA_CLEANUP_INTERVAL_SEC` | `3600` |
| `DATA_TICKS_RETENTION_DAYS` / `DATA_TICKS_MAX_GB` | `7` / `5` |
| `DATA_TELEMETRY_RETENTION_DAYS` / `DATA_TELEMETRY_MAX_GB` | `14` / `2` |
| `DATA_RESEARCH_CACHE_RETENTION_DAYS` / `_MAX_GB` | `14` / `1` |
| `DATA_BACKTEST_RETENTION_DAYS` / `DATA_BACKTEST_MAX_GB` | `30` / `2` |

## Research signal layer

| Variable | Default | Meaning |
|---|---|---|
| `RESEARCH_SIGNAL_ENABLED` | `false` | Enable research signal aggregation. |
| `RESEARCH_SIGNAL_WINDOW_SEC` | `86400` | Lookback window. |
| `RESEARCH_SIGNAL_MAX_ITEMS` | `5` | Items retained per round. |
| `RESEARCH_SIGNAL_CACHE_TTL_SEC` / `_CACHE_DIR` | `300` / `data/research_signal` | Disk cache. |
| `RESEARCH_SIGNAL_HTTP_JSON_SOURCES` | *(empty)* | Extra HTTP JSON sources, as a JSON list. |
| `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED` | `true` | Fear & Greed collector. Free, no auth, one row per crypto topic. Not a hard gate. |
| `RESEARCH_SIGNAL_MANIFOLD_ENABLED` | `true` | Manifold Markets crowd probability. Tier-2 source, mainly useful for politics/geopolitics/sports/awards where news RSS is thin. Stance: ≥0.60 bullish, ≤0.40 bearish. |

## Backtest

| Variable | Default |
|---|---|
| `BACKTEST_ENABLED` | `false` |
| `BACKTEST_DATA_DIR` | `data/backtest` |
| `BACKTEST_DEFAULT_DATASET` | `default` |
| `BACKTEST_SLIPPAGE_BPS` | `5` |
| `BACKTEST_REPORTS_DIR` | `research/backtest/output` |

## Dashboard and logging

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_ENABLED` | `false` | FastAPI monitoring backend. |
| `DASHBOARD_PORT` | `8077` | Port. **Binds loopback only, by design.** Reach it over an SSH tunnel; do not rebind to `0.0.0.0`. |
| `LOG_LEVEL` | `INFO` | `httpx`/`httpcore` request logs are forced down to WARNING automatically. |
| `LOG_FILE` | `arb_bot.log` | Log file path. |

## Notifications (Feishu)

Notifications enable themselves once a complete Feishu app-bot configuration is
present, and stay off otherwise.

| Variable | Default | Meaning |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | *(empty)* | App-bot credentials. |
| `FEISHU_OPEN_ID` | *(empty)* | Single fixed recipient; `open_id` only. |
| `FEISHU_API_BASE` | — | Feishu Open Platform base URL. |
| `NOTIFICATION_COOLDOWN_SEC` | `30` | Per-category cooldown. |
| `NOTIFY_ON_ARB_FOUND` | `false` | Opportunity notifications. Off by default — they flood. |
| `NOTIFY_ON_ARB_FOUND_IN_SHADOW` | `false` | Also push opportunities in dry-run. When on, every shadow message is prefixed `🌓 [SHADOW]`. |
| `NOTIFY_ON_TRADE_SUCCESS` / `NOTIFY_ON_TRADE_FAILURE` | `true` / `true` | Fill notifications. |
| `NOTIFY_ON_FATAL_ERROR` | `true` | Repeated API errors, risk breaker trips. |
| `NOTIFY_ON_PNL_ALERT` | `true` | Threshold alerts against recorded `daily_pnl`. |
| `NOTIFY_ON_DAILY_SUMMARY` | `true` | Daily digest. |
| `PNL_PROFIT_ALERT_USDC` / `PNL_LOSS_ALERT_USDC` | `20` / `10` | Alert thresholds. |
| `FATAL_ERROR_COOLDOWN_SEC` | — | Cooldown per fatal-error category. |
| `DAILY_SUMMARY_TIME_HHMM` / `DAILY_SUMMARY_TIMEZONE` | `08:05` / `Asia/Shanghai` | Digest schedule. The risk day still rolls at UTC midnight. |
| `NOTIFICATION_STATE_FILE` | `data/telemetry/notification_state.json` | Dedup and counters, preserved across restarts. |

## LLM and scoring providers

Neither of these is on the trading hot path. See
[ai-configuration.md](ai-configuration.md).

| Variable | Default | Meaning |
|---|---|---|
| `AI_PROVIDER` | `openai` | `openai` / `anthropic` / `ollama` / `deepseek` / `gemini`. |
| `AI_API_KEY` | *(empty)* | Falls back to `OPENAI_API_KEY`. |
| `AI_API_BASE` | *(empty)* | Custom endpoint; empty uses the provider default. |
| `AI_MODEL` | `gpt-4o` | Model name. |
| `AI_TEMPERATURE` | `0.1` | Sampling temperature. |
| `TYPESAFE_API_KEY` | *(empty)* | TypeSafe Jev key. |
| `TYPESAFE_MODEL` | `jev-1.13.0` | Pin the version — `jev-latest` drifts and invalidates calibrated thresholds. |
| `TYPESAFE_RPS` | `10` | Client-side token bucket. |
| `TYPESAFE_TIMEOUT_SEC` / `TYPESAFE_MAX_RETRIES` | `10` / `3` | Request timeout and 429/529 retries. |
| `TYPESAFE_BASE_URL` | *(empty)* | Defaults to `https://api.typesafe.ai`. |

---

## Observation-mode preset

Before going anywhere near live capital, run wide and record everything:

```dotenv
ARB_DRY_RUN=true
TICK_RECORD_ENABLED=true
TELEMETRY_RECORD_ENABLED=true
RESEARCH_SIGNAL_ENABLED=true

ARB_MARKET_FOCUS_KEYWORDS=          # whole market, no topic filter
ARB_HOT_MARKET_POOL_SIZE=150
ARB_HOT_EVENT_POOL_SIZE=50
ARB_MIN_LIQUIDITY=500
ARB_MIN_VOLUME_24H=300
WS_MAX_MARKETS=150

T2_MIN_DEVIATION=0.01               # shadow only — see below
T2_MAX_SPREAD_BPS=1000
T2_MIN_TOP_DEPTH=30
T2_MAX_COMPLEMENT_ERROR_BPS=250
```

A wider observation preset is not a step toward live trading. Its only purpose
is to answer "are there signals at all". Keep `ARB_DRY_RUN=true`, keep risk caps
small, and tighten the thresholds back before considering real orders.
`T2_MIN_DEVIATION=0.01` in particular is far below anything that survives fees.
