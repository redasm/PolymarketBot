[English](../en/architecture.md) · [中文](../zh/architecture.md)

# 架构

这个 bot 是一条面向 Polymarket 预测市场的「信号 → 风控 → 执行」多层流水线。整体设计由一个
约束决定：**结构性套利必须在毫秒内检测并执行，其余一切可以按秒计。** 这个分界决定了全部架构。

在把这份文档当成"怎么赚钱"来读之前，先读
[research-findings.md](research-findings.md)。下面描述的每一层都被检验过，且都没有跑出正期望。

## 策略分层

四层执行策略按优先级顺序运行，外加三个可选量化信号源 —— 后者只有在你提供了外部已验证的输入时
才会激活。

| 层 | 策略 | 触发 | 延迟预算 |
|---|---|---|---|
| T0 | 结构性套利 | WebSocket best bid/ask 变动 | 毫秒 |
| T1 | 跨平台套利 | 定时扫描 | 秒 |
| T2 | 统计 / 模型驱动 | 定时扫描 | 秒 |
| T3 | 做市 | 定时扫描 | 秒 |

### T0 —— 结构性套利

**二元市场。** Yes/No token 中恰好有一个结算为 $1.00。当
`ask(Yes) + ask(No) < $1.00 - fee` 时，买入双边即锁定差额。

```
fee_per_leg = fee_rate * price * (1 - price)
profit      = 1.00 - ask_yes - ask_no - Σ fee_per_leg
```

**多结果事件。** 一个事件（比如一次选举）下有 N 个互斥市场。当 `Σ ask_i < 1.00 - Σ fee_i`
时，买下所有结果即锁定差额。对 `neg_risk` 事件，检测器逐腿取 `min(ask_yes, 1 - bid_no)`
中更便宜的那条路径。

检测由 WebSocket 驱动，并在 sizing 之前用 VWAP 深度校验，避免一手好价造出幻影机会。

