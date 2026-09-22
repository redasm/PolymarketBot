[English](../en/ai-configuration.md) · [中文](../zh/ai-configuration.md)

# LLM and scoring-model configuration

## The one thing to understand first

**No language model runs on the trading hot path.** There is no `AIAdvisor`, no
inline LLM gate on entries or exits, and no tier from T0 to T3 that blocks on a
model call. An earlier version of this project did have an inline advisor; it
was removed.

What exists today is an out-of-process pattern:

```
sidecar worker  ──LLM call──→  data/quant_inputs/*.json  ──mtime hot reload──→  main loop
 (minutes)                      (atomic write)                                 (every cycle)
```

The worker (`scripts/scan_quant_strategy_inputs.py`) consumes the `LLMProvider`
abstraction in `polymarket_arb/ai_provider.py` and writes refreshed JSON. The
main loop reads those files through `quant_input_store.py` and uses them inside
`StrategyOrchestrator`'s research overlay. If the worker dies, the model is
down, or a file is mid-write and unparseable, the loop keeps using the last
valid content and keeps trading. Model latency can never stall order-book
scanning.

That property is not a nicety. A 2-second model call inserted into a loop whose
whole premise is millisecond structural detection destroys the strategy.

## Install

```bash
pip install -r requirements-ai.txt
```

Or individually — only the provider you actually use needs to be installed:

```bash
pip install 'openai>=1.30.0'      # OpenAI, DeepSeek, Gemini (OpenAI-compatible)
pip install 'anthropic>=0.30.0'   # Anthropic Claude
pip install 'httpx>=0.27.0'       # Ollama, and the TypeSafe client
```

## Environment variables

These are the only LLM variables the codebase reads:

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `AI_PROVIDER` | str | `openai` | `openai` / `anthropic` / `ollama` / `deepseek` / `gemini` |
| `AI_API_KEY` | str | *(empty)* | Falls back to `OPENAI_API_KEY` when unset. Not needed for Ollama. |
| `AI_API_BASE` | str | *(empty)* | Custom endpoint. Empty uses the provider default. |
| `AI_MODEL` | str | `gpt-4o` | Model name. |
| `AI_TEMPERATURE` | float | `0.1` | Sampling temperature. 0.0–0.2 is the useful range here. |

There is deliberately no `AI_ENABLED`. The workers are separate processes — not
starting one *is* the off switch.

## Provider setup

### OpenAI

```dotenv
AI_PROVIDER=openai
AI_API_KEY=sk-...
AI_MODEL=gpt-4o          # or gpt-4o-mini
# AI_API_BASE empty → https://api.openai.com/v1
```

### DeepSeek

OpenAI-compatible, roughly a tenth of the cost.

```dotenv
AI_PROVIDER=deepseek
AI_API_KEY=sk-...
AI_API_BASE=https://api.deepseek.com
AI_MODEL=deepseek-chat   # or deepseek-reasoner
```

Keys: <https://platform.deepseek.com>

### Google Gemini

Via the OpenAI-compatible endpoint, so no extra SDK.

```dotenv
AI_PROVIDER=gemini
AI_API_KEY=...
AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
AI_MODEL=gemini-2.0-flash
```

Keys: <https://aistudio.google.com/apikey>

### Anthropic

```dotenv
AI_PROVIDER=anthropic
AI_API_KEY=sk-ant-...
AI_MODEL=claude-sonnet-4-20250514
# AI_API_BASE not needed
```

Tool schemas are converted from OpenAI shape to Anthropic shape automatically
(`function.parameters` → `input_schema`).

Keys: <https://console.anthropic.com>

### Ollama (local, free)

```dotenv
AI_PROVIDER=ollama
AI_API_BASE=http://localhost:11434
AI_MODEL=qwen2.5:7b
# AI_API_KEY unused
```

```bash
ollama pull qwen2.5:7b
ollama serve
```

Some Ollama models do not support function calling; the provider falls back to
JSON mode automatically. Output quality in JSON mode degrades quickly below 7B.

## The provider interface

```python
class LLMProvider(ABC):
    async def chat(self, messages, *, temperature, json_mode, tools) -> LLMResponse
    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float

@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    latency_ms: float
    tool_calls: list
```

| Implementation | Covers | Requires |
|---|---|---|
| `OpenAIProvider` | OpenAI, DeepSeek, Gemini, and any OpenAI-compatible endpoint | `openai` |
| `AnthropicProvider` | Anthropic Claude | `anthropic` |
| `OllamaProvider` | any locally served Ollama model | `httpx` |

Errors are typed: `LLMConfigurationError` for bad or missing configuration,
`LLMRequestError` for transport and API failures — both subclasses of
`LLMProviderError`, so a worker can distinguish "you configured this wrong" from
"the network is having a bad day".

