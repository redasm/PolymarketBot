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

不等结构性机会出现，而是用贝叶斯模型**主动发现 mispricing**:

```
model_prob = 0.72     # 模型估计真实概率
market_price = 0.60   # 市场隐含概率
→ 偏差 +12% → 买入 Yes → Kelly 公式决定仓位
```

信号源:
- **订单簿不平衡 (OBI)**: bid 深度 / ask 深度的偏离 → 预测短期方向
- **价格动量**: 短期趋势延续信号
- **跨市场逻辑约束**: P(Trump wins) > P(Republican wins) 是逻辑矛盾

### T3 — 做市策略

在模型 fair value 两侧挂 **maker 限价单**:
- **Maker 费率 = 0%**（对比 Taker 2%），每笔交易的 edge 直接提升 2 个百分点
- 在激励带 `[mid - δ, mid + δ]` 内挂单可获得 Polymarket **流动性奖励积分**
- 动态 spread = `base + volatility_adj + inventory_skew`，根据波动率和持仓偏斜实时调整

## 架构

```
polymarket_arb/
├── __init__.py
├── config.py                          # 环境变量配置（ArbConfig frozen dataclass）
├── client_factory.py                  # CLOB 只读/交易客户端工厂
├── models.py                          # 数据模型（ArbOpportunity, OrderBookSnapshot 等）
├── market_scanner.py                  # Gamma API 批量拉取活跃市场/事件
├── orderbook_analyzer.py              # 订单簿分析、VWAP 加权成交价计算
├── arbitrage_detector.py              # T0 结构性套利检测（二元 + 多结果 + neg_risk）
├── websocket_feed.py                  # WebSocket 实时订单簿镜像 + 事件驱动触发
├── execution_engine.py                # 交易执行（多腿原子提交 + 失败回滚）
├── risk_manager.py                    # 风控（敞口/止损/熔断/市场冷却）
├── telegram_notifier.py               # Telegram 推送（套利发现/执行/错误）
├── logger_setup.py                    # 日志（控制台 + 文件双输出）
├── main_loop.py                       # 主循环入口
└── strategies/
    ├── __init__.py
    ├── kelly.py                       # Kelly Criterion 最优仓位（二元/结构性/统计）
    ├── cross_platform.py              # T1 跨平台套利（Polymarket vs Kalshi）
    ├── statistical_model.py           # T2 贝叶斯定价模型 + OBI/动量/跨市场信号
    ├── maker_strategy.py              # T3 做市策略（动态 spread + 库存倾斜）
    └── strategy_orchestrator.py       # 策略编排器（优先级调度 + 资金分配）
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
OrderBookMirror 更新 → best bid/ask 变动?
         │ Yes                        │ No
         ▼                            └→ 忽略
T0 结构性套利检测 (毫秒级)
  ├─ 二元: ask_yes + ask_no < 1 - 0.02?
  └─ 多结果: Σ ask_i < 1 - 0.02?
         │
         ├─ 命中 → VWAP 深度验证 → Kelly 计算仓位 → 风控预检 → 执行
         │
定时扫描 (5s 周期)
  ├─ T1 跨平台: Poly vs Kalshi 价差
  ├─ T2 统计模型: 贝叶斯偏差 > threshold?
  └─ T3 做市: 更新 bid/ask 报价
         │
         ▼
StrategyOrchestrator 优先级排序 → 资金分配 → 逐个执行
         │
         ▼
Telegram 通知 + 风控状态更新
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
| `RISK_MAX_TOTAL_EXPOSURE` | 全局最大敞口 | 500 USDC |
| `RISK_MAX_DAILY_LOSS` | 日亏损止损线 | 50 USDC |

完整配置见 `.env.example`。

## 运行

```bash
# Dry Run 模式（默认，只扫描不交易）
python run_arb_bot.py

# 或作为模块运行
python -m polymarket_arb.main_loop
```

**强烈建议**先用 `ARB_DRY_RUN=true` 观察一段时间，确认策略逻辑和信号质量符合预期后再切换为实盘。

### 使用 tmux（远程服务器）

```bash
tmux new -s arb
source .venv/bin/activate
python run_arb_bot.py
# Ctrl+b d 断开
# tmux attach -t arb 接回
```

## 免责声明

- 本项目仅供学习研究，不构成投资建议
- 结构性套利机会在实际市场中极其稀缺，Polymarket 有专业做市商（如 Wintermute）在毫秒级修正偏差
- 统计套利依赖模型质量，模型错误会导致亏损
- 做市策略面临逆向选择和库存风险
- 请在充分理解所有风险后自行决定是否使用，作者不对任何损失负责
- 请遵守 Polymarket 服务条款和所在地法律法规（含地理限制）
