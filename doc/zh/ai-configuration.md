[English](../en/ai-configuration.md) · [中文](../zh/ai-configuration.md)

# LLM 与打分模型配置

## 先理解这一件事

**交易热路径上没有任何语言模型。** 没有 `AIAdvisor`，入场和退出上没有内联 LLM 闸门，
T0 到 T3 没有任何一层会阻塞在模型调用上。早期版本确实有过内联 advisor，已经移除。

现在的形态是纯旁路：

```
旁路 worker  ──LLM 调用──→  data/quant_inputs/*.json  ──mtime 热加载──→  主循环
  （分钟级）                    （原子写入）                              （每周期）
```

worker（`scripts/scan_quant_strategy_inputs.py`）使用
`polymarket_arb/ai_provider.py` 里的 `LLMProvider` 抽象，写出刷新后的 JSON。主循环通过
`quant_input_store.py` 读这些文件，在 `StrategyOrchestrator` 的 research overlay 里使用。
worker 挂了、模型宕了、文件写到一半解析不了 —— 主循环继续用上一次有效内容继续跑。
模型延迟永远不可能卡住盘口扫描。

这不是锦上添花。在一个前提就是毫秒级结构性检测的循环里插进一个 2 秒的模型调用，策略直接作废。

## 安装

```bash
pip install -r requirements-ai.txt
```

或按需单装 —— 只有你实际用的 provider 需要装：

```bash
pip install 'openai>=1.30.0'      # OpenAI、DeepSeek、Gemini（OpenAI 兼容）
pip install 'anthropic>=0.30.0'   # Anthropic Claude
pip install 'httpx>=0.27.0'       # Ollama，以及 TypeSafe 客户端
```

## 环境变量

代码实际读取的 LLM 变量只有这些：

| 变量 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `AI_PROVIDER` | str | `openai` | `openai` / `anthropic` / `ollama` / `deepseek` / `gemini` |
| `AI_API_KEY` | str | *(空)* | 未设时回退 `OPENAI_API_KEY`。Ollama 不需要。 |
| `AI_API_BASE` | str | *(空)* | 自定义端点，留空用 provider 默认。 |
| `AI_MODEL` | str | `gpt-4o` | 模型名。 |
| `AI_TEMPERATURE` | float | `0.1` | 采样温度。这里有用的区间是 0.0–0.2。 |

有意不提供 `AI_ENABLED`。worker 是独立进程 —— 不启动它就是关闭开关。

## Provider 配置

### OpenAI

```dotenv
AI_PROVIDER=openai
AI_API_KEY=sk-...
AI_MODEL=gpt-4o          # 或 gpt-4o-mini
# AI_API_BASE 留空 → https://api.openai.com/v1
```

### DeepSeek

OpenAI 兼容，成本约十分之一。

```dotenv
AI_PROVIDER=deepseek
AI_API_KEY=sk-...
AI_API_BASE=https://api.deepseek.com
AI_MODEL=deepseek-chat   # 或 deepseek-reasoner
```

获取 key：<https://platform.deepseek.com>

### Google Gemini

走 OpenAI 兼容端点，不需要额外 SDK。

```dotenv
AI_PROVIDER=gemini
AI_API_KEY=...
AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
AI_MODEL=gemini-2.0-flash
```

获取 key：<https://aistudio.google.com/apikey>

### Anthropic

```dotenv
AI_PROVIDER=anthropic
AI_API_KEY=sk-ant-...
AI_MODEL=claude-sonnet-4-20250514
# AI_API_BASE 不需要
```

tool schema 会自动从 OpenAI 格式转成 Anthropic 格式（`function.parameters` → `input_schema`）。

获取 key：<https://console.anthropic.com>

### Ollama（本地，免费）

```dotenv
AI_PROVIDER=ollama
AI_API_BASE=http://localhost:11434
AI_MODEL=qwen2.5:7b
# AI_API_KEY 不用
```

```bash
ollama pull qwen2.5:7b
ollama serve
```

部分 Ollama 模型不支持 function calling，provider 会自动降级到 JSON mode。7B 以下模型在
JSON mode 下的输出质量下降很快。

## Provider 接口

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

| 实现 | 覆盖 | 依赖 |
|---|---|---|
| `OpenAIProvider` | OpenAI、DeepSeek、Gemini，以及任何 OpenAI 兼容端点 | `openai` |
| `AnthropicProvider` | Anthropic Claude | `anthropic` |
| `OllamaProvider` | 任何本地 Ollama 模型 | `httpx` |

错误是分型的：配置错误抛 `LLMConfigurationError`，传输/API 失败抛 `LLMRequestError`，
都继承自 `LLMProviderError` —— worker 因此能区分"你配错了"和"网络今天不太行"。

DeepSeek 和 Gemini 不需要单独实现，因为它们说的就是 OpenAI Chat Completions 协议，只差一个
`AI_API_BASE`。这正是这层抽象的意义。

## worker 产出什么

