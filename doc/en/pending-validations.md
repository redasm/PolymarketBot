[English](../en/pending-validations.md) · [中文](../zh/pending-validations.md)

# Pending validations

Several optimisations from the May 2026 research review ship
**shadow-mode-only** or **behind disabled flags** because pre-implementation
empirical verification was blocked on data availability. This document is the
checklist for finishing each validation once the data becomes available.

> These items are **untested**, not validated. That is a different status from
> the strategies in [research-findings.md](research-findings.md), which were
> tested and failed. Nothing here is known to work.

For each item:
  - **Claim** — the article assertion we want to verify.
  - **Status** — what currently runs in the bot.
  - **Data required** — what we couldn't get on free-tier APIs.
  - **Validation method** — what to compute and the graduation threshold.
  - **Script** — re-runnable entrypoint already in the repo.
  - **Flag to flip** — the env var(s) that turn the feature on after the
    claim is validated.

---

## 1. Near-certainty 92-98¢ trap (Article 4 — Taleb / @stacyonchain)

- **Claim**: Polymarket binary contracts that trade at ≥0.92 systematically
  resolve YES at a rate < 92%, i.e. crowds underprice tail risk on
  near-certain bets.
- **Status**: `NearCertaintyRule` ships in **shadow mode** by default
  (`T2_NEAR_CERTAINTY_SHADOW_MODE=true`). The orchestrator computes what
  the rule would do on each directional signal and surfaces counts under
  `meta.near_certainty.{would_apply_high, would_apply_longshot}` in the
  status payload — but does not modify production size/confidence.
