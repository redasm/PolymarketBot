# Polymarket 套利机器人

多策略 Polymarket 预测市场套利系统——从结构性套利到概率模型驱动的统计套利，分层最大化利润。

## 策略体系

本系统实现 **4 层策略**，按优先级从高到低执行：

```
                    ┌──────────────┐
                    │ T0 结构性套利 │  确定利润 | 胜率≈95% | 年化 ~2-5%
                    └──────┬───────┘
                    ┌──────┴───────┐
                    │ T1 跨平台套利 │  Poly vs Kalshi | 胜率≈85% | 年化 ~10-20%
                    └──────┬───────┘
              ┌────────────┴────────────┐
              │ T2 统计/概率模型套利     │  贝叶斯定价 | 胜率≈55-70% | 年化 ~20-50%
              └────────────┬────────────┘
         ┌─────────────────┴─────────────────┐
         │ T3 做市 + 流动性奖励               │  0% 费率 | 连续收入 | 年化 ~15-30%
         └───────────────────────────────────┘
```

### T0 — 结构性套利

**二元市场**: Polymarket 的 Yes/No token 最终必有一个结算为 $1.00。当 `ask(Yes) + ask(No) < $1.00 - fee` 时，买入双方锁定无风险利润。

```
利润 = $1.00 - ask_yes - ask_no - taker_fee(2%)
```

**多结果事件**: 一个事件（如选举）有 N 个互斥市场。当 `Σ(ask_i) < $1.00 - fee` 时，买入所有结果锁定利润。neg_risk 市场自动选择 `min(ask_yes, 1 - bid_no)` 最优路径。

**检测方式**: WebSocket 事件驱动（毫秒级）+ VWAP 深度验证防滑点。

### T1 — 跨平台套利

同一事件在 Polymarket 和 Kalshi 的隐含概率经常有显著偏差（5%+），原因是用户群体不同、跨平台资金转移有摩擦。

```
poly_yes_ask = 0.55   # Polymarket 买 Yes
kalshi_no_ask = 0.40   # Kalshi 买 No
total = 0.95           # < 1.0 → 5% 毛利
```

风险: 结算规则差异、资金锁定时间、平台对手方风险。

### T2 — 统计套利（概率模型驱动）

不等结构性机会出现，而是用**多信号融合模型**主动发现 mispricing:

```
FairValueModel (log-normal GBM)  ──┐
                                    ├→ compute_general_fair_value() → model_prob
订单簿 microprice + imbalance    ──┤
价格动量 + 跨市场约束            ──┘
    model_prob = 0.72   vs  market_price = 0.60
    → Edge = +1200 bps → 买入 Yes → Kelly 决定仓位
```

