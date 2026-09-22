# Documentation

English and Chinese versions have matching section structure, so the two can be
diffed against each other. Code, identifiers, environment variable names and log
strings are identical in both.

| | English | 中文 |
|---|---|---|
| **What was tested and what failed** | [research-findings.md](en/research-findings.md) | [research-findings.md](zh/research-findings.md) |
| Architecture | [architecture.md](en/architecture.md) | [architecture.md](zh/architecture.md) |
| Configuration reference | [configuration.md](en/configuration.md) | [configuration.md](zh/configuration.md) |
| Operations | [operations.md](en/operations.md) | [operations.md](zh/operations.md) |
| Recording and backtesting | [backtesting.md](en/backtesting.md) | [backtesting.md](zh/backtesting.md) |
| LLM / scoring model setup | [ai-configuration.md](en/ai-configuration.md) | [ai-configuration.md](zh/ai-configuration.md) |
| Pending validations | [pending-validations.md](en/pending-validations.md) | [pending-validations.md](zh/pending-validations.md) |
| External references | [references.md](en/references.md) | [references.md](zh/references.md) |
| Contributing | [../CONTRIBUTING.md](../CONTRIBUTING.md) | [contributing.md](zh/contributing.md) |

Start with **research-findings**. It determines how to read everything else:
the architecture is real and the infrastructure works, but none of the
strategies it implements produced positive expectancy on this project's own
data.

`analysis/` has its own [README](../analysis/README.md) covering the one-off
empirical scripts behind those findings.