DeepSeek and Gemini need no separate implementation because they speak the
OpenAI Chat Completions protocol; only `AI_API_BASE` differs. That is the point
of the abstraction.

## What the workers produce

```bash
# Pull candidate same-event relations from Gamma for LLM review
python scripts/scan_quant_strategy_inputs.py logical-candidates \
  --fetch-gamma --event-limit 100 \
  --output data/quant_inputs/logical_candidates.json

# Have the configured provider strictly filter them down to genuine
# containment / upper-bound relations
python scripts/scan_quant_strategy_inputs.py logical-rules-llm \
  --candidates data/quant_inputs/logical_candidates.json \
  --output data/quant_inputs/logical_constraints.json
```

Other subcommands cover event baselines, wallet observations, and wallet
profiles; see [operations.md](operations.md#sidecar-workers).

Every `--output` write is atomic, and the main loop picks up the new file on its
next scan cycle without a restart.

## TypeSafe Jev — a scoring model, not an LLM provider

Jev is not another `AI_PROVIDER` option. It does not generate text. Given a
`state`, it answers typed questions in parallel — Noul (yes/no probability),
Choice (pick one, with a distribution), Score (ordinal grade) — and returns
calibrated probabilities.

The client is `polymarket_arb/typesafe_provider.py`, which calls
`POST /v1/systemone` directly over `httpx` rather than using the vendor SDK, so
429/529 responses and token usage land precisely in telemetry.

```dotenv
TYPESAFE_API_KEY=            # https://console.typesafe.ai/settings/keys
TYPESAFE_MODEL=jev-1.13.0    # pin it; jev-latest drifts and breaks calibration
TYPESAFE_RPS=10              # client token bucket (published limit 1200 RPM, may change)
TYPESAFE_TIMEOUT_SEC=10
TYPESAFE_MAX_RETRIES=3
TYPESAFE_BASE_URL=           # empty → https://api.typesafe.ai
```

Shadow mode only — it does not write `data/quant_inputs/` and does not enter the
trading loop:

```bash
# Candidates only, no API key needed: non-numeric binary markets resolving in 0.25–45 days
python scripts/shadow_typesafe_baselines.py scan --dry-run --limit 100

# Score every 30 minutes → data/telemetry/typesafe_shadow.ndjson
python scripts/shadow_typesafe_baselines.py scan --repeat-interval-sec 1800

# Backfill settlements → data/telemetry/typesafe_shadow_settlements.ndjson
python scripts/shadow_typesafe_baselines.py backfill
```

Design rules that matter:

- Price-threshold, percentage, temperature and count markets are filtered out by
  `is_numeric_market` — the vendor's own documentation says "Jev is not a
  calculator". Those keep using `fair_value_model.py`.
- The `state` contains only the question, resolution criteria,
  `days_to_resolution` (date arithmetic done in code), and optionally news
  headlines. **It must not contain the order book price**, or the Brier
  comparison against the book stops being independent.
- Each request appends a row to `data/telemetry/typesafe_requests.ndjson`
  (usage, latency, 429 count, `request_id`); each round writes
  `typesafe_shadow_status.json`.
- Cost: $0.042 per million input tokens, output free. 100 markets every 30
  minutes is around $0.01/day.

**Result**: this was tested over 93 settled markets and lost badly to the order
book — Brier skill −1.49, AUC 0.589 versus 0.936. Do not write
`source="typesafe_jev"` baselines into `event_baselines.json`. Full numbers,
including the contamination probe and the follow-up information-layer audit, are
in
[research-findings.md](research-findings.md#typesafe-jev--can-a-calibrated-scoring-model-beat-the-book).

## Cost control

There is no built-in daily budget cap, because there is no in-loop caller to cap
— cost is bounded by how often you schedule the workers.

| Workload | Frequency | Rough cost |
|---|---|---|
| `logical-rules-llm` over ~100 candidates | on demand | cents per run |
| `event-baselines-auto` | every 30–60 min | low single-digit $/month on a cheap model |
| TypeSafe shadow, 100 markets | every 30 min | ~$0.01/day |

To spend less: run the workers less often, switch `AI_MODEL` to a cheaper model
(`gpt-4o-mini`, `deepseek-chat`), or run Ollama locally for zero marginal cost.

## FAQ

**Does disabling this affect the strategies?**
No. The main loop reads JSON files. Absent files mean the logical-constraint,
event-calendar and wallet-alpha tiers produce no signals; T0/T2/T3 are
untouched.

**Can I use two providers at once?**
Not within one worker process. Run two workers with different `--dotenv-path`
files if you need it.

**Where do I see what the model decided?**
The output JSON under `data/quant_inputs/`, and the overlay counters at
`strategy_status.meta.research_overlay` (`applied / boosted / penalized /
vetoed`).

**Can LLM latency delay T0?**
No. Different process, no shared lock. The main loop only ever reads files.