**核心升级**（集成自 [mlmodelpoly](https://github.com/txbabaxyz/mlmodelpoly)）:
- **FairValueModel**: 对有现货锚定的 UPDOWN 市场（如 "BTC 15min 高于 X?"），用 GBM 精确定价
- **VolEstimator**: 多尺度波动率（fast 1h / slow 6h / adaptive blend），替代简单滚动窗口
- **EnhancedBookStore**: 提供 microprice（成交量加权中间价）、多层 imbalance、spread_bps 等衍生指标
- **EdgeEngine**: 统一决策接口，融合所有信号源并做 veto check（spread/depth/confidence）

信号源:
- **订单簿不平衡 (OBI)**: 5 档 bid/ask depth 偏离 → 预测短期方向
- **Microprice**: 比 mid 更精确的短期方向指标
- **价格动量**: 短期趋势延续信号
- **跨市场逻辑约束**: P(Trump wins) > P(Republican wins) 是逻辑矛盾
- **现货锚定**: UPDOWN 市场中 BTC 现货价 vs 参考价的 z-score

### T3 — 做市策略

在模型 fair value 两侧挂 **maker 限价单**:
- **Maker 费率 = 0%**（对比 Taker 2%），每笔交易的 edge 直接提升 2 个百分点
- 在激励带 `[mid - δ, mid + δ]` 内挂单可获得 Polymarket **流动性奖励积分**
- 动态 spread 由 **VolEstimator** 驱动: `base + sigma_blend×2 + inventory_skew`
- 波动率飙升时 spread 自动拉宽（保护逆向选择），平稳期自动收窄（提高成交率）

## 架构

```
polymarket_arb/
├── __init__.py
├── config.py                          # 环境变量配置（ArbConfig frozen dataclass）
├── client_factory.py                  # CLOB 只读/交易客户端工厂
├── models.py                          # 数据模型（ArbOpportunity, OrderBookSnapshot 等）
├── utils_time.py                      # 时间工具（毫秒时间戳、区间对齐）
├── book_store.py                      # 增强订单簿存储（microprice/imbalance/depth 衍生指标）
├── fair_value_model.py                # Fair Value 定价（log-normal GBM + 通用多信号融合）
├── volatility_estimator.py            # 多尺度波动率（fast 1h / slow 6h / adaptive blend）
├── edge_engine.py                     # Edge 决策引擎（融合 BookStore + Vol + FairValue）
├── market_scanner.py                  # Gamma API 批量拉取活跃市场/事件
├── orderbook_analyzer.py              # 订单簿分析、VWAP 加权成交价计算
├── arbitrage_detector.py              # T0 结构性套利检测（二元 + 多结果 + neg_risk）
├── websocket_feed.py                  # WebSocket 实时订单簿镜像 + 同步 EnhancedBookStore
├── execution_engine.py                # 交易执行（多腿原子提交 + 失败回滚）
├── risk_manager.py                    # 风控（敞口/止损/熔断/市场冷却）
├── telegram_notifier.py               # Telegram 推送（套利发现/执行/错误）
├── logger_setup.py                    # 日志（控制台 + 文件双输出）
├── dashboard_api.py                   # FastAPI 监控后端 + 波动率/Edge/BookStore 端点
├── dashboard.html                     # 前端仪表盘
├── main_loop.py                       # 主循环入口
└── strategies/
    ├── __init__.py
    ├── kelly.py                       # Kelly Criterion 最优仓位（二元/结构性/统计）
    ├── cross_platform.py              # T1 跨平台套利（Polymarket vs Kalshi）
    ├── statistical_model.py           # T2 贝叶斯定价 + FairValueModel 现货锚定
    ├── maker_strategy.py              # T3 做市策略（VolEstimator 驱动 spread）
    └── strategy_orchestrator.py       # 策略编排器（优先级调度 + 资金分配）

research/
└── backtest/                          # 回测/离线研究骨架（runner、execution model、reports）

research_signal/
└── ...                                # 研究信号聚合骨架（collectors/normalizers/scorers/service）
```

## 核心设计决策

### WebSocket 事件驱动 vs REST 轮询

```
REST 轮询:  平均延迟 = scan_interval/2 ≈ 2.5s → 结构性机会已被吃掉
WebSocket:  延迟 ≈ 网络 RTT ≈ 10-50ms → 50-250x 提升
```

`OrderBookMirror` 在内存中维护订单簿副本，每当 best bid/ask 变动时触发回调，立即进入检测→执行流程。

### Kelly Criterion 仓位优化

不是每次固定下 $10，而是根据 edge 和胜率数学计算最优下注比例:

```
f* = (p·b - q) / b    (经典 Kelly)
实际使用 Quarter-Kelly: f = 0.25 × f*
→ 方差降低 75%，期望收益只降 25%
```

不同策略的 Kelly 参数:
| 策略 | win_prob | kelly_fraction | max_bet_pct |
|------|----------|---------------|-------------|
| 结构性套利 | 0.95 | 0.25 | 10% |
| 跨平台套利 | 0.85 | 0.25 | 10% |
| 统计套利 | 0.55-0.70 | 0.25 | 5% |

### 策略编排与资金分配

`StrategyOrchestrator` 统一调度，高优先级策略先执行:

```
总资金 $1000 默认分配:
├── T0 结构性套利: $300 (30%) — 确定利润，留足弹药
├── T1 跨平台套利: $200 (20%) — 高利润但需跨平台资金
├── T2 统计套利:   $300 (30%) — 主要利润来源
└── T3 做市策略:   $200 (20%) — 持续被动收入
```

## 执行流程

```
WebSocket 订单簿变动推送
         │
         ▼
OrderBookMirror 更新 ──→ EnhancedBookStore 同步更新
         │                    │
         ▼                    ▼
best bid/ask 变动?      microprice / imbalance / depth 计算
         │ Yes                        │ No
         ▼                            └→ 忽略
T0 结构性套利检测 (毫秒级)
  ├─ 二元: ask_yes + ask_no < 1 - 0.02?
  └─ 多结果: Σ ask_i < 1 - 0.02?
         │
         ├─ 命中 → VWAP 深度验证 → Kelly 计算仓位 → 风控预检 → 执行
         │
定时扫描 (5s 周期)
  ├─ EdgeEngine: BookStore + VolEstimator + FairValue → edge_bps → veto check
  ├─ T1 跨平台: Poly vs Kalshi 价差
  ├─ T2 统计模型: 贝叶斯偏差 > threshold?
  └─ T3 做市: VolEstimator 驱动 spread → 更新 bid/ask 报价
         │
         ▼
StrategyOrchestrator 优先级排序 → 资金分配 → 逐个执行
         │
         ▼
Dashboard 更新（volatility / edge / book_summary）+ Telegram 通知
```

## 风控机制

| 规则 | 参数 | 说明 |
|------|------|------|
| 最大持仓数 | `RISK_MAX_OPEN_POSITIONS` | 超出后不开新仓 |
| 单市场敞口 | `RISK_MAX_EXPOSURE_PER_MARKET` | 单个 condition_id 的最大 USDC 暴露 |
| 全局敞口 | `RISK_MAX_TOTAL_EXPOSURE` | 全局持仓总成本上限 |
| 日亏损止损 | `RISK_MAX_DAILY_LOSS` | 当日累计亏损触发后暂停全部交易 |
| 连续失败熔断 | `RISK_MAX_CONSECUTIVE_FAILURES` | 连续 N 次执行失败后触发熔断 |
| 市场冷却 | 60s | 同一事件 60 秒内不重复执行 |
| 腿失败回滚 | 自动 | 多腿套利中任一腿失败，尝试撤销已提交的腿 |

## 安装

```bash
cd PolymarketBot

cd /d E:\AppProject\PolymarketBot

python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
```

## 配置

```bash
cp .env.example .env
# 编辑 .env 填写必要参数
```

关键配置项:

| 变量 | 说明 | 默认 |
|------|------|------|
| `PRIVATE_KEY` | 钱包私钥 | (必填) |
| `POLYMARKET_FUNDER` | 代理钱包地址 | (必填) |
| `ARB_DRY_RUN` | true=只扫描不交易 | true |
| `ARB_MIN_EDGE_USD` | 最小净利润门槛 | 0.005 |
| `ARB_MIN_EDGE_PCT` | 最小利润率门槛 | 0.3% |
| `ARB_SCAN_INTERVAL_SEC` | 定时扫描间隔 | 5s |
| `ARB_MAX_ORDER_SIZE_USDC` | 单笔最大下单量 | 50 USDC |
| `ORDERBOOK_SNAPSHOT_TTL_SEC` | REST 订单簿快照缓存 TTL | 0.5s |
| `POLYMARKET_TAKER_FEE_RATE` | Polymarket taker 费率假设 | 2.0% |
| `KALSHI_TAKER_FEE_RATE` | Kalshi taker 费率假设 | 0.3% |
| `RISK_MAX_TOTAL_EXPOSURE` | 全局最大敞口 | 500 USDC |
| `RISK_MAX_DAILY_LOSS` | 日亏损止损线 | 50 USDC |
| `VOL_FAST_MINUTES` | 快速波动率窗口 | 60 分钟 |
| `VOL_SLOW_MINUTES` | 慢速波动率窗口 | 360 分钟 |
| `EDGE_MIN_BPS` | Edge 引擎最小触发阈值 | 100 bps |
| `EDGE_MAX_SPREAD_BPS` | 最大可接受 spread | 500 bps |
| `AI_AUTO_RECOVER_SEC` | AI 降级后自动恢复等待时间 | 1800s |
| `RESEARCH_SIGNAL_ENABLED` | 启用研究信号摘要 | false |
| `BACKTEST_ENABLED` | 启用回测状态展示 | false |

完整配置见 `.env.example`。

几个新增参数建议保持保守默认值：

- `ORDERBOOK_SNAPSHOT_TTL_SEC`
  建议保持在 `0.2-0.8s`。太小会重新回到高频 REST 压力，太大会让扫描看到的盘口变旧。
- `POLYMARKET_TAKER_FEE_RATE`
  当前实现按更保守的“成本侧计费”估算，目的是避免高估套利利润。除非你已经核实最新官方费率模型，否则不建议往下调。
- `KALSHI_TAKER_FEE_RATE`
  默认 `0.3%` 是偏保守的中位数假设。做跨平台利润回测时建议把它和真实账户成交单据对齐。
- `AI_AUTO_RECOVER_SEC`
  默认 30 分钟，避免 AI 因短期连续亏损被永久锁死；如果你希望 AI 更谨慎，可以调到 `3600-7200`。

## 运行

```bash
# Dry Run 模式（默认，只扫描不交易）
python run_arb_bot.py

# 独立运行 research signal 调试，不要求钱包参数
python run_research.py --limit 20 --show-markets
python run_research.py --query btc --json

# 或作为模块运行
python -m polymarket_arb.main_loop

# 运行最小回测 runner
python -m research.backtest.run --dataset default

# 刷新研究信号摘要
python -m research_signal.refresh --limit 10
```

**强烈建议**先用 `ARB_DRY_RUN=true` 观察一段时间，确认策略逻辑和信号质量符合预期后再切换为实盘。

`run_research.py` 适合单独验证研究层：

- 拉取活跃市场并按 `--query` 过滤
- 输出 research signal 聚合报告、来源分布、cache hit 状态
- 用 `--json` 导出结构化结果，方便后续接 AI context 或离线分析

Research layer 也支持两种可选扩展源：

- 额外 RSS feeds：设置 `RESEARCH_SIGNAL_EXTRA_RSS_FEEDS`，格式如 `custom=https://example.com/rss?q={query}`
- 本地知识库：设置 `RESEARCH_SIGNAL_KNOWLEDGE_ENABLED=true`，并把 `*.jsonl` 放到 `RESEARCH_SIGNAL_KNOWLEDGE_DIR`

本地知识库 JSONL 每行可包含这些字段：

```json
{
  "topic": "BTC ETF approval odds",
  "summary": "ETF approval usually boosts BTC sentiment",
  "tags": ["btc", "etf", "approval"],
  "event_id": "1234",
  "condition_id": "0xabc",
  "source": "local_knowledge_base",
  "link": "https://example.com/note",
  "published_ts": 1710000000
}
```

仓库里也放了一个最小示例文件：

- [example_signals.jsonl](e:/AppProject/PolymarketBot/data/research_signal/knowledge/example_signals.jsonl)

把 `RESEARCH_SIGNAL_KNOWLEDGE_ENABLED=true` 打开后，`run_research.py` 和主循环都会自动读取它。

## Dry-Run 验证清单

建议第一次接通 research 扩展源时，按下面顺序验证：

1. 准备 `.env`
   设置 `ARB_DRY_RUN=true`
   设置 `RESEARCH_SIGNAL_ENABLED=true`
   设置 `RESEARCH_SIGNAL_KNOWLEDGE_ENABLED=true`
   设置 `RESEARCH_SIGNAL_KNOWLEDGE_DIR=data/research_signal/knowledge`

2. 单独验证 research 层

```bash
python run_research.py --query btc --show-markets
python run_research.py --query btc --json
```

预期检查点：

- 输出里能看到 `source_counts`
- `signals` 里出现 `local_knowledge_base`
- `cache_hit` 在第二次运行时变为 `true`

3. 启动 dry-run 主循环

```bash
python run_arb_bot.py
```

预期检查点：

- Dashboard 的 `Research Signals` 卡片里能看到信号数量和摘要
- `strategy_status.meta.research_overlay` 会开始累计 `applied / boosted / penalized / vetoed`
- `AI decisions` 里会附带 `research_overlay`

4. 如需验证离线回放

```bash
python -m research.backtest.run --dataset default
```

预期检查点：

- 不要求钱包私钥也能运行
- 生成 report、trade log 和 recommended params

5. 最后再考虑切到 live

- 先确认 `research` 没有系统性反向误导
- 先确认 `strategy overlay` 更多是在降噪，而不是频繁 veto 全部信号
- 先确认 AI 成本、风控和 dashboard 状态都稳定

## 测试

```bash
# 运行全部单元测试
pytest

# 运行单个模块
pytest tests/test_fair_value_model.py -v

# 只跑某个类
pytest tests/test_arbitrage_detector.py::TestBinaryArbDetection -v
```

测试覆盖的模块：

| 测试文件 | 被测模块 | 验证内容 |
|----------|----------|----------|
| `test_fair_value_model.py` | `fair_value_model` | GBM 定价 z-score 与手算一致、边界处理、多信号融合 |
| `test_volatility_estimator.py` | `volatility_estimator` | warmup 行为、恒定价 σ≈0、GBM 模拟 σ 合理、√15 缩放 |
| `test_arbitrage_detector.py` | `arbitrage_detector` | 二元/多结果套利检出、已关闭市场过滤、微利过滤 |
| `test_edge_engine.py` | `edge_engine` | 方向判定、spread/depth/disconnect veto、波动率对置信度影响 |

## 数据录制与回测

### 录制

设置 `TICK_RECORD_ENABLED=true`，WS 推送的每次订单簿更新会被写入 NDJSON 文件：

```
data/ticks/
├── 2026-04-11.ndjson      # 按 UTC 日期滚动
├── 2026-04-12.ndjson
└── 2026-04-12.1.ndjson    # 单文件超过 200MB 时自动滚动
```

每行格式：

```json
{
  "ts_ms": 1712345678000,
  "token_id": "0xabc...def",
  "event_type": "book",
  "best_bid": 0.52,
  "best_ask": 0.54,
  "bid_depth_5": 12500.0,
  "ask_depth_5": 8700.0,
  "imbalance_5": 0.18,
  "microprice": 0.5285,
  "spread_bps": 377.4,
  "bids_top3": [[0.52, 5000], [0.51, 4500], [0.50, 3000]],
  "asks_top3": [[0.54, 3200], [0.55, 2800], [0.56, 2700]]
}
```

### 回测

录制数据后，可以编写回放脚本按时间顺序重放：

```python
import json
from polymarket_arb.book_store import EnhancedBookStore
from polymarket_arb.edge_engine import EdgeEngine

store = EnhancedBookStore()
engine = EdgeEngine(min_edge_bps=100)

with open("data/ticks/2026-04-11.ndjson") as f:
    for line in f:
        tick = json.loads(line)
        bids = [(p, s) for p, s in tick["bids_top3"]]
        asks = [(p, s) for p, s in tick["asks_top3"]]
        store.update_by_token_id(tick["token_id"], bids, asks, tick["ts_ms"])
        decision = engine.evaluate(store)
        if decision.direction != "NONE":
            print(f"[{tick['ts_ms']}] {decision.direction} edge={decision.edge_bps:.0f}bps")
```

### 使用 tmux（远程服务器）

```bash
cd ~/PolymarketBot
source .venv/bin/activate
tmux new -s arb
python run_arb_bot.py
```

后台保活与回连：

```bash
# 在 tmux 中启动后，按 Ctrl+b 再按 d，可断开但保持机器人继续运行

# 查看当前会话
tmux ls

# 重新接回运行中的机器人
tmux attach -t arb

# 如需停止并重建会话
tmux kill-session -t arb
tmux new -s arb
```

### 远程访问 Dashboard（SSH 端口转发）

机器人默认只在服务器本机监听 dashboard：

```bash
http://127.0.0.1:8077
```

推荐在**本地电脑**执行 SSH 端口转发，而不是把 dashboard 暴露到公网：

```bash
ssh -N -L 18077:127.0.0.1:8077 -i /path/to/your_key.pem root@your_server_ip
```

Windows PowerShell 示例：

```powershell
ssh -N -L 18077:127.0.0.1:8077 -i C:\path\to\your_key.pem root@your_server_ip
```

然后在本地浏览器打开：

```bash
http://127.0.0.1:18077
```

如果出现：

```bash
channel ... open failed: connect failed: Connection refused
```

通常表示 SSH 登录已经成功，但**服务器上的 dashboard 没有监听 8077**。请先在服务器里确认：

```bash
grep DASHBOARD_ENABLED .env
grep DASHBOARD_PORT .env
ss -lntp | grep 8077
tail -n 50 arb_bot.log
```

推荐的运维方式：

1. 用 `tmux` 在服务器后台运行 `python run_arb_bot.py`
2. 用另一条本地 SSH 隧道访问 dashboard
3. 不要直接把 dashboard 监听改成 `0.0.0.0`

## 免责声明

- 本项目仅供学习研究，不构成投资建议
- 结构性套利机会在实际市场中极其稀缺，Polymarket 有专业做市商（如 Wintermute）在毫秒级修正偏差
- 统计套利依赖模型质量，模型错误会导致亏损
- 做市策略面临逆向选择和库存风险
- 请在充分理解所有风险后自行决定是否使用，作者不对任何损失负责
- 请遵守 Polymarket 服务条款和所在地法律法规（含地理限制）
