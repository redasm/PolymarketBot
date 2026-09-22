# Polymarket Arbitrage Bot

[English](README.md) · [中文](README.zh-CN.md)

A multi-tier trading system for Polymarket prediction markets: structural
arbitrage, cross-platform arbitrage, model-driven statistical arbitrage, and
market making — with the risk management, execution, telemetry and backtesting
infrastructure to actually measure whether any of it works.

> ## Read this first
>
> **Every strategy in this repository was tested on real data and failed.**
>
> T0 structural arbitrage: zero clean opportunities in seven days of tick data —
> every apparent signal was a crossed book. T3 market making: 53 of 53 closed
> positions lost money. Copy trading: −28.6% ROI followed to settlement. The
> best-looking result, +$443/week on 15-minute UPDOWN markets, retained 9% of
> its profit once a 1-second entry delay was injected.
>
> This is published as **research infrastructure and a record of negative
> results**, not as a profitable trading system. Live trading with it has been
> stopped.
>
> Full numbers, methods and reproduction scripts:
> **[doc/en/research-findings.md](doc/en/research-findings.md)**

## Why publish it anyway

The interesting part is not the strategies, it is the measurement apparatus that
killed them:

- **Delay injection** as a standard gate — the single test that exposed the
  largest apparent edge as look-ahead bias.
- **Per-market fee resolution** — the default fee rate understated crypto
  Up/Down costs by 14×, which is the difference between a profitable and an
  unprofitable backtest.
- **Crossed-book rejection** in arbitrage detection — without it, a venue's
  transient feed glitches read as free money.
- **Tick-level recording**, not snapshot sampling — snapshot joins manufacture
  opportunities that never coexisted.
- Honest accounting: median per-trade result and win fraction alongside the
  total, de-duplicated to the settlement unit.

Each of these caught a false positive in this project. They are documented in
[doc/en/research-findings.md](doc/en/research-findings.md#transferable-methodology).

## Strategy tiers

| Tier | Strategy | Mechanism | Verdict |
|---|---|---|---|
| T0 | Structural arbitrage | Buy every outcome when `Σ ask < 1 - fee` | No real opportunities found |
| T1 | Cross-platform | Polymarket vs Kalshi divergence | Never reached a testable sample |
| T2 | Statistical / model-driven | Bayesian fair value vs market price | Negative after real fees |
| T3 | Market making | Maker quotes around model fair value | Killed by adverse selection |

Detection is WebSocket-driven: `OrderBookMirror` fires on every best bid/ask
change, and T0 goes straight to the risk manager because structural arbitrage is
a millisecond game. Everything else runs on a periodic scan. No tier consults a
language model on the hot path.

See [doc/en/architecture.md](doc/en/architecture.md) for the full design.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # ARB_DRY_RUN=true by default

python run_arb_bot.py              # scan only, no orders
```

No wallet is needed for a dry run. Two independent confirmations
(`ARB_DRY_RUN=false` **and** `LIVE_TRADING_ACK=true`) are required before the
process will submit a real order.

```bash
python run_research.py --limit 20 --show-markets   # research layer alone
python -m research.backtest.run --dataset default  # offline replay
pytest                                             # test suite
```

## Documentation

All documentation lives in [`doc/`](doc/), in English and Chinese.

| Document | Contents |
|---|---|
| [research-findings.md](doc/en/research-findings.md) | **What was tested, what failed, and the methodology that proved it** |
| [architecture.md](doc/en/architecture.md) | Tier design, execution flow, module map, cross-cutting contracts |
| [configuration.md](doc/en/configuration.md) | Full environment variable reference |
| [operations.md](doc/en/operations.md) | Running, sidecar workers, dry-run checklist, observability |
| [backtesting.md](doc/en/backtesting.md) | Tick recording, the replay runner, how to avoid fooling yourself |
| [ai-configuration.md](doc/en/ai-configuration.md) | LLM providers and the TypeSafe scoring model — all out-of-process |
| [pending-validations.md](doc/en/pending-validations.md) | Features shipped behind flags, with graduation criteria |
| [references.md](doc/en/references.md) | Data sources, APIs, prior art |

## Project layout

```
polymarket_arb/      main package: config, feeds, detection, risk, execution
  └── strategies/    T0–T3 plus Kelly sizing, optimal stopping, orchestration
research/backtest/   offline backtest runner
research_signal/     research signal collectors / normalizers / scorers
analysis/            one-off empirical studies (see analysis/README.md)
scripts/             sidecar workers and verification entrypoints
tests/               pytest suite, external APIs mocked
doc/                 documentation, en + zh
```

Roughly 71k lines of Python with a 1,190-test suite.

## Requirements

Python 3.10+ (3.11 or newer recommended). `py-clob-client` is version-pinned
because `ExecutionEngine` reaches into a module-private HTTP client attribute;
see [doc/en/operations.md](doc/en/operations.md#install).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) ([中文](doc/zh/contributing.md)). The one
hard rule: **never commit `.env`, a private key, or real wallet data.**

## Disclaimer

- For research and education only. Nothing here is investment advice.
- Every strategy in this repository has been empirically falsified on this
  project's own data. The author has stopped deploying capital to them.
- Structural arbitrage opportunities are vanishingly rare in practice —
  professional market makers correct deviations in milliseconds.
- Statistical arbitrage is only as good as the model; a wrong model loses money
  efficiently.
- Market making carries adverse-selection and inventory risk, both of which
  measurably dominated in this project's data.
- Decide for yourself, at your own risk. The author accepts no liability for any
  loss.
- Comply with Polymarket's terms of service and the law where you live,
  including geographic restrictions.

## License

[MIT](LICENSE)
