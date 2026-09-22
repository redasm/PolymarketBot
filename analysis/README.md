# `analysis/` — one-off empirical studies

Scripts in this directory are **research one-offs**, not library code. They were
written to answer a specific question once, they are not imported by the bot,
and they are not covered by the test suite. They are kept because the
conclusions in
[../doc/en/research-findings.md](../doc/en/research-findings.md) are only
meaningful if you can re-run the analysis that produced them.

Expect to read a script before running it. Several take paths to data that no
longer exists on any particular machine.

## Datasets are not in the repository

Two files that several scripts default to are deliberately **not tracked** —
one is 7.7MB of other people's on-chain trades, the other a list of wallet
addresses. Both are regenerable:

| File | Regenerate with |
|---|---|
| `analysis/wallet_trades_apr_jun2026.parquet` | The HuggingFace dataset `TimeSeventeen/Polymarket-v1`, config `orderfilled` — see `explore_hf_dataset.py` for the schema. Filter to your window and save as parquet with columns `taker`, `price`, `usdc_amount`, `block_timestamp`, `condition_id`. |
| `analysis/tracked_wallets.json` | `python analysis/extract_wallets.py --telemetry-dir data/telemetry` — extracts followed wallet addresses from `*.positions_lifecycle.ndjson`. |

Recorded ticks and telemetry (`data/`) are also untracked. Produce them by
running the bot with `TICK_RECORD_ENABLED=true` and
`TELEMETRY_RECORD_ENABLED=true`.

## Dependencies

Several scripts need `pandas`, `pyarrow`, or `datasets`, which are **not** in
`requirements-base.txt` — the bot itself does not use them.

```bash
pip install pandas pyarrow datasets
```

## What each script does

### T0 — structural arbitrage

| Script | Purpose |
|---|---|
| `t0_tick_scan.py` | Scan recorded tick streams for `Σ ask < 1` and classify each hit — the script that showed every one was a crossed book. |
| `t0_window_survival.py` | How long an apparent opportunity survives after it appears. |
| `build_t0_dataset.py` | Build a T0 backtest dataset from recorded ticks. |
| `run_t0_backtest.py` | Run the T0 backtest over that dataset. |
| `fetch_public_trades.py` | Pull public taker prints from `data-api.polymarket.com/trades` for the condition_ids in a recorded tick day. Ticks only carry `book` events, so without this a book delta is indistinguishable between "traded through" and "cancelled". |

### T2 / UPDOWN

| Script | Purpose |
|---|---|
| `verify_updown_markets.py` | Confirm which UPDOWN markets exist and their window length. |
| `updown_threshold_sweep.py` | Sweep `min_deviation` and report PnL per threshold. |
| `updown_realism.py` | Apply realistic execution assumptions (executable exit prices, per-market fees). |
| `updown_bias_check.py` | **Delay injection.** Re-run with entries delayed and report surviving PnL. This is the script that falsified the 15-minute result. |

### T3 — market making

| Script | Purpose |
|---|---|
| `t3_maker_replay.py` | Replay maker quoting against recorded books and public trades; produces the open/close distribution behind the adverse-selection finding. |

### Wallet alpha

| Script | Purpose |
|---|---|
| `extract_wallets.py` | Build the tracked-wallet list from telemetry. |
| `build_wallet_alpha_dataset.py` | Join tracked-wallet buys with contemporaneous BBO into a backtest dataset. |
| `build_wallet_profiles.py` | Compute per-wallet quality profiles. |
| `run_wallet_alpha_backtest.py` | Backtest following those wallets. |
| `wallet_alpha_settle_validate.py` | Follow positions all the way to settlement — the −28.6% ROI number. |

### Weather

| Script | Purpose |
|---|---|
| `weather_harvest.py` | Harvest weather events, geocode cities, pull historical forecasts and CLOB price history. |
| `weather_lead_forecasts.py` | Pull **lead-honest** forecasts from Open-Meteo's previous-runs API — the forecast vintage published before the decision time. |
| `weather_upper_bound.py` | Best-case bound given perfect use of the forecast. |
| `run_weather_open_data_test.py` | End-to-end test against Open-Meteo ensemble data. |

### TypeSafe Jev

| Script | Purpose |
|---|---|
| `build_typesafe_replay_snapshots.py` | Build replay snapshots from settled markets. |
| `eval_typesafe_replay.py` | Score the model against the order book: Brier, skill, AUC, by category and horizon. |
| `probe_news_coverage.py` | Query GDELT for news coverage before settlement — the information-layer audit. Runs for hours because of free-tier rate limits. |

### General

| Script | Purpose |
|---|---|
| `pnl_summary.py`, `pnl_deep.py` | PnL aggregation over telemetry. |
| `analyze_two_periods.py` | Compare two time windows. |
| `run_backtests.py` | Batch-run the backtest runner across configurations. |
| `explore_hf_dataset.py` | Inspect the HuggingFace Polymarket dataset schema. |

## Writing a new one

Put it here. Do not gate on finding it a "proper home" — that is what this
directory is for. Two conventions worth keeping:

1. Write a module docstring stating the question the script answers and the
   exact command line used to produce the result you are citing.
2. Emit a summary table, not raw rows. The point of these scripts is to make a
   large NDJSON file legible.
