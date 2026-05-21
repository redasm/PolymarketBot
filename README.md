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
fee_per_leg = feeRate * price * (1 - price)
利润 = $1.00 - ask_yes - ask_no - Σ(fee_per_leg)
```

**多结果事件**: 一个事件（如选举）有 N 个互斥市场。当 `Σ(ask_i) < $1.00 - Σ(fee_i)` 时，买入所有结果锁定利润。neg_risk 市场自动选择 `min(ask_yes, 1 - bid_no)` 最优路径。

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
├── feishu_notifier.py                 # 飞书应用机器人推送
├── notifier.py                        # 统一通知路由（成交/错误/盈亏/日报）
├── logger_setup.py                    # 日志（控制台 + 文件双输出）
├── dashboard_api.py                   # FastAPI 监控后端 + 波动率/Edge/BookStore 端点
├── dashboard.html                     # 前端仪表盘
├── main_loop.py                       # 主循环入口
└── strategies/
    ├── __init__.py
    ├── kelly.py                       # Kelly Criterion 最优仓位（二元/结构性/统计）
    ├── optimal_stopping.py            # Bellman/MDP 最优退出与分批止盈阈值
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

### 最优退出与尾部风险折扣

`optimal_stopping.py` 用有限时域 Bellman 递归求解持仓退出边界:

```
V_tau(m) = max(m, E[V_tau-1(m')])
```

输入剩余时间、当前 token 价格、模型终端概率和 Markov 转移矩阵后，输出 HOLD/STOP 和分批止盈阈值。它适合接入 T2/T3 方向性持仓，避免只会进场、不会量化退出。

`StrategyOrchestrator` 还会在方向性信号入队前做两层调整:
- **Research 共振**: 3 条以上同向、来源分散且置信度足够的研究信号会额外加权；强冲突共振会 veto。
- **尾部风险折扣**: 地缘政治、停火、战争、单人决策等高尾部风险市场会自动降低推荐仓位和置信度，避免高概率合约的黑天鹅尾部把 Kelly 放得过大。

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
Dashboard 更新（volatility / edge / book_summary）+ 飞书通知
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
| `POLYMARKET_FUNDER` | 交易资金钱包地址；老用户填 proxy/Safe，新 deposit wallet 用户填 deposit wallet | (必填) |
| `POLYMARKET_DEPOSIT_WALLET` | deposit wallet 地址别名；留空时使用 `POLYMARKET_FUNDER` | 空 |
| `POLYMARKET_SIGNATURE_TYPE` | `0` EOA / `1` Proxy / `2` Safe / `3` Deposit Wallet (`POLY_1271`) | `2` |
| `ARB_DRY_RUN` | true=只扫描不交易 | true |
| `ARB_MIN_EDGE_USD` | 最小净利润门槛 | 0.005 |
| `ARB_MIN_EDGE_PCT` | 最小利润率门槛 | 0.3% |
| `ARB_SCAN_INTERVAL_SEC` | 定时扫描间隔 | 5s |
| `ARB_MAX_ORDER_SIZE_USDC` | 单笔最大下单量 | 50 USDC |
| `ORDERBOOK_SNAPSHOT_TTL_SEC` | REST 订单簿快照缓存 TTL | 0.5s |
| `POLYMARKET_TAKER_FEE_RATE` | Polymarket taker 费率假设 | 2.0% |
| `LIVE_MIN_NET_EDGE_BPS` | 实盘扣费后最低净 edge 安全垫 | 25 |
| `LIVE_MIN_NET_EDGE_USD` | 实盘扣费后最低每股净 edge | 0.0025 |
| `LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC` | 实盘 WS 盘口最大年龄（按信号 token 单独判定） | 5s |
| `LIVE_MIN_WS_HIT_RATIO` | 实盘近期 WS 盘口命中率下限 | 0.25 |
| `KALSHI_TAKER_FEE_RATE` | Kalshi taker 费率假设 | 0.3% |
| `RISK_MAX_TOTAL_EXPOSURE` | 全局最大敞口 | 500 USDC |
| `RISK_MAX_DAILY_LOSS` | 日亏损止损线 | 50 USDC |
| `VOL_FAST_MINUTES` | 快速波动率窗口 | 60 分钟 |
| `VOL_SLOW_MINUTES` | 慢速波动率窗口 | 360 分钟 |
| `EDGE_MIN_BPS` | Edge 引擎最小触发阈值 | 100 bps |
| `EDGE_MAX_SPREAD_BPS` | 最大可接受 spread | 500 bps |
| `T2_MIN_DEVIATION` | T2 最小绝对概率偏差 | 0.02 |
| `RESEARCH_SIGNAL_ENABLED` | 启用研究信号摘要 | false |
| `BACKTEST_ENABLED` | 启用回测状态展示 | false |
| `SNIPER_GATE_ENABLED` | 启用方向性信号高置信门禁 | false |
| `LOGICAL_CONSTRAINTS_JSON` | 逻辑约束策略规则 JSON | 空 |
| `EVENT_BASELINES_JSON` | 事件时间节点 baseline JSON | 空 |
| `WALLET_ALPHA_PROFILES_JSON` | 已离线验证的钱包画像 JSON | 空 |
| `WALLET_ALPHA_OBSERVATIONS_JSON` | 钱包最新观察 JSON | 空 |
| `LOGICAL_CONSTRAINTS_FILE` | 逻辑约束 JSON 文件，主循环热加载 | 空 |
| `EVENT_BASELINES_FILE` | 事件 baseline JSON 文件，主循环热加载 | 空 |
| `WALLET_ALPHA_PROFILES_FILE` | 钱包画像 JSON 文件，主循环热加载 | 空 |
| `WALLET_ALPHA_OBSERVATIONS_FILE` | 钱包观察 JSON 文件，主循环热加载 | 空 |
| `WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED` | dry-run 下允许未验证钱包生成候选影子信号 | false |
| `WALLET_ALPHA_SHADOW_VALIDATION_ENABLED` | live 单实例内启用候选钱包 shadow-only 验证通道 | true |
| `WALLET_ALPHA_SHADOW_MAX_SIGNALS_PER_CYCLE` | live 内部 shadow 验证每周期最多处理候选数 | 5 |
| `WALLET_ALPHA_SHADOW_MAX_EXEC_MS_PER_CYCLE` | live 内部 shadow 验证每周期最多占用毫秒数 | 250 |

完整配置见 `.env.example`。

如果你希望把成交、严重错误、盈亏阈值和日报推送到飞书应用机器人，优先关注这些新增配置：

- `FEISHU_APP_ID=...`
- `FEISHU_APP_SECRET=...`
- `FEISHU_OPEN_ID=...`
- `NOTIFICATION_COOLDOWN_SEC=30`
- `NOTIFY_ON_ARB_FOUND=false`
- `NOTIFY_ON_TRADE_SUCCESS=true`
- `NOTIFY_ON_TRADE_FAILURE=true`
- `NOTIFY_ON_FATAL_ERROR=true`
- `NOTIFY_ON_PNL_ALERT=true`
- `NOTIFY_ON_DAILY_SUMMARY=true`
- `PNL_PROFIT_ALERT_USDC=20`
- `PNL_LOSS_ALERT_USDC=10`
- `DAILY_SUMMARY_TIME_HHMM=08:05`
- `DAILY_SUMMARY_TIMEZONE=Asia/Shanghai`

当前日报默认 `08:05 Asia/Shanghai` 发送，配合现有 `daily_pnl` / 风控日切口径做去重与归档。
飞书渠道当前会通过飞书应用机器人 OpenAPI 发送结构化 `post` 消息，包括启动、停止、成交、严重错误、盈亏提醒和日报，便于把它作为主要值守入口。

如果你希望让 dashboard / 风控 / 盈亏提醒尽量接近账户真实状态，可以打开低频账户同步：

- `PORTFOLIO_SYNC_ENABLED=true`
- `PORTFOLIO_SYNC_INTERVAL_SEC=60`
- `PORTFOLIO_SYNC_TIMEOUT_SEC=5`
- `DATA_API_HOST=https://data-api.polymarket.com`
- `PORTFOLIO_SYNC_USER_ADDRESS=`：留空时默认使用 `POLYMARKET_FUNDER`

第一版账户同步只做两件事：

- 同步当前真实持仓到 dashboard / 风控状态
- 同步当日已实现盈亏到 `daily_pnl`

它不会进入高频盘口扫描或执行路径，只按低频周期刷新。

几个新增参数建议保持保守默认值：

- `ORDERBOOK_SNAPSHOT_TTL_SEC`
  建议保持在 `0.2-0.8s`。太小会重新回到高频 REST 压力，太大会让扫描看到的盘口变旧。
- `POLYMARKET_TAKER_FEE_RATE`
  当前实现按更保守的“成本侧计费”估算，目的是避免高估套利利润。除非你已经核实最新官方费率模型，否则不建议往下调。
- `KALSHI_TAKER_FEE_RATE`
  默认 `0.3%` 是偏保守的中位数假设。做跨平台利润回测时建议把它和真实账户成交单据对齐。
- `CROSS_PLATFORM_PAIRS_JSON`
  T1 需要明确的 Poly/Kalshi 事件配对，默认留空。主循环现在支持把跨平台机会写进 telemetry，但不会在没有配对表时自行猜测映射。
当前主循环的观测口径：

- T0 结构性套利继续进入 `opportunities` / `trades`
- T1/T2/T3 的信号会额外写入 `data/telemetry/*.strategy_signals.ndjson`
- 新增逻辑约束、事件日历、钱包 alpha 信号也进入 `strategy_signals`，并在 runtime summary 的 `signals.quant_strategies` 下单独聚合
- 因此以后看到 “0 arbs” 时，要同时检查 `strategy_signals`，不要再把它误解成“整套策略都没有信号”

### 可选量化策略输入

这几类策略默认通过文件热加载输入。推荐的常态不是“影子跑完再手动切实盘”，而是四条链路一直跑：

```text
影子模式一直跑
实盘一直跑
扫描器一直跑
晋级器一直跑
实盘只吃通过验证的数据
```

一键拉起这条链路：

```bash
python scripts/run_automated_quant_pipeline.py --dotenv-path .env
```

这个编排入口不会复制 `.env`，只启动一份 bot 主循环，再启动两个旁路数据进程：

- `bot`: 只读根目录 `.env`；如果是 live 模式，内部 shadow-only lane 会验证未晋级钱包
- `wallet-scanner`: 持续从 Polymarket Data API 自动发现活跃钱包，刷新 `wallet_observations.json`
- `wallet-promoter`: 持续读取 shadow telemetry，只有通过 `min_trades / ROI / 集中度 / 回撤` 的钱包才写入 `wallet_profiles.json`

bot 进程读取同一组热加载文件：

```bash
LOGICAL_CONSTRAINTS_FILE=data/quant_inputs/logical_constraints.json
EVENT_BASELINES_FILE=data/quant_inputs/event_baselines.json
WALLET_ALPHA_PROFILES_FILE=data/quant_inputs/wallet_profiles.json
WALLET_ALPHA_OBSERVATIONS_FILE=data/quant_inputs/wallet_observations.json
```

因此未验证钱包最多进入内部 shadow-only 验证通道；实盘执行通道只会消费已经晋级到 `wallet_profiles.json` 的钱包。数据扫描和晋级在旁路进程中按自己的 interval 跑，不会阻塞实盘扫描周期。`.env` 里仍必须显式配置实盘安全确认，例如 `LIVE_TRADING_ACK=true` 和小额的 `LIVE_MAX_*` 限额，否则 live 进程会按安全校验退出。

生成 `.env` 模板：

```bash
python scripts/quant_strategy_config_template.py --format env
```

从 CSV/JSON 生成配置片段：

```bash
python scripts/build_quant_strategy_inputs.py logical-constraints --input data/logical_constraints.csv
python scripts/build_quant_strategy_inputs.py event-baselines --input data/event_baselines.csv
python scripts/build_quant_strategy_inputs.py wallet-observations --input data/wallet_observations.csv
```

自动扫描候选输入：

```bash
# 直接从 Gamma 拉活跃 events，生成同事件逻辑关系候选，供 LLM 复核
python scripts/scan_quant_strategy_inputs.py logical-candidates --fetch-gamma --event-limit 100 --output data/quant_inputs/logical_candidates.json

# 用已配置的 AI_PROVIDER/AI_API_KEY 对候选关系做严格筛选，只保留确定性包含/上界关系
python scripts/scan_quant_strategy_inputs.py logical-rules-llm --candidates data/quant_inputs/logical_candidates.json --output data/quant_inputs/logical_constraints.json

# 从 Polymarket Data API 拉指定钱包最近成交，生成 WALLET_ALPHA_OBSERVATIONS_JSON 候选
python scripts/scan_quant_strategy_inputs.py wallet-observations --wallet 0xabc... --limit 200

# 从你自己的离线 markout/settlement 结果生成 WALLET_ALPHA_PROFILES_JSON
python scripts/scan_quant_strategy_inputs.py wallet-profiles --input data/wallet_markouts.csv --min-trades 30
```

手动运行时也应使用文件热加载，避免复制环境变量后重启：

```bash
LOGICAL_CONSTRAINTS_FILE=data/quant_inputs/logical_constraints.json
EVENT_BASELINES_FILE=data/quant_inputs/event_baselines.json
WALLET_ALPHA_PROFILES_FILE=data/quant_inputs/wallet_profiles.json
WALLET_ALPHA_OBSERVATIONS_FILE=data/quant_inputs/wallet_observations.json
```

扫描脚本支持 `--output` 原子写入这些文件，主循环每个扫描周期自动读取最新有效 JSON；如果文件暂时损坏或接口失败，会继续使用上一次有效内容。

```bash
python scripts/scan_quant_strategy_inputs.py wallet-observations --wallet 0xabc... --limit 200 --output data/quant_inputs/wallet_observations.json
python scripts/scan_quant_strategy_inputs.py wallet-profiles --input data/wallet_markouts.csv --min-trades 30 --output data/quant_inputs/wallet_profiles.json
```

钱包观察不需要提供钱包名单，可以直接从 Polymarket 最近成交里自动发现活跃钱包并持续刷新：

```bash
python scripts/scan_quant_strategy_inputs.py auto-wallet-observations \
  --min-trades 3 \
  --min-notional 100 \
  --max-wallets 25 \
  --repeat-interval-sec 120 \
  --repeat-count 0 \
  --output data/quant_inputs/wallet_observations.json
```

这个命令只生成“观察”，不会自动把活跃钱包当成可跟单钱包；`WALLET_ALPHA_PROFILES_FILE` 必须来自影子验证后的自动晋级结果。

如果不用总编排脚本，也可以拆开跑：

```bash
# bot 主进程：live 时内部 shadow-only 验证候选钱包；实盘只吃已晋级 profiles
ARB_DRY_RUN=false
WALLET_ALPHA_SHADOW_VALIDATION_ENABLED=true
WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED=false
WALLET_ALPHA_OBSERVATIONS_FILE=data/quant_inputs/wallet_observations.json
WALLET_ALPHA_PROFILES_FILE=data/quant_inputs/wallet_profiles.json
```

```bash
# 旁路扫描器：自动发现钱包并刷新观察文件
python scripts/scan_quant_strategy_inputs.py auto-wallet-observations \
  --min-trades 3 \
  --min-notional 100 \
  --max-wallets 25 \
  --repeat-interval-sec 120 \
  --repeat-count 0 \
  --output data/quant_inputs/wallet_observations.json
```

```bash
# 旁路晋级器：从影子 telemetry 生成 markouts，并只晋级通过验证的钱包
python scripts/scan_quant_strategy_inputs.py auto-promote-wallet-profiles \
  --telemetry-dir data/telemetry \
  --lookback-days 7 \
  --min-trades 30 \
  --min-lagged-roi 0.04 \
  --max-concentration 0.35 \
  --max-drawdown 0.35 \
  --repeat-interval-sec 300 \
  --repeat-count 0 \
  --output data/quant_inputs/wallet_profiles.json
```

实盘执行通道读取 `WALLET_ALPHA_PROFILES_FILE` 和 `WALLET_ALPHA_OBSERVATIONS_FILE`，但不要开启 `WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED`；这样只有已晋级的钱包会进入实盘信号。未晋级钱包由 `WALLET_ALPHA_SHADOW_VALIDATION_ENABLED=true` 的内部 shadow-only lane 处理。

CSV 字段约定：

- `logical-constraints`: `subject_market_id,bound_market_id,relation_type,min_violation_bps,tags,max_size_usdc`
- `event-baselines`: `condition_id,baseline_probability,confidence,time_to_event_sec`（脚本会写入 `generated_at`，运行时会扣除已流逝时间；更推荐直接提供 `resolution_at` / `resolution_ts`）
- `wallet-observations`: `wallet_address,market_id,category,action,observed_size_usdc`

钱包跟单需要同时配置 `WALLET_ALPHA_PROFILES_JSON` 和 `WALLET_ALPHA_OBSERVATIONS_JSON`：前者是离线验证后的钱包质量，后者是你观察到的新动作。只给公开“盈利地址”不会触发信号。

观察运行结果：

```bash
python scripts/summarize_runtime_artifacts.py --telemetry-dir data/telemetry --ticks-dir data/ticks
```

其中 `logical_constraint` 只做显式包含/上界关系，例如 `P(候选人胜) <= P(党派胜)`；`event_calendar` 只比较你提供的 baseline 与当前盘口；`wallet_alpha` 只接受已经离线验证过“延迟跟单仍为正”的钱包观察，不会自动相信公开盈利地址。

如果你处在“先跑 3-7 天，看 bot 到底能不能看到机会”的观测期，建议把配置切到更偏探索的档位：

- `ARB_MARKET_FOCUS_KEYWORDS=`：先放开全市场，不要只盯 crypto 关键词
- `ARB_HOT_MARKET_POOL_SIZE=150~200`
- `ARB_HOT_EVENT_POOL_SIZE=50~80`
- `ARB_MIN_LIQUIDITY=300~800`
- `ARB_MIN_VOLUME_24H=200~500`
- `WS_MAX_MARKETS=8~15`
- `T2_MIN_DEVIATION=0.01`：让 T2 至少能看到 1% 级别的模型偏差
- `T2_MAX_SPREAD_BPS=800~1500`
- `T2_MIN_TOP_DEPTH=20~50`
- `T2_MAX_COMPLEMENT_ERROR_BPS=200~300`

更激进的观测档并不等于直接实盘。更合适的做法是：

- 继续保持 `ARB_DRY_RUN=true`
- 继续保持小仓位和较低风险上限
- 用更宽的扫描和 T2 门槛先确认“有没有信号”，再决定是否收紧成实盘档

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
- 用 `--json` 导出结构化结果，方便后续离线分析

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
- `strategy_status.meta.research_overlay` 会显示 overlay 的聚合效果

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
- 先确认风控、执行和 dashboard 状态都稳定

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

新增的逻辑约束、事件日历、钱包 alpha 策略也提供离线 adapter：
`research.backtest.adapters.LogicalConstraintBacktestAdapter`、
`EventCalendarBacktestAdapter`、`WalletAlphaBacktestAdapter`。它们复用线上策略模型，把 snapshot/观察行转换为 `StrategySignal`，适合先在 notebook 或小脚本里做离线筛选，再决定是否放进 shadow 配置。

也可以直接走 runner：

```bash
python -m research.backtest.run --strategy logical-constraint --dataset default
python -m research.backtest.run --strategy event-calendar --dataset default
python -m research.backtest.run --strategy wallet-alpha --dataset default
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

### 远程访问 Dashboard（可选）

Dashboard 现在默认关闭；如果你显式开启它，它也只会在服务器本机监听：

```bash
http://127.0.0.1:8077
```

如果你要看它，推荐在**本地电脑**执行 SSH 端口转发，而不是把 dashboard 暴露到公网：

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

更完整的日志/telemetry 值守建议见 [SERVER_OBSERVABILITY.md](/abs/c:/AppProject/PolymarketBot/SERVER_OBSERVABILITY.md)。

## 免责声明

- 本项目仅供学习研究，不构成投资建议
- 结构性套利机会在实际市场中极其稀缺，Polymarket 有专业做市商（如 Wintermute）在毫秒级修正偏差
- 统计套利依赖模型质量，模型错误会导致亏损
- 做市策略面临逆向选择和库存风险
- 请在充分理解所有风险后自行决定是否使用，作者不对任何损失负责
- 请遵守 Polymarket 服务条款和所在地法律法规（含地理限制）