- **Data required**:
  - Per-market price history for *resolved* Polymarket binary markets
    (need max(price) over each market's lifetime), paired with the
    resolution outcome (`outcomePrices`).
  - **Free-tier blockers** found 2026-05-22:
    - `clob.polymarket.com/prices-history` → empty for closed tokens
      (only active markets retain history).
    - `data-api.polymarket.com/trades` → per-market filters silently
      ignored; returns global feed only.
    - Goldsky `orderbook-subgraph/prod` → `orderBy: timestamp desc`
      statement-timeouts on heavy markets; older markets unindexed.
  - **Candidate paid sources** (to evaluate):
    - Dune Analytics (Polymarket dashboards already exist) — ~$0/$390/mo
      tiers; SQL queries against indexed trade events.
    - Goldsky paid tier — removes the statement-timeout.
    - Polymarket's own analytics team (request via Discord) — may
      publish historical OHLC for resolved markets.
- **Validation method**:
  1. Pull resolved binary markets from `gamma-api?closed=true`, paired
     with `outcomePrices`.
  2. For each, fetch lifetime max(YES price) from the paid source.
  3. For multiple buckets (0.92, 0.95, 0.97, 0.98):
     - cohort = markets with max ≥ bucket
     - realised YES rate = (# resolved YES) / |cohort|
     - 95% Wilson CI on the rate
  4. **Graduation rule**: at the 0.95 bucket, realised YES rate must be
     < 0.92 *with the CI upper bound also < 0.92*, on a cohort of N ≥ 50.
  5. **Reject rule**: rate ≥ 0.92 or CI overlaps 0.92 — `NearCertaintyRule`
     does not represent real edge, archive it.
- **Script**: `scripts/verify_near_certainty_trap.py` already scaffolds
  the gamma-side pull and the Wilson-CI bucketing; only the per-market
  max-price lookup needs the new data source.
- **Flag to flip**: `T2_NEAR_CERTAINTY_SHADOW_MODE=false`. Keep the
  thresholds, multiplier, and confidence delta at the validated values
  (currently `T2_NEAR_CERTAINTY_{HIGH,LOW}_THRESHOLD`,
  `T2_NEAR_CERTAINTY_SIZE_MULTIPLIER`, `T2_NEAR_CERTAINTY_CONFIDENCE_DELTA`).
- **Local-data fallback**: the bot's `data/telemetry/*.strategy_signals.ndjson`
  records every T2 directional signal with its `market_prob` and
  `near_certainty` block. After ≥3 months of live operation, you can do
  a partial validation by joining these records against the
  eventual resolution outcomes from `gamma-api`. The cohort will be
  smaller (only markets the bot scanned) and skewed (only signals that
  passed earlier gates), but a directionally consistent result on local
  data is a strong prior for paid-data verification.

---

## 2. On-chain BTC signals (MVRV / SOPR / ETF / macro) — Article 3

- **Claim**: A four-dim resonance of MVRV Z-Score, SOPR 28-day MA, ETF
  netflow, and macro liquidity predicts BTC direction. The article
  proposes triggering only when ≥3 of 4 dimensions agree.
- **Status**: A free-tier subset is implemented — `CryptoMacroCollector`
  surfaces Fear & Greed (alternative.me) as a `research_signal` row for
  any topic containing crypto keywords. Default off:
  `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED=false`. The full four-dim
  resonance is **not implemented** because the three remaining dims need
  paid data.
- **Data required**:
  - **MVRV Z-Score** — Glassnode `/v1/metrics/market/mvrv_z_score`
    (paid, ~$30-40/mo) OR CryptoQuant free tier (1-day lag).
  - **SOPR 28d MA** — Glassnode `/v1/metrics/indicators/sopr` (paid) OR
    CryptoQuant free.
  - **ETF netflow** — SoSoValue / Coinglass free tier rate-limits at
    ~60 req/min; doable but needs careful caching.
  - **Macro (M2 / Fed funds)** — FRED API, free with API key.
  - **BTC price** — CoinGecko / Binance public, free.
- **Validation method**:
  1. Pull all resolved BTC/ETH price-based binary markets (BTC > $X by
     date Y) from `gamma-api` with ≥ ~12 months of historical coverage.
     Filter by `volume24hr > 1M` to keep cohort meaningful (low-volume
     markets are noise).
  2. For each market, sample the four-dim signals on the *entry day*
     (or first day the market was active).
  3. Label each market by its resolution outcome (YES = 1, NO = 0).
  4. Fit a logistic regression: `P(YES) ~ MVRV_z + SOPR_28d + ETF_5d +
     Fed_dovish + Fear_Greed`. Use train/test split 80/20.
  5. **Graduation rule**: holdout AUC > 0.60 on a cohort of N ≥ 80.
  6. **Reject rule**: AUC ≤ 0.55 or coefficient signs inconsistent with
     the article's directional claim — archive the idea.
- **Script**: `scripts/verify_onchain_signal_predictive_power.py` is a
  stub today; expand it after subscribing to a data source. The script
  is intentionally split from the trading bot — it only writes to
  `data/research/onchain_signal_verification.json`.
- **Flags to flip on success**:
  - `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED=true` (already free; can flip
    independently — see "Local-data only" note below).
  - A new collector wiring MVRV/SOPR/ETF as additional rows. To be
    added; pattern follows `CryptoMacroCollector`.
- **Local-data fallback**: Fear & Greed alone is too weak to validate
  the multi-dim resonance claim. You can still enable
  `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED=true` independently — it just
  adds one sentiment row per crypto topic, no decision authority. The
  orchestrator's existing research_overlay resonance scoring is the
  arbiter.

---

## 3. Dynamic ATR-equivalent stop-loss — Article 2 (HyperLiquid)

- **Claim**: Replacing a fixed % stop with `k × ATR` improves PnL across
  236 PineScript strategies; the rescued strategies all converged on
  this rule.
- **Status**: Implemented as `T2_STOP_LOSS_DYNAMIC_ENABLED`, default
  **off**. The exit manager has the rolling per-position volatility
  tracker wired in; just doesn't consume it for the stop until the flag
  flips. Effective stop is surfaced as
  `decisions[i].effective_stop_bps` and `vol_bps` on each
  `t2_exit_telemetry` row.
- **Data required**: **None external**. The validation can be done
  entirely from local data the bot already produces.
- **Validation method**:
  1. Run the bot in shadow / dry-run mode for ≥ 2 weeks with
     `T2_STOP_LOSS_DYNAMIC_ENABLED=false` (records the static
     `T2_STOP_LOSS_BPS` baseline) and one with
     `T2_STOP_LOSS_DYNAMIC_ENABLED=true` in parallel (the
     `shadow_t2_exit_manager` is already wired to the same provider).
  2. For each exited position, compare:
     - per-position max-drawdown
     - count of premature stop-outs (positions that hit stop within 3×
       eval interval of entry)
     - distribution of `effective_stop_bps` vs static 300
  3. **Graduation rule**: dynamic config produces ≥ 15% reduction in
     premature stop-outs AND no degradation in net PnL on N ≥ 50 paired
     positions.
  4. **Reject rule**: any net PnL regression on a similarly sized cohort
     OR the median `vol_bps` is wildly different from `T2_STOP_LOSS_BPS`
     (suggests k=2 is wrong — recalibrate `T2_STOP_LOSS_DYNAMIC_K`
     before flipping).
- **Telemetry**: `data/telemetry/*.t2_exit.ndjson` (or wherever
  `t2_exit_telemetry()` is persisted in the cycle row) carries the
  per-decision `effective_stop_bps` and `vol_bps`. Pair with
  `*.strategy_executions.ndjson` for entry/exit PnL.
- **Flag to flip on success**: `T2_STOP_LOSS_DYNAMIC_ENABLED=true`.
  Tune `T2_STOP_LOSS_DYNAMIC_K` (default 2.0), `T2_STOP_LOSS_MIN_BPS`
  (100), `T2_STOP_LOSS_MAX_BPS` (1000) based on observed vol distribution.

---

## 4. Barbell capital allocation — Article 4 (Taleb)

- **Claim**: Splitting T2 capital into ~80% data-driven + ~15% tail +
  ~5% reserve, with relaxed size discount on tail bets (because
  portfolio-level concentration is capped at the bucket size), beats a
  flat T2 allocation with a uniform tail discount.
- **Status**: Implemented as `T2_BARBELL_ENABLED`, default **off**.
  Per-class exposure ledger is built and surfaced under `meta.barbell`
  in the orchestrator status. The relaxation logic is wired but
  inactive while disabled.
- **Data required**: **None external**. Same local-telemetry approach
  as #3.
- **Validation method**:
  1. Run with `T2_BARBELL_ENABLED=false` for the baseline; record
     `meta.barbell.exposure_usdc` and `meta.tail_risk` over ≥ 2 weeks.
     The exposure ledger will still populate even with the policy
     disabled — that's by design so you can see what *would* happen.
  2. Compute baseline:
     - distribution of T2 PnL by tail-risk class
     - max tail-class drawdown
     - count of consecutive losses in the high_tail bucket
  3. Flip `T2_BARBELL_ENABLED=true` for an equal-length period.
  4. **Graduation rule**: tail-class PnL volatility decreases without
     median PnL degradation, AND tail-class drawdown ≤ baseline.
  5. **Caveat — per-signal-ID release**: the current exposure ledger
     over-counts in production because `record_settlement` does not
     pass through `signal_id`. Before flipping to live, plumb
     `signal_id` through `t2_exit_manager._release_orchestrator_exposure`
     → orchestrator → `_release_barbell_exposure(signal_id)`. Without
     this fix, the tail bucket will appear full faster than reality
     and the relaxation will stop applying. The hook is already in
     `_release_barbell_exposure(signal_id, amount)` — just wire the
     caller. Tests live in `tests/test_barbell_policy.py`.
- **Telemetry**: `meta.barbell.exposure_usdc.{data_driven, tail}` in
  the orchestrator status; `barbell.{applied, bucket, multiplier_override}`
  on every signal payload.
- **Flag to flip on success**: `T2_BARBELL_ENABLED=true`. Tune
  `T2_BARBELL_TAIL_BUDGET_PCT` (default 0.15 of T2 allocation) and
  `T2_BARBELL_TAIL_RELAXED_MULTIPLIER` (default 0.85) based on observed
  tail exposure utilisation.

---

## 5. RTDS crypto_prices as the UPDOWN settlement reference

- **Claim**: UPDOWN markets settle against Polymarket's own price feed, so
  pricing them off Binance introduces a basis that is largest exactly when
  UPDOWN is most sensitive (high volatility).
- **Status**: `RtdsSpotFeed` + `CompositeSpotFeed` ship in **shadow mode**
  (`T2_UPDOWN_RTDS_MODE=shadow`). Pricing still uses Binance; RTDS only
  emits an `updown_spot_basis` row into `risk_events` every
  `T2_UPDOWN_BASIS_LOG_INTERVAL_SEC`.
- **Data required**: an authoritative sample of the `crypto_prices` payload.
  The subscription protocol is confirmed (`{"action": "subscribe",
  "subscriptions": [{"topic", "type"}]}`, messages shaped
  `{"topic", "type", "timestamp", "payload"}`), but the payload field names
  are not. `parse_rtds_crypto_payload` therefore matches field names
  leniently (`symbol`/`pair`/`asset`, `value`/`price`/`close`, …) and drops
  anything it cannot read rather than guessing.
- **Validation method**: over ≥ 24h of `updown_spot_basis` rows, require
  (a) RTDS ticks arrive for every configured symbol with
  `rtds_age_sec` staying under `T2_UPDOWN_RTDS_STALENESS_SEC`, and
  (b) the basis distribution is centred near 0 with no unexplained regime
  breaks. A persistently non-zero basis means the two feeds are quoting
  different things — investigate before switching, do not switch.
- **Script**: `jq 'select(.event=="updown_spot_basis")'` over
  `data/telemetry/*.risk_events.ndjson`.
- **Flag to flip**: `T2_UPDOWN_RTDS_MODE=primary`.

---

## 6. Wallet profit-quality thresholds

- **Claim**: filtering followed wallets by win rate / profit factor /
  consistency / single-trade concentration yields better copy signals than
  ranking by activity.
- **Status**: `strategies/wallet_quality.py` ships **off**
  (`--quality-filter` not passed). The offline worker computes the full
  profile for every candidate regardless, so the distribution is
  observable before any threshold binds.
- **Data required**: our own distribution of `/closed-positions` metrics
  across the candidate pool. The default thresholds (win rate ≥ 0.60,
  profit factor ≥ 1.5, consistency ≥ 0.70, top-trade share ≤ 0.30) are
  taken from a public reference implementation and are **not validated on
  this project's data**.
- **Validation method**: run `python scripts/scan_quant_strategy_inputs.py
  wallet-quality --output data/quant_inputs/wallet_quality.json`, read the
  distribution of each metric, and set thresholds from percentiles rather
  than from the imported defaults.
- **Flag to flip**: pass `--quality-filter` to `auto-wallet-observations`
  (plus any `--min-*` overrides derived above).

---

## 7. Cancelling maker orders that are not scoring

- **Claim**: a resting maker order that `/orders-scoring` reports as
  non-scoring is earning no liquidity reward and should be re-posted.
- **Status**: the audit ships **observation-only**
  (`MAKER_SCORING_CANCEL_UNSCORED=false`). Every cycle writes
  `maker_scoring_audit` with `scoring / not_scoring / unknown /
  scoring_ratio`.
- **Data required**: enough `maker_scoring_audit` rows to know the baseline
  `scoring_ratio` and how often an order goes non-scoring transiently.
  Cancelling on a transient reading would churn orders and lose queue
  position for nothing.
- **Validation method**: confirm that orders flagged non-scoring stay
  non-scoring beyond `MAKER_SCORING_UNSCORED_GRACE_SEC` (i.e. the grace
  window separates transient from persistent), and that `unknown` stays a
  small share.
- **Flag to flip**: `MAKER_SCORING_CANCEL_UNSCORED=true`.

---

## Quick re-validation checklist (when paid data is acquired)

```bash
# 1. Install whichever paid client (Dune SDK, Glassnode SDK, etc).
# 2. Update the verification scripts to use it:
#    - scripts/verify_near_certainty_trap.py        # fills fetch_max_price()
#    - scripts/verify_onchain_signal_predictive_power.py
# 3. Run and read the JSON reports under data/research/.
# 4. Apply graduation rules above. If passed, flip the env flag and
#    redeploy.
```

## Article claims that are already validated or non-data-dependent

For completeness, the following Article-derived items shipped *without*
needing further data validation because they rest on textbook math:

| Item | Status | Justification |
|---|---|---|
| Bellman optimal-stopping recursion | shipped, on by default | Snell envelope (1965), textbook |
| `d-stop > 1-stop` scale-out | shipped, on by default | Kobylanski 2009, published theorem |
| Rolling `p_t` update for Bellman | shipped, on by default | basic dynamic programming |
| Quarter-Kelly | already shipped pre-2026 | direct from Kelly (1956) |

These do not appear in the pending list above.