```bash
# 从 Gamma 拉同事件候选关系，供 LLM 复核
python scripts/scan_quant_strategy_inputs.py logical-candidates \
  --fetch-gamma --event-limit 100 \
  --output data/quant_inputs/logical_candidates.json

# 用已配置的 provider 严格筛选，只保留确定性包含/上界关系
python scripts/scan_quant_strategy_inputs.py logical-rules-llm \
  --candidates data/quant_inputs/logical_candidates.json \
  --output data/quant_inputs/logical_constraints.json
```

其余子命令覆盖事件 baseline、钱包观察、钱包画像，见
[operations.md](operations.md#旁路-worker)。

每个 `--output` 都是原子写入，主循环在下一个扫描周期自动读到新文件，不需要重启。

## TypeSafe Jev —— 打分模型，不是 LLM provider

Jev 不是 `AI_PROVIDER` 的又一个选项。它不生成文本：对一个 `state` 并行回答若干类型化问题 ——
Noul（是/否概率）、Choice（多选一 + 分布）、Score（有序等级）—— 返回校准概率。

客户端是 `polymarket_arb/typesafe_provider.py`，直接用 `httpx` 调 `POST /v1/systemone`
而不是用官方 SDK，这样 429/529 和 token usage 能精确落进 telemetry。

```dotenv
TYPESAFE_API_KEY=            # https://console.typesafe.ai/settings/keys
TYPESAFE_MODEL=jev-1.13.0    # 必须 pin；jev-latest 漂移会让校准阈值失效
TYPESAFE_RPS=10              # 客户端令牌桶（公布限额 1200 RPM，可能调整）
TYPESAFE_TIMEOUT_SEC=10
TYPESAFE_MAX_RETRIES=3
TYPESAFE_BASE_URL=           # 留空 → https://api.typesafe.ai
```

仅影子模式 —— 不写 `data/quant_inputs/`，不进交易循环：

```bash
# 只看候选（不需要 API key）：非数值二元市场、0.25–45 天内结算
python scripts/shadow_typesafe_baselines.py scan --dry-run --limit 100

# 每 30 分钟一轮打标 → data/telemetry/typesafe_shadow.ndjson
python scripts/shadow_typesafe_baselines.py scan --repeat-interval-sec 1800

# 结算回填 → data/telemetry/typesafe_shadow_settlements.ndjson
python scripts/shadow_typesafe_baselines.py backfill
```

几条重要设计规则：

- 价格阈值 / 百分比 / 温度 / 计数类市场被 `is_numeric_market` 过滤掉 —— 官方文档原话
  "Jev is not a calculator"。这些继续走 `fair_value_model.py`。
- `state` 里只放 question、resolution criteria、`days_to_resolution`（日期比较在代码里算好），
  可选新闻标题。**不能放盘口价格**，否则 Brier 对比失去独立性。
- 每次请求写一行 `data/telemetry/typesafe_requests.ndjson`（usage、延迟、429 次数、
  `request_id`）；每轮汇总写 `typesafe_shadow_status.json`。
- 成本：输入 $0.042/Mtok，输出免费。100 个市场每 30 分钟一轮约 $0.01/天。

**结果**：在 93 个已结算市场上实测，显著劣于盘口 —— Brier skill −1.49，AUC 0.589 对 0.936。
不要把 `source="typesafe_jev"` 的 baseline 写进 `event_baselines.json`。完整数字（含污染探针和
后续的信息层体检）见
[research-findings.md](research-findings.md#typesafe-jev--一个校准过的打分模型能打败盘口吗)。

## 成本控制

没有内建的日预算上限，因为没有循环内调用方需要限制 —— 成本完全取决于你多久跑一次 worker。

| 负载 | 频率 | 大致成本 |
|---|---|---|
| `logical-rules-llm` 处理约 100 个候选 | 按需 | 每次几分钱 |
| `event-baselines-auto` | 每 30–60 分钟 | 便宜模型上每月个位数美元 |
| TypeSafe 影子，100 个市场 | 每 30 分钟 | 约 $0.01/天 |

想更省：降低 worker 频率、把 `AI_MODEL` 换成更便宜的（`gpt-4o-mini`、`deepseek-chat`）、
或本地跑 Ollama 做到零边际成本。

## 常见问题

**关掉这些会影响策略吗？**
不会。主循环只读 JSON 文件。文件不存在意味着逻辑约束、事件日历、钱包 alpha 三层不产生信号，
T0/T2/T3 完全不受影响。

**能同时用两个 provider 吗？**
单个 worker 进程内不行。需要的话用不同 `--dotenv-path` 起两个 worker。

**在哪看模型决定了什么？**
`data/quant_inputs/` 下的输出 JSON，以及 `strategy_status.meta.research_overlay` 的计数
（`applied / boosted / penalized / vetoed`）。

**LLM 延迟会拖慢 T0 吗？**
不会。独立进程，没有共享锁，主循环只读文件。
