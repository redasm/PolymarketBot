# Contributing

[English](CONTRIBUTING.md) · [中文](doc/zh/contributing.md)

Thanks for taking a look. This is a research codebase for a live trading system,
so a few of the rules below are stricter than usual.

## Never commit secrets

- `.env`, private keys, API keys, and real wallet data must never enter the
  repository or an issue.
- `.gitignore` covers `.env`, `.env.*` (except the example), `*.pem` and `*.key`.
  Do not work around it.
- A leaked `PRIVATE_KEY` drains a wallet immediately. If it happens, rotate the
  key first and worry about the git history second.
- Recorded telemetry under `data/` contains your own trading behaviour. It is
  gitignored; keep it that way.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-base.txt -r requirements-dev.txt
pytest
```

`pythonpath = ["."]` is set in `pyproject.toml`, so no install step is needed to
run the tests. External APIs are mocked; the suite needs no network and no
wallet.

## Before you open a pull request

- `pytest` passes.
- New behaviour has tests. `tests/conftest.py` has the shared fixtures.
- Changes on network, API, partial-fill or reconnect paths have exception
  handling. This code runs unattended against real money.
- Hot-path code emits structured logs with `trace_id` continuity.
- No new runtime dependency unless you can explain why the standard library and
  the existing dependencies are insufficient. Say so in the PR description.
- Bug fixes touch only the relevant code. Please do not fold in unrelated
  refactoring.

## Things that need extra care

Changes in these areas affect real orders and real money. Describe your
reasoning in the PR, not just the diff:

- Signal generation, position sizing, risk management, execution
- Anything that changes when or how an order is submitted
- The cross-cutting contracts documented in
  [doc/en/architecture.md](doc/en/architecture.md#cross-cutting-contracts) —
  order type by side, resolving position outcome from the trade rather than the
  signal, and the risk-state synchronisation windows. Each of those corresponds
  to a bug that reached production.

## Empirical claims

If a change is justified by a performance claim, the claim needs evidence, and
the evidence needs to survive the checks in
[doc/en/backtesting.md](doc/en/backtesting.md#making-a-backtest-trustworthy):

- delay injection, with the surviving fraction reported
- fees resolved per market, not from the default rate
- exits priced at executable levels, not mid
- results de-duplicated to the settlement unit
- median per-trade result and win fraction alongside the total

"The backtest is positive" is not sufficient. Several strategies in this
repository had positive backtests and were still wrong.

Never propose a threshold without having looked at the distribution of the
underlying data first.

## Documentation

Documentation lives in `doc/en/` and `doc/zh/`, with matching section structure
so the two can be diffed. If you change one, change the other — or say in the PR
that you cannot, and which file is now behind.

Code, identifiers and log strings stay in English in both versions.

## Style

Match the surrounding code: same naming, same comment density, same idioms.
There is no enforced formatter.
