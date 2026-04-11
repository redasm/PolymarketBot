# AI 决策引擎 — 配置与使用文档

## 目录

- [概述](#概述)
- [架构设计](#架构设计)
- [快速开始](#快速开始)
- [环境变量参考](#环境变量参考)
- [Provider 配置](#provider-配置)
  - [OpenAI](#openai)
  - [DeepSeek](#deepseek)
  - [Google Gemini](#google-gemini)
  - [Anthropic Claude](#anthropic-claude)
  - [Ollama 本地部署](#ollama-本地部署)
- [模块详解](#模块详解)
  - [ai_provider.py — LLM 抽象层](#ai_providerpy--llm-抽象层)
  - [ai_advisor.py — 决策核心](#ai_advisorpy--决策核心)
  - [ai_context.py — 上下文构建器](#ai_contextpy--上下文构建器)
  - [ai_prompts.py — Prompt 模板与 Schema](#ai_promptspy--prompt-模板与-schema)
- [决策场景](#决策场景)
  - [市场评估 (Market Evaluation)](#市场评估-market-evaluation)
  - [执行决策 (Execution Decision)](#执行决策-execution-decision)
  - [动态风控 (Risk Adjustment)](#动态风控-risk-adjustment)
- [安全约束](#安全约束)
- [成本控制](#成本控制)
- [自动降级机制](#自动降级机制)
- [Dashboard 监控](#dashboard-监控)
- [数据流](#数据流)
- [调参建议](#调参建议)
- [常见问题](#常见问题)

---

## 概述

AI 决策引擎是 Polymarket 套利机器人的可选模块，利用大语言模型 (LLM) 进行：

1. **市场评估** — 批量扫描活跃市场，识别定价偏差 (mispricing)
2. **执行决策** — 对规则引擎检测到的机会，判断是否执行、仓位多大
3. **动态风控** — 根据市场状态自动调整敞口上限、止损线等参数

核心原则：**AI 建议 + 规则兜底**。AI 的每个决策都必须通过现有 `RiskManager` 硬校验，AI 不能绕过风控。`AI_ENABLED=false` 时整个链路零开销。

---

## 架构设计

```
┌──────────────────────────────────────────────────────────────┐
│                       数据采集层                               │
│  WebSocket 订单簿 → EnhancedBookStore                        │
│  Gamma API → MarketScanner                                    │
│  VolEstimator (多尺度波动率)                                   │
└──────────────┬───────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────┐
│                    AI 决策层 (新增)                            │
│                                                              │
│  MarketContextBuilder ──→ AIAdvisor ──→ AIDecision           │
│       (收集压缩数据)    (调用 LLM)    (结构化输出)             │
│                            │                                  │
│                     LLMProvider 抽象层                         │
│              ┌─────────┼──────────┐                           │
│          OpenAI   Anthropic   Ollama                          │
│        (+ DeepSeek, Gemini)                                   │
└──────────────┬───────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────┐
│                       执行层                                  │
│  T0 结构套利 → 不经 AI，直达 RiskManager (毫秒级)              │
│  AI 信号 → StrategyOrchestrator → RiskManager → ExecutionEngine│
└──────────────────────────────────────────────────────────────┘
```

关键：**AI 不在热路径上**。T0 结构性套利（毫秒级）仍走原有规则引擎，AI 只介入 T2/T3 等可容忍 1-3 秒延迟的策略。

---

## 快速开始

### 1. 安装依赖

```bash
# 必须安装（OpenAI / DeepSeek / Gemini 共用）
pip install openai>=1.30.0

# 可选：如需使用 Anthropic Claude
pip install anthropic>=0.30.0

# 可选：如需使用 Ollama 本地模型
pip install httpx>=0.27.0
```

按需导入：只有实际使用的 provider 需要安装对应包，其他不装不报错。

### 2. 配置 .env

```env
# 开启 AI 决策
AI_ENABLED=true
AI_PROVIDER=openai
AI_API_KEY=sk-your-key-here
AI_MODEL=gpt-4o
```

### 3. 启动

无需额外操作，主循环自动检测 `AI_ENABLED=true` 后加载 AI 模块。日志中会出现：

```
AI 决策引擎已启用: provider=openai, model=gpt-4o, interval=30s
```

---

## 环境变量参考

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `AI_ENABLED` | bool | `false` | AI 决策引擎总开关。关闭时整个 AI 链路不加载，零开销 |
| `AI_PROVIDER` | str | `openai` | LLM 提供商：`openai` / `anthropic` / `ollama` / `deepseek` / `gemini` |
| `AI_API_KEY` | str | *(空)* | LLM API Key。Ollama 本地部署不需要 |
| `AI_API_BASE` | str | *(空)* | 自定义 API 端点 URL。留空使用 provider 默认值 |
| `AI_MODEL` | str | `gpt-4o` | 使用的模型名称 |
| `AI_TEMPERATURE` | float | `0.1` | 采样温度。越低输出越确定性，推荐 0.05-0.2 |
| `AI_EVAL_INTERVAL_SEC` | float | `30` | AI 评估周期（秒）。越小越灵敏但 API 成本越高 |
| `AI_MAX_COST_PER_DAY` | float | `5.0` | 日 API 成本上限（USD）。超限后 AI 自动降级为只读 |
| `AI_OVERRIDE_RISK` | bool | `false` | AI 是否可以动态调整风控参数。受 [50%, 150%] 硬上限约束 |

> 兼容性: `AI_API_KEY` 未设置时会 fallback 到 `OPENAI_API_KEY`。

---

## Provider 配置

### OpenAI

最成熟的 function calling 支持，推荐首选。

```env
AI_PROVIDER=openai
AI_API_KEY=sk-xxx
AI_MODEL=gpt-4o          # 或 gpt-4o-mini（便宜 15 倍）
# AI_API_BASE 留空，自动使用 https://api.openai.com/v1
```

支持的模型：`gpt-4o`、`gpt-4o-mini`、`gpt-4-turbo`、`o1`、`o3-mini` 等。

### DeepSeek

极低成本（约 OpenAI 的 1/10），兼容 OpenAI 格式。

```env
AI_PROVIDER=deepseek
AI_API_KEY=sk-xxx
AI_API_BASE=https://api.deepseek.com
AI_MODEL=deepseek-chat     # 或 deepseek-reasoner
```

注册获取 Key：https://platform.deepseek.com

### Google Gemini

通过 OpenAI 兼容端点接入，无需额外 SDK。

```env
AI_PROVIDER=gemini
AI_API_KEY=AIzaXxx
AI_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
AI_MODEL=gemini-2.0-flash   # 或 gemini-2.5-pro
```

注册获取 Key：https://aistudio.google.com/apikey

### Anthropic Claude

推理质量优秀，需要单独安装 `anthropic` 包。

```env
AI_PROVIDER=anthropic
AI_API_KEY=sk-ant-xxx
AI_MODEL=claude-sonnet-4-20250514
# AI_API_BASE 不需要设置
```

注册获取 Key：https://console.anthropic.com

tool schema 会自动从 OpenAI 格式转换为 Anthropic 格式（`function.parameters` → `input_schema`），无需手动处理。

### Ollama 本地部署

完全免费（仅电费），需要本机或服务器上运行 Ollama。

```env
AI_PROVIDER=ollama
AI_API_BASE=http://localhost:11434    # 默认值
AI_MODEL=qwen2.5:7b                  # 或 llama3.1:8b, mistral:7b
# AI_API_KEY 留空即可
```

安装 Ollama：https://ollama.com

```bash
# 拉取模型
ollama pull qwen2.5:7b

# 启动服务（默认端口 11434）
ollama serve
```

注意：Ollama 部分模型不支持 function calling，会自动降级为 JSON mode。推荐使用 `qwen2.5:7b` 或更大的模型以获得更好的 JSON 输出质量。

---

## 模块详解

### ai_provider.py — LLM 抽象层

所有 LLM 交互都通过统一的 `LLMProvider` 接口：

```python
class LLMProvider(ABC):
    async def chat(messages, *, temperature, json_mode, tools) -> LLMResponse
    def estimate_cost(model, input_tokens, output_tokens) -> float

@dataclass
class LLMResponse:
    content: str          # 文本响应
    input_tokens: int     # 输入 token 数
    output_tokens: int    # 输出 token 数
    model: str            # 实际使用的模型
    latency_ms: float     # 延迟（毫秒）
    tool_calls: list      # function calling 结果
```

三个具体实现：

| 类 | 覆盖的 Provider | 安装依赖 |
|---|---|---|
| `OpenAIProvider` | OpenAI, DeepSeek, Gemini, Together 等 OpenAI 兼容 API | `openai` |
| `AnthropicProvider` | Anthropic Claude | `anthropic` |
| `OllamaProvider` | 本地 Ollama 部署的任意模型 | `httpx` |

工厂函数 `create_provider(config)` 根据 `AI_PROVIDER` 自动创建对应实例。

### ai_advisor.py — 决策核心

`AIAdvisor` 是 AI 决策的核心入口，提供三个主方法：

| 方法 | 功能 | 调用频率 |
|------|------|---------|
| `evaluate_markets(context)` | 批量扫描市场，返回 `list[AIDecision]` | 每 `AI_EVAL_INTERVAL_SEC` 秒 |
| `evaluate_execution(context, opp)` | 对具体机会做执行决策 | 仅有机会时 |
| `adjust_risk_params(context)` | 建议风控参数调整 | 同上（仅 `AI_OVERRIDE_RISK=true`） |

内置功能：
- **日成本追踪**：每次 LLM 调用自动累计成本，超 `AI_MAX_COST_PER_DAY` 后停止调用
- **自动降级**：连续 5 笔亏损后降级为只读模式
- **决策历史**：最近 200 条决策记录，可通过 Dashboard 查看

```python
AIDecision:
    action: str              # "BUY_YES" | "BUY_NO" | "SELL_YES" | "SELL_NO" | "HOLD" | "CLOSE"
    market_id: str           # 市场 ID
    confidence: float        # 0-1 置信度
    recommended_size_pct: float  # 占可用资金的比例 (0-0.1)
    reasoning: str           # AI 推理过程（写入日志和 Dashboard）
    risk_adjustment: dict    # 动态风控建议
    urgency: float           # 0-1 执行紧迫度
```

### ai_context.py — 上下文构建器

`MarketContextBuilder` 从各数据源收集信息并压缩为 LLM 友好的格式：

| 数据源 | 提取内容 |
|--------|---------|
| `EnhancedBookStore` | yes/no 中间价、spread、连接状态 |
| `VolEstimator` | fast/slow/blend sigma、数据就绪状态 |
| `EdgeEngine` | 最近的 edge 信号（方向、bps、置信度） |
| `RiskState` | 总敞口、持仓数、日盈亏、熔断状态 |
| `MarketScanner` | 活跃市场摘要（问题、价格、流动性、24h 量） |

输出结构：

```python
MarketContext:
    timestamp: float
    active_markets: list[dict]    # 最多 10 个精简市场摘要
    orderbook_summary: dict       # BookStore 快照
    volatility: dict              # VolEstimator 快照
    edge_signals: list[dict]      # 最近 5 条 edge 信号
    recent_trades: list[dict]     # 最近 10 笔交易
    risk_state: dict              # 当前风控状态
```

`to_prompt_text()` 方法将上述结构序列化为紧凑的 Markdown 文本，控制 token 量在 **2000-4000** 以内。

### ai_prompts.py — Prompt 模板与 Schema

三种场景各有一套完整的模板：

| 场景 | System Prompt | User Prompt | Tool Schema | JSON Fallback |
|------|--------------|-------------|-------------|---------------|
| 市场评估 | `SYSTEM_BASE` | `MARKET_EVALUATION_PROMPT` | `MARKET_EVAL_TOOL` | `JSON_SCHEMA_MARKET_EVAL` |
| 执行决策 | `SYSTEM_BASE` | `EXECUTION_DECISION_PROMPT` | `EXECUTION_TOOL` | `JSON_SCHEMA_EXECUTION` |
| 风控调整 | `SYSTEM_BASE` | `RISK_ADJUSTMENT_PROMPT` | `RISK_TOOL` | `JSON_SCHEMA_RISK` |

支持 function calling 的 provider（OpenAI、Anthropic）优先使用 tool/function 模式保证结构化输出；不支持的 provider（部分 Ollama 模型）降级为 JSON mode。

---

## 决策场景

### 市场评估 (Market Evaluation)

**触发时机**：每 `AI_EVAL_INTERVAL_SEC` 秒自动执行一次

**输入**：当前所有活跃市场的摘要、订单簿状态、波动率、近期 edge 信号

**输出**：0 到多个 `AIDecision`，包含具体的交易建议

**执行路径**：
```
AIAdvisor.evaluate_markets()
  → AIDecision 列表
    → 转为 StrategySignal (tier=STATISTICAL_ARB)
      → StrategyOrchestrator.submit_signal()
        → process_signals() 排序
          → RiskManager.pre_trade_check()
            → ExecutionEngine (如果通过)
```

### 执行决策 (Execution Decision)

**触发时机**：规则引擎检测到套利机会后，询问 AI 是否执行

**输入**：机会详情 + 市场上下文 + 当前风控状态

**输出**：单个 `AIDecision`，推荐执行还是跳过，以及建议仓位大小

### 动态风控 (Risk Adjustment)

**触发时机**：与市场评估同频率，仅在 `AI_OVERRIDE_RISK=true` 时生效

**输入**：当前风控状态 + 市场环境

**输出**：调整因子

| 参数 | 范围 | 说明 |
|------|------|------|
| `max_exposure_factor` | 0.5 - 1.5 | 乘以 `RISK_MAX_TOTAL_EXPOSURE` 的基准值 |
| `daily_loss_factor` | 0.5 - 1.5 | 乘以 `RISK_MAX_DAILY_LOSS` 的基准值 |

示例：基准 `RISK_MAX_TOTAL_EXPOSURE=500`，AI 建议 `max_exposure_factor=1.2`，则有效上限变为 $600。

---

## 安全约束

| 约束 | 说明 |
|------|------|
| **RiskManager 硬校验** | 所有 AI 决策必须通过 `pre_trade_check()`，不能绕过 |
| **风控调整硬上限** | AI 调整的参数不能超过 `.env` 配置原始值的 150%，不能低于 50% |
| **日成本上限** | 超过 `AI_MAX_COST_PER_DAY` 后自动停止 LLM 调用 |
| **连续亏损降级** | AI 建议的交易连续 5 笔亏损后，自动降级为只读（只记录不执行） |
| **推理日志** | 所有决策的 `reasoning` 字段写入日志，便于事后审计 |
| **零开销** | `AI_ENABLED=false` 时，不创建任何 AI 对象，不导入 LLM SDK |
| **仓位上限** | Prompt 层硬约束：单个推荐不超过可用资金的 10% |

---

## 成本控制

### 预估成本

| 决策类型 | 频率 | 预估 token/次 | 默认模型 | 月成本 |
|----------|------|--------------|---------|--------|
| 市场评估 | 每 30s | ~3,000 | gpt-4o | ~$30-50 |
| 执行决策 | 仅有机会时 | ~1,500 | gpt-4o | ~$5-10 |
| 风控调整 | 每 30s | ~1,000 | gpt-4o | ~$5-10 |
| **合计** | | | | **~$40-60/月** |

### 各 Provider 对比

| Provider | 模型 | 预估月成本 | 延迟 | 特点 |
|----------|------|-----------|------|------|
| OpenAI | gpt-4o | ~$40-60 | 1-3s | 最成熟的 function calling |
| OpenAI | gpt-4o-mini | ~$5-10 | 0.5-1s | 性价比最高的云端方案 |
| DeepSeek | deepseek-chat | ~$2-5 | 1-2s | 极低成本，OpenAI 兼容 |
| Gemini | gemini-2.0-flash | ~$3-8 | 0.5-1.5s | Google 免费额度可用 |
| Anthropic | claude-sonnet-4-20250514 | ~$30-50 | 1-3s | 推理质量好 |
| Ollama | qwen2.5:7b | **$0**（仅电费） | 0.5-2s | 需要 GPU 服务器 |

### 降低成本的方法

1. **调大 `AI_EVAL_INTERVAL_SEC`**：从 30s 改为 60s，成本减半
2. **换用便宜模型**：`gpt-4o` → `gpt-4o-mini` 或 `deepseek-chat`
3. **Ollama 本地部署**：完全免费，需 6GB+ 显存
4. **设置 `AI_MAX_COST_PER_DAY`**：硬性日成本上限

---

## 自动降级机制

AI 模块有两层自动降级：

### 1. 成本降级

当日 API 累计成本达到 `AI_MAX_COST_PER_DAY` 后：
- `_check_budget()` 返回 `False`
- 所有 `evaluate_*` 方法直接返回空结果或 HOLD
- 日志警告：`AI 日成本已达上限: $X.XX >= $Y.YY`
- 次日 UTC 00:00 自动重置

### 2. 亏损降级

当 AI 建议的交易连续亏损 5 笔后：
- `is_degraded` 属性变为 `True`
- `should_evaluate()` 返回 `False`，不再调用 LLM
- 日志警告：`AI 已降级: 连续 N 笔亏损`
- 需手动调用 `reset_degradation()` 解除

---

## Dashboard 监控

AI 状态通过 `/api/ai` 端点暴露：

```
GET http://127.0.0.1:8077/api/ai
```

响应示例：

```json
{
  "status": {
    "enabled": true,
    "provider": "openai",
    "model": "gpt-4o",
    "degraded": false,
    "daily_cost_usd": 0.0342,
    "cost_limit_usd": 5.0,
    "call_count": 12,
    "consecutive_losses": 0,
    "recent_decisions": 8
  },
  "decisions": [
    {
      "type": "market_eval",
      "timestamp": 1712880000.0,
      "action": "BUY_YES",
      "market_id": "0x1234abcd",
      "confidence": 0.72,
      "recommended_size_pct": 0.05,
      "reasoning": "Orderbook imbalance strongly favors YES, spread is narrow...",
      "urgency": 0.6
    }
  ]
}
```

---

## 数据流

完整的 AI 决策数据流：

```
1. 主循环每 scan_interval_sec 执行一次
        │
2. EdgeEngine.evaluate() 产生 edge 信号
        │
3. 检查 ai_advisor.should_evaluate()
   (距上次 >= AI_EVAL_INTERVAL_SEC 且未降级)
        │
4. MarketContextBuilder.build()
   收集: BookStore + VolEstimator + EdgeSignals + RiskState
   压缩为 ~2000-4000 token 的 MarketContext
        │
5. AIAdvisor.evaluate_markets(context)
   → 组装 messages: SYSTEM_BASE + MARKET_EVALUATION_PROMPT + context
   → LLMProvider.chat() (带 MARKET_EVAL_TOOL schema)
   → 解析 function calling 返回值 → list[AIDecision]
        │
6. AIDecision → StrategySignal (tier=T2)
   → StrategyOrchestrator.submit_signal()
        │
7. StrategyOrchestrator.process_signals()
   按 tier + urgency × edge 排序
        │
8. RiskManager.pre_trade_check() 硬校验
        │
9. ExecutionEngine.execute_arbitrage() (如果通过)
        │
10. Dashboard: /api/ai 展示决策历史和状态
```

---

## 调参建议

### 新手推荐配置

```env
AI_ENABLED=true
AI_PROVIDER=openai
AI_MODEL=gpt-4o-mini          # 便宜且快
AI_EVAL_INTERVAL_SEC=60       # 1 分钟评估一次
AI_MAX_COST_PER_DAY=2.0       # 每天最多 $2
AI_OVERRIDE_RISK=false         # 先不开动态风控
AI_TEMPERATURE=0.1
```

预估月成本：~$3-5

### 进阶配置

```env
AI_ENABLED=true
AI_PROVIDER=openai
AI_MODEL=gpt-4o               # 更强的推理能力
AI_EVAL_INTERVAL_SEC=30       # 30 秒评估
AI_MAX_COST_PER_DAY=10.0      # 提高上限
AI_OVERRIDE_RISK=true          # 允许动态风控
AI_TEMPERATURE=0.05            # 更确定性的输出
```

预估月成本：~$40-60

### 零成本方案

```env
AI_ENABLED=true
AI_PROVIDER=ollama
AI_API_BASE=http://localhost:11434
AI_MODEL=qwen2.5:7b
AI_EVAL_INTERVAL_SEC=15        # 本地模型可以更频繁
AI_MAX_COST_PER_DAY=999        # 无成本限制
AI_OVERRIDE_RISK=true
```

前提：需要有 NVIDIA GPU (6GB+ 显存) 或 Apple Silicon Mac。

---

## 常见问题

### Q: AI 关闭后会影响原有策略吗？

不会。`AI_ENABLED=false` 时，不创建任何 AI 对象、不导入 LLM SDK、不产生任何开销。原有的 T0 结构套利、edge 引擎等完全不受影响。

### Q: 能否同时使用多个 Provider？

当前版本只支持单 provider。如需切换，修改 `.env` 中的 `AI_PROVIDER` 后重启即可，无需改代码。

### Q: DeepSeek / Gemini 为什么不需要单独实现？

它们都兼容 OpenAI 的 Chat Completions API 格式，直接复用 `OpenAIProvider`，只需通过 `AI_API_BASE` 指向不同的端点。这是 `ai_provider.py` 的关键设计。

### Q: AI 延迟会影响 T0 结构套利吗？

不会。T0 结构套利（二元市场 Yes+No<1）仍走原有规则引擎，不经过 AI 层。AI 仅介入 T2 统计套利和 T3 做市策略，这些策略可以容忍 1-3 秒延迟。

### Q: 日成本超限后怎么办？

AI 自动停止 LLM 调用，所有 `evaluate_*` 方法返回空/HOLD。原有规则引擎不受影响。次日 UTC 00:00 自动重置成本计数。

### Q: 如何查看 AI 做了什么决策？

1. **日志**：所有决策的 reasoning 写入 `arb_bot.log`
2. **Dashboard**：访问 `http://127.0.0.1:8077/api/ai` 查看状态和最近 30 条决策
3. **代码**：`ai_advisor.decision_history` 属性保存最近 200 条记录

### Q: Ollama 推荐什么模型？

- **qwen2.5:7b** — 推荐，JSON 输出质量好，7B 参数量适中
- **llama3.1:8b** — Meta 出品，通用能力强
- **mistral:7b** — 速度快，适合低延迟场景
- **qwen2.5:14b** — 更强的推理能力，需要 10GB+ 显存

### Q: 如何手动解除 AI 降级？

通过代码调用 `ai_advisor.reset_degradation()`，或重启机器人。后续版本会增加 Dashboard 操作按钮。

---

## 文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `polymarket_arb/ai_provider.py` | 新增 | LLM Provider 抽象层（OpenAI / Anthropic / Ollama） |
| `polymarket_arb/ai_advisor.py` | 新增 | AI 决策核心（市场评估 / 执行决策 / 风控调整） |
| `polymarket_arb/ai_context.py` | 新增 | 市场上下文构建器（数据收集 + token 压缩） |
| `polymarket_arb/ai_prompts.py` | 新增 | Prompt 模板 + Function Calling Schema |
| `polymarket_arb/config.py` | 修改 | ArbConfig 新增 9 个 AI 配置字段 |
| `polymarket_arb/models.py` | 修改 | 新增 AIDecision / MarketContext 数据类 |
| `polymarket_arb/main_loop.py` | 修改 | 接入 AIAdvisor + StrategyOrchestrator |
| `polymarket_arb/risk_manager.py` | 修改 | 新增 apply_ai_adjustment() 动态风控 |
| `polymarket_arb/dashboard_api.py` | 修改 | 新增 /api/ai 端点 + AI 状态字段 |
| `.env.example` | 修改 | 新增 AI 环境变量 |
| `requirements.txt` | 修改 | 新增 openai / anthropic / httpx 依赖 |