**最重要的一个实现细节**：交叉簿（`bid > ask`，Polymarket 的数据流会瞬时推出这种状态）天然
满足 `Σ ask < 1`，但它根本不是套利。见
[research-findings.md](research-findings.md#t0--结构性套利) —— 在 7 天真实 tick 数据上，
*每一个*看似 T0 的信号都是交叉簿。

### T1 —— 跨平台套利

同一个现实事件在 Polymarket 和 Kalshi 上经常定价不同，因为用户群不同、两边搬资金有摩擦。

```
poly_yes_ask  = 0.55
kalshi_no_ask = 0.40
total         = 0.95   → 5% 毛利
```

风险是结构性的而非统计性的：结算标准不同、资金锁在两个平台、两边都有对手方风险。T1 需要显式
配对表（`CROSS_PLATFORM_PAIRS_JSON`）；bot 绝不会自己猜测两个市场是同一事件。配对实体一致性
检查只能否决配对，永远不会创建配对。

### T2 —— 统计套利

不等结构性错价出现，而是把若干信号融合成一个模型概率，与盘口比较：

```
FairValueModel (log-normal GBM)   ─┐
订单簿 microprice + imbalance     ─┼→ compute_general_fair_value() → model_prob
价格动量 + 跨市场约束              ┘

model_prob = 0.72  vs  market_price = 0.60  →  edge → Kelly 仓位
```

信号源：5 档订单簿不平衡、microprice、短周期动量、跨市场逻辑约束、UPDOWN 市场的现货锚定
（BTC/ETH "N 分钟内高于 X"）、事件日历 baseline、钱包 alpha 观察。

入场是单腿 FOK。退出由 `T2ExitManager` 管理 —— 它才是把正期望入场变成已实现 PnL 的那一环；
没有它，方向性持仓就只能一路骑到结算。

### T3 —— 做市

在模型公允价两侧挂限价单：

- Maker 费率为 0%（对比 taker 费率），所以每一对成交起手就领先。
- 报价落在激励带 `[mid - δ, mid + δ]` 内可获得 Polymarket 流动性奖励。
- Spread 由 `VolEstimator` 驱动：`base + sigma_blend × 2 + inventory_skew`，波动上升时自动
  拉宽。

T3 还带抗狙击保护（跳变暂停、稳定确认、报价滤波、成交后冷却、追价上限）和一个
`/orders-scoring` 审计，用来回答"挂着的单到底有没有在赚奖励"。

## 执行流程

```
WebSocket 订单簿推送
        │
        ▼
OrderBookMirror 更新 ──→ EnhancedBookStore（microprice / imbalance / depth）
        │
        ▼
  best bid/ask 变了？
        │ 是
        ▼
T0 检测（毫秒级）
  ├─ 二元:   ask_yes + ask_no < 1 - fee?
  └─ 多结果: Σ ask_i        < 1 - Σ fee?
        │
        └─ 命中 → VWAP 深度校验 → Kelly 仓位 → RiskManager → ExecutionEngine

定时扫描（默认 5s）
  ├─ EdgeEngine: BookStore + VolEstimator + FairValue → edge_bps → veto 检查
  ├─ T1 跨平台价差
  ├─ T2 统计偏差 vs 阈值
  └─ T3 maker 报价刷新
        │
        ▼
StrategyOrchestrator → 优先级排序 → 资金分配 → 执行
        │
        ▼
Dashboard + 通知
```

T0 **直接**进 `RiskManager`。热路径上没有任何一层会调用 LLM。

## 模块地图

```
polymarket_arb/
├── config.py                  # ArbConfig frozen dataclass，全部来自环境变量
├── client_factory.py          # CLOB 只读 / 交易客户端工厂
├── models.py                  # ArbOpportunity、OrderBookSnapshot 等
├── utils_time.py              # 毫秒时间戳、区间对齐
├── book_store.py              # EnhancedBookStore：microprice/imbalance/depth
├── fair_value_model.py        # log-normal GBM + 多信号融合公允价
├── volatility_estimator.py    # fast 1h / slow 6h / adaptive blend σ
├── edge_engine.py             # 统一 Edge 决策 + veto 检查
├── market_scanner.py          # Gamma API 市场/事件 universe
├── orderbook_analyzer.py      # 按深度算 VWAP 成交价
├── arbitrage_detector.py      # T0 二元 + 多结果 + neg_risk
├── websocket_feed.py          # 订单簿镜像，每次变动触发 T0
├── user_feed.py               # user 频道 WS：自己的成交无需轮询
├── execution_engine.py        # 多腿原子提交 + 回滚
├── risk_manager.py            # 敞口 / 亏损 / 熔断 硬闸门
├── portfolio_sync.py          # 低频真实账户对账
├── rtds_feed.py, spot_feed.py # UPDOWN 定价用的现货源
├── typesafe_provider.py       # TypeSafe Jev 打分客户端（仅旁路）
├── ai_provider.py             # LLMProvider 抽象（仅旁路）
├── quant_input_store.py       # data/quant_inputs/*.json 热加载
├── notifier.py                # 通知路由
├── feishu_notifier.py         # 飞书应用机器人 OpenAPI 传输层
├── dashboard_api.py           # FastAPI 监控后端（仅 loopback）
├── main_loop.py               # 顶层异步编排
└── strategies/
    ├── kelly.py                    # Quarter-Kelly 仓位
    ├── optimal_stopping.py         # Bellman 退出阈值 / 分批止盈
    ├── cross_platform.py           # T1
    ├── statistical_model.py        # T2
    ├── t2_exit_manager.py          # T2 退出：止损 / 止盈 / 时间 / Bellman
    ├── logical_constraints.py      # 可选：包含 / 上界关系
    ├── event_calendar_model.py     # 可选：外部 baseline 概率
    ├── wallet_alpha.py             # 可选：已验证钱包跟随
    ├── sniper_gate.py              # 高置信 / 低相关过滤器
    ├── maker_strategy.py           # T3
    └── strategy_orchestrator.py    # 优先级调度 + 资金分配

research/backtest/    离线回测 runner（runner、执行模型、报告）
research_signal/      研究信号 collectors / normalizers / scorers
analysis/             一次性实证研究脚本（见 analysis/README.md）
scripts/              旁路 worker 与验证入口
```

## 核心设计决策

### WebSocket 推送，而不是 REST 轮询

```
REST 轮询: 平均延迟 = scan_interval / 2 ≈ 2.5s
WebSocket: 延迟 ≈ 网络 RTT ≈ 10-50ms
```

`OrderBookMirror` 在内存里维护订单簿副本，best bid/ask 一动就触发回调，直接进 T0 检测。
2.5 秒的平均延迟意味着任何结构性机会早就没了。

### Quarter-Kelly 仓位

仓位是算出来的，不是固定的：

```
f* = (p·b - q) / b          经典 Kelly
f  = 0.25 × f*              实际使用
```

Quarter-Kelly 把方差砍掉约 75%，同时保留约 75% 的期望增长。

| 策略 | win_prob | kelly_fraction | max_bet_pct |
|---|---|---|---|
| 结构性 (T0) | 0.95 | 0.25 | 10% |
| 跨平台 (T1) | 0.85 | 0.25 | 10% |
| 统计 (T2) | 0.55-0.70 | 0.25 | 5% |

### 最优停止

`strategies/optimal_stopping.py` 用有限时域 Bellman 递归求解退出边界：

```
V_τ(m) = max( m, E[ V_{τ-1}(m') ] )
```

给定剩余时间、当前 token 价格、模型终端概率和 Markov 转移矩阵，返回 HOLD/STOP 和分批止盈
阈值。这是让 T2/T3 方向性持仓不至于「只会进场」的那一环。

### 编排器的两层叠加调整

方向性信号入队之前，`StrategyOrchestrator` 会做两件事：

- **Research 共振** —— 三条及以上同向、来源分散、置信度足够的信号会获得额外权重；强烈的跨源
  冲突会 veto 该信号。计数写在
  `strategy_status.meta.research_overlay` 下的 `applied / boosted / penalized / vetoed`。
- **尾部风险折扣** —— 被归为地缘政治、停火、战争、单人决策的市场自动降低仓位和置信度，
  避免对一个"97% 确定"的合约按没有尾部的方式下 Kelly 注。

### 资金分配

$1000 本金的默认切分（可配；Polymarket 单平台部署不开 T1）：

```
T0 结构性     35%
T1 跨平台     10%
T2 统计       35%
T3 做市       20%
```

## 风控机制

| 规则 | 参数 | 效果 |
|---|---|---|
| 最大持仓数 | `RISK_MAX_OPEN_POSITIONS` | 超出后不再开新仓 |
| 单市场敞口 | `RISK_MAX_EXPOSURE_PER_MARKET` | 单个 `condition_id` 的 USDC 上限 |
| 全局敞口 | `RISK_MAX_TOTAL_EXPOSURE` | 总成本基础上限 |
| 日亏损止损 | `RISK_MAX_DAILY_LOSS` | 当日停止交易 |
| 连续失败熔断 | `RISK_MAX_CONSECUTIVE_FAILURES` | N 次执行失败后熔断 |
| 市场冷却 | 60s | 同一事件不重复执行 |
| 腿回滚 | 自动 | 任一腿失败即尝试撤销已提交的腿 |

## 跨文件设计契约

下面这些跨多个文件，从任何单个文件里都看不出来。每一条都对应一个真实发生过的 bug。

### 订单类型由方向决定，不由方便决定

`ExecutionEngine._resolve_execution_order_type` 按 `FOK > FAK > GTC` 的回退链解析，除非调用方
显式传入类型，否则入场用这个结果。

- **T0 多腿入场必须 FOK。** 部分成交会破坏结构性不变量 —— 五个结果里买到三个就成了裸头寸。
- **T2 单腿入场**用 FOK 没问题；不成交只是错过一次机会。
- **T2 退出必须显式传 FAK**（`t2_exit_manager._issue_exit`）。用 FOK 的话，bid 侧任何一档薄
  就会把整笔退出打回，仓位留着不动，部分退出永远达不到。
- **T3 maker / GTC** 由 `_gtc_order_type` 单独解析，在 maker 路径上通过 `order_type=` 传入。

### 持仓方向来自成交，不来自信号

`t2_exit_manager._resolve_trade_outcome_label` 先查
`market.tokens[token_id].outcome`；signal payload 里的 `action` 只是兜底。历史上有个 bug 跳过了
这一步并默认 `BUY_YES`，结果把每个 BUY NO 持仓都静默记成 YES 持仓 —— 于是每次退出都在试图卖
账户从未持有的 YES token，全部 FOK 失败。永远不要只凭信号短路这段逻辑。

### 风控状态同步窗口

- `RiskManager.record_execution` 在 BUY 成交时增加敞口，在 SELL 成交时释放已记账敞口。
- T2 退出在 `T2ExitManager` 内更新 `pos.size_remaining`，并在确认的 SELL 成交上调用
  `RiskManager.release_market_exposure`，所以 `RISK_MAX_OPEN_POSITIONS` 和
  `RISK_MAX_TOTAL_EXPOSURE` 会立即释放，而不是等下一次账户同步。
- `_maybe_reset_daily` 在 UTC 午夜把 `daily_pnl` 归零，并立刻重算
  `total_pnl = unrealized_pnl`。

### 单市场信号频率上限

`StrategyOrchestrator._check_per_market_rate_cap` 把 T2（以及其他受限层）限制在
`T2_MAX_SIGNALS_PER_MARKET_PER_HOUR`。上游 T2 collector 并不知道这个上限，在盘口稳定时每个扫描
周期都会重发同一条信号，所以 `StrategySignalTelemetryCompressor`
（`main_helpers/signal_telemetry.py`）会对 NDJSON 写入去重，编排器也会节流对应的日志行。
否则 telemetry 文件和 `arb_bot.log` 几个小时内就没法看了。

## Telemetry

| 文件 | 内容 |
|---|---|
| `data/ticks/YYYY-MM-DD.ndjson` | 原始订单簿更新，200MB 滚动（`TICK_RECORD_ENABLED`） |
| `data/telemetry/*.cycle_metrics.ndjson` | 每个扫描周期一行：`book_stats`（ws_hit / cache_hit / rest_fallback）、`timing_stats`、敞口、持仓数 |
| `data/telemetry/*.strategy_signals.ndjson` | T1/T2/T3 与量化层信号，即使 T0 什么也没找到也会写 |
| `data/telemetry/*.strategy_executions.ndjson` | 入场、退出，以及编排器跳过聚合（`skip_reasons`） |
| `data/telemetry/*.risk_events.ndjson` | 风控触发，另含 `cycle_summary` 与 `portfolio_sync` 行 |
| `data/telemetry/notification_state.json` | 日计数与上次发送时间，跨重启保留 |

两个常被混淆的指标：cycle 行上的 `arbs_found_total` 是编排器的**方向性**信号数
（T0 + T1 + T2 + 逻辑/事件/钱包层，不含 T3 maker 报价），同一行上的
`t0_opportunities_total` 才是纯 T0 结构性套利。看到 T0 "0 arbs" 不等于其他层没信号 ——
要同时看 `strategy_signals`。

每条日志和每行 telemetry 都带 `run_id=run-<pid>-<UTC start>`。改代码在进程重启前不生效；
如果你改了模块而 `run_id` 没变，那么无论磁盘上是什么，跑着的仍然是旧代码。
