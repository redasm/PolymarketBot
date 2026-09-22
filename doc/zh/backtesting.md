[English](../en/backtesting.md) · [中文](../zh/backtesting.md)

# 录制与回测

在相信这条流水线产出的任何数字之前，先读
[research-findings.md](research-findings.md#可复用的方法论)。本仓库四条策略里有三条跑出过
正的回测，最后都被证明是测量假象。

## 录制 tick

设 `TICK_RECORD_ENABLED=true`，每次 WebSocket 订单簿更新都会追加到 NDJSON：

```
data/ticks/
├── 2026-04-11.ndjson      # 按 UTC 日期滚动
├── 2026-04-12.ndjson
└── 2026-04-12.1.ndjson    # 超过 200MB 再滚
```

每行一条更新：

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

**是 tick，不是快照。** 这是连续的逐 token 流。任何按周期采样订单簿再跨 token 拼接的分析，
都会把从未同时存在过的报价接在一起，从而凭空造出套利。这个错误在本项目里造出了 4,667 个
幻影 T0 机会。

保留期由 `DATA_TICKS_RETENTION_DAYS` 和 `DATA_TICKS_MAX_GB` 控制。

## 直接回放

最简形式 —— 把 tick 喂回线上组件：

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

复用生产类而不是重新实现是刻意的：一个重新实现了决策逻辑的回放，测的是那个重新实现。

## 回测 runner

```bash
# 从录制的 tick 生成数据集
python scripts/build_backtest_datasets.py \
  --ticks-dir data/ticks --output-root data/backtest --prefix current

# 运行
python -m research.backtest.run --dataset default
python -m research.backtest.run --strategy logical-constraint --dataset default
python -m research.backtest.run --strategy event-calendar     --dataset default
python -m research.backtest.run --strategy wallet-alpha       --dataset default
```

`research/backtest/` 与 `research_signal/` 是两个不同的包：前者是离线 runner，后者是线上研究
信号层。

完整参数形式（一次真实敏感性测试用的命令）：

```bash
python -m research.backtest.run \
  --strategy logical-constraint \
  --dataset current_quant_sample \
  --dotenv-path data/backtest/current_quant_sample.env \
  --output-dir research/backtest/output/current_quant_sample/logical \
  --execution-model top \
  --holding-period-ms 300000 \
  --max-open-positions 5 \
  --max-total-exposure 100
```

三个可选量化策略需要额外输入，在纯 tick 数据上会报 0 信号 —— 这是预期行为不是故障：

| 策略 | 需要 |
|---|---|
| `logical-constraint` | `LOGICAL_CONSTRAINTS_JSON` 或文件形式 |
| `event-calendar` | 每行 snapshot 带 `baseline_probability`、`confidence`、`time_to_event_sec` |
| `wallet-alpha` | `WALLET_ALPHA_PROFILES_JSON` 以及 snapshot 行上的 `wallet_address` / `action` / `category` |

另有离线 adapter，复用线上策略模型把 snapshot/观察行转成 `StrategySignal`，适合在 notebook 里
先筛一遍再决定要不要放进 shadow 配置：
`research.backtest.adapters.LogicalConstraintBacktestAdapter`、
`EventCalendarBacktestAdapter`、`WalletAlphaBacktestAdapter`。

### 一个（负面的）实例

在 `data/backtest/current_quant_sample` 上 —— 40 个世界杯冠军相关市场、27 个时间步，
baseline / 钱包 / 逻辑规则均为离线构造，因此**不代表真实 alpha**：

| 策略 | fills | 净 PnL |
|---|---|---|
| logical-constraint | 30 / 135 | −28.18 |
| event-calendar | 30 / 1080 | −51.80 |
| wallet-alpha | 10 / 400 | −17.27 |

三者在这个样本里都不可用。这次运行只说明 runner 和执行/markout 管线能跑通，仅此而已 ——
对一个合成输入的回测，这也是唯一能得出的正确结论。

## 让回测可信

下面这些控制项之所以存在，是因为它们的缺失在本项目里造出过假阳性。

### 注入延迟

把入场推迟一个真实的量（从 1 秒开始）重跑，其他什么都不改，比较存活的 PnL。

大部分利润仍在的策略是可信的。像 15 分钟 UPDOWN 那样只剩 9% 的，说明它读的是根本成交不到的
价格。把这一步做成门禁，不要做成可选检查。

### 逐市场解析费率

`BACKTEST_SLIPPAGE_BPS` 管的是滑点，不是费用。费用是 `rate · p · (1-p)`，`rate` 逐市场不同；
默认的 `0.005` 对 crypto Up/Down 低估 14 倍。用
`scripts/verify_polymarket_fees.py` 重新推一遍，并确认回测用的是 `for_market` 费率。

### 退出用可成交价

退出必须按你真能打到的 bid 侧 / VWAP 计价，绝不能用 mid。用 mid 计退出，等于每笔交易白送你
半个价差。

### 打分前先去重

如果多行共享同一个结算结果 —— 同一市场的几个变体、同一持仓的多个快照 —— 按行下注会把一笔
赌注算很多次。先聚合到结算单位。在 TypeSafe 那次评估里，只有 `dedup=market` 的行有意义。

### 看分布，不要只看总和

由一笔异常值撑起来的正总和不是 edge。TypeSafe 那次在 `dedup=market, θ=0.10` 下：40 笔赌注、
命中率 25%、中位每份 −0.0151、只有 10/40 为正。去掉最好的那一笔，总和从 +1.77 掉到 +0.90，
另一个变体直接转负。永远在总和旁边报出中位单笔结果和胜率。

### 样本量

把真实正期望和噪声分开，量级上需要 500–1000 笔独立交易。低于这个量，结果的符号和幅度都是噪声。

## 天气子策略

天气路径解析合约措辞、拉 Open-Meteo GFS 集合预报、估计温度阈值或区间的概率，然后以普通
`BUY_YES` / `BUY_NO` 信号进入现有 T2 执行与退出管理器，入场时按盘口 VWAP 重算 edge。

```dotenv
WEATHER_STRATEGY_ENABLED=true
ARB_DRY_RUN=true
WEATHER_MIN_EDGE=0.10
WEATHER_MIN_CONFIDENCE=0.70
```

对它的任何评估都必须使用 **lead-honest 预报**：每个决策只能用决策时点之前已发布的那一版预报，
而不是该日期的当前最优预报。做完这个修正之后，策略从看起来为正翻成为负，Brier 分数还差于
市场自己的价格。保持 dry-run。
