[English](../en/configuration.md) · [中文](../zh/configuration.md)

# 配置参考

全部配置都是环境变量，通过 `python-dotenv` 从 `.env` 读入 frozen 的 `ArbConfig` dataclass
（`polymarket_arb/config.py`）。运行时不读配置文件，启动后也不再变更 ——
**改 `.env` 必须重启**，唯一例外是下面说的量化输入 JSON 热加载文件。

```bash
cp .env.example .env
$EDITOR .env
```

`.env.example` 是权威的、注释详尽的模板（行内注释为中文）。本文档是同一批设置的分组参考。

> **永远不要提交 `.env`。** 它和 `.env.*`（example 除外）都在 `.gitignore` 里。
> `PRIVATE_KEY` 泄漏 = 钱包被清空，立即且不可逆。

## 确认实际加载了什么

每条日志和每行 telemetry 都带 `run_id=run-<pid>-<UTC start>`。改了变量而 `run_id` 没变，
说明运行中的进程还没读到。

---

## 钱包与认证

| 变量 | 默认 | 含义 |
|---|---|---|
| `PRIVATE_KEY` | — | 钱包私钥。实盘必填。 |
| `POLYMARKET_FUNDER` | — | 持有资金的地址。老用户填 Polymarket 设置页里的 proxy/Safe 地址；deposit wallet 用户填 deposit wallet 地址。 |
| `POLYMARKET_DEPOSIT_WALLET` | *(空)* | `POLYMARKET_FUNDER` 的别名，设置后作为 funder。 |
| `POLYMARKET_SIGNATURE_TYPE` | `2` | `0` EOA / `1` Magic-Email / `2` 浏览器或 Gnosis Safe / `3` Deposit Wallet（`POLY_1271`）。 |

## CLOB / Gamma 端点

| 变量 | 默认 | 含义 |
|---|---|---|
| `CLOB_HOST` | `https://clob.polymarket.com` | CLOB API。 |
| `GAMMA_HOST` | `https://gamma-api.polymarket.com` | 市场/事件元数据 API。 |
| `CHAIN_ID` | `137` | Polygon 主网。 |
| `POLYMARKET_CLOB_CLIENT_VERSION` | `auto` | `auto` 优先 `py-clob-client-v2`，缺失时回退 v1。deposit wallet 必须用 `auto` 或 `v2`。 |
| `CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASSPHRASE` | *(空)* | 可选的已创建 L2 凭证。留空时客户端基于 `PRIVATE_KEY` 派生或创建。 |

## 套利与扫描

| 变量 | 默认 | 含义 |
|---|---|---|
| `ARB_DRY_RUN` | `true` | 只扫描，绝不下单。 |
| `LIVE_TRADING_ACK` | `false` | 第二道确认。真实下单需要 `ARB_DRY_RUN=false` **且**此项为 `true`。 |
| `ARB_MIN_EDGE_USD` | `0.005` | 单次机会最小净利润。 |
| `ARB_MIN_EDGE_PCT` | `0.3` | 最小净利润率（%）。 |
| `ARB_MAX_ORDER_SIZE_USDC` | `50.0` | 单笔上限。 |
| `ARB_DEFAULT_ORDER_SIZE_USDC` | `10.0` | 默认下单量。 |
| `ARB_SCAN_INTERVAL_SEC` | `5` | 定时扫描间隔。 |
| `DIRTY_MARKET_WAKE_THRESHOLD` | `1` | 累计多少个市场出现 best 变化后打断扫描等待。`1` = 任一变化即唤醒（最低延迟）。 |
| `ARB_MARKET_FETCH_LIMIT` | `100` | Gamma 分页大小。 |
| `ARB_MARKET_UNIVERSE_REFRESH_SEC` | `600` | 全 universe 刷新周期，其间只扫热池。 |
| `ARB_HOT_MARKET_POOL_SIZE` | `80` | 热市场池大小。 |
| `ARB_HOT_EVENT_POOL_SIZE` | `30` | 热事件池大小。 |
| `ARB_MARKET_FOCUS_KEYWORDS` | *(视模板)* | 逗号分隔主题过滤，留空=全市场。 |
| `ARB_MIN_LIQUIDITY` | `1000` | 最小流动性（USDC）。 |
| `ARB_MIN_VOLUME_24H` | — | 最小 24h 成交量（USDC）。 |
| `ARB_MAX_MULTI_OUTCOME_LEGS` | `20` | 腿数超过则跳过整个事件。 |

关键词过滤有个坑：crypto Up/Down 市场的 `volume24hr ≈ 0`，会在 fetch 阶段被
`ARB_MIN_VOLUME_24H` 滤掉，所以它们是通过 slug 直查绕过 volume 过滤拉进来的（见 UPDOWN 一节）。
窄关键词 + volume 过滤的组合可能**静默排除掉你正打算研究的那类市场**。

### 实盘安全闸门

仅在 `ARB_DRY_RUN=false` 时生效。

| 变量 | 默认 | 含义 |
|---|---|---|
| `LIVE_MAX_ORDER_SIZE_USDC` | `10.0` | 实盘单笔硬上限。 |
| `LIVE_MAX_TOTAL_EXPOSURE_USDC` | `100.0` | 实盘总敞口硬上限。 |
| `LIVE_MIN_NET_EDGE_BPS` | `25` | 扣费后 edge 下限（bps）。 |
| `LIVE_MIN_NET_EDGE_USD` | `0.0025` | 扣费后每股 edge 下限。 |
| `LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC` | `5` | 快照过旧则拒绝该信号。**按信号涉及的 token 单独判定**，不会因单个空闲 token 拖垮全局。 |
| `LIVE_MIN_WS_HIT_RATIO` | `0.25` | 近期 WS 盘口命中率低于此值时拒绝下单。 |
| `LIVE_REQUIRE_PORTFOLIO_SYNC` | `true` | 开仓前要求账户同步成功。 |
| `LIVE_ALLOW_ZERO_TAKER_FEE` | `false` | 按零费假设运行需要显式确认。 |

## 风险管理

| 变量 | 默认 | 含义 |
|---|---|---|
| `RISK_MAX_OPEN_POSITIONS` | `10` | 同时持仓上限。 |
| `RISK_MAX_EXPOSURE_PER_MARKET` | `100.0` | 单 `condition_id` USDC 上限。 |
| `RISK_MAX_TOTAL_EXPOSURE` | `500` | 全局成本基础上限。 |
| `RISK_MAX_DAILY_LOSS` | `50` | 日亏损止损。T0 的 BINARY/MULTI_OUTCOME 绕过此检查，DIRECTIONAL 不绕。 |
| `RISK_MAX_CONSECUTIVE_FAILURES` | — | 熔断阈值。 |
| `RISK_MARKET_COOLDOWN_SEC` | `60` | 同一事件两次执行的最小间隔。 |
| `RISK_PENDING_RESERVATION_SEC` | — | pending 订单预留敞口的保留时长。若启用可能长时间挂着的订单，调到 120–300。 |
| `RISK_BREAKER_AUTO_RESET_SEC` | — | 无新失败多少秒后自动解除熔断，`0` = 不自动恢复。 |

### 账户同步

低频对账，不参与高频扫描/执行路径。

| 变量 | 默认 | 含义 |
|---|---|---|
| `PORTFOLIO_SYNC_ENABLED` | `false` | 同步真实持仓与当日已实现盈亏到 dashboard/风控状态。 |
| `PORTFOLIO_SYNC_INTERVAL_SEC` | `60` | 同步周期。 |
| `PORTFOLIO_SYNC_TIMEOUT_SEC` | `5` | 请求超时。 |
| `PORTFOLIO_SYNC_USER_ADDRESS` | *(空)* | 查询地址，留空用 `POLYMARKET_FUNDER`。 |
| `DATA_API_HOST` | `https://data-api.polymarket.com` | Polymarket Data API。 |

## 费率

| 变量 | 默认 | 含义 |
|---|---|---|
| `POLYMARKET_TAKER_FEE_RATE` | `0.005` | taker 费率兜底值。市场 fee metadata 存在时以 metadata 为准。 |
| `KALSHI_TAKER_FEE_RATE` | `0.003` | T1 用的 Kalshi taker 费率假设。 |

Polymarket Fee V2（2026-03-30 起）按 `rate · p · (1-p)` 收费，`p = 0.5` 时最高。
`rate` 逐市场不同：crypto Up/Down 的 `feeSchedule.rate = 0.07`，是上面默认值的 14 倍。
Bot 在 T0/T2 都走 `for_market` 解析，所以默认值只是兜底 —— 但
**任何用默认值的离线分析都会严重低估成本**。见
[research-findings.md](research-findings.md#费率模型错了-14-倍)。

## Edge 引擎

| 变量 | 默认 | 含义 |
|---|---|---|
| `EDGE_MIN_BPS` | `100` | 最小触发 edge（bps）。 |
| `EDGE_MAX_SPREAD_BPS` | `500` | 最大可接受 spread。 |
| `EDGE_MIN_CONFIDENCE` | — | 置信度下限。 |
| `EDGE_FULL_CONFIDENCE_BPS` | — | 置信度饱和所需 edge。 |
| `EDGE_OBI_WEIGHT` | — | 订单簿不平衡在置信度中的权重。 |
| `EDGE_VOL_SPIKE_RATIO` / `EDGE_VOL_SPIKE_MULTIPLIER` | — | 判定波动突刺的 `fast/slow` 比值，及置信度惩罚（<1）。 |
| `EDGE_VOL_CALM_RATIO` / `EDGE_VOL_CALM_MULTIPLIER` | — | 判定平静的比值，及置信度加成（>1）。 |

## 波动率估算

| 变量 | 默认 | 含义 |
|---|---|---|
| `VOL_FAST_MINUTES` | `60` | 快窗口。 |
| `VOL_SLOW_MINUTES` | `360` | 慢窗口。 |
| `VOL_MIN_SAMPLES` | `20` | 视为已 warmup 的最少样本数。 |

## T2 —— 统计套利

### 质量门

| 变量 | 默认 | 含义 |
|---|---|---|
| `T2_MIN_DEVIATION` | `0.05` | 最小绝对概率偏差。 |
| `T2_MAX_SPREAD_BPS` | — | 双边最大 spread。 |
| `T2_MIN_TOP_DEPTH` | — | 盘口顶档最小可成交深度。 |
| `T2_MAX_COMPLEMENT_ERROR_BPS` | — | Yes/No 互补误差上限。 |
| `T2_MAX_SIGNALS_PER_MARKET_PER_HOUR` | `5` | 单市场频率上限，防止一个稳定错价每周期重复触发。 |
| `T2_MAX_SIGNALS_PER_CYCLE` | `30` | 单周期信号上限。 |
| `T2_MAX_HORIZON_DAYS` | `180` | 拒绝结算过远的市场 —— 长尾仓位 IRR 不划算。 |
| `T2_LONG_HORIZON_DAYS` / `T2_LONG_HORIZON_MIN_NET_EDGE_BPS` | `30` / `200` | 超过该期限的市场需要更高净 edge。 |
| `T2_NEAR_EFFICIENT_MIN_NET_EDGE_BPS` | `300` | Finance/Crypto 类目 maker-taker gap 仅 0.17pp，taker fee 会吞掉普通 edge。 |
| `T2_REJECT_PRICE_BELOW` / `T2_REJECT_PRICE_ABOVE` | `0.10` / `0.90` | 冷门/热门税闸门。价格 <0.10 买 YES 平均 −41% EV。 |

### 退出

| 变量 | 默认 | 含义 |
|---|---|---|
| `T2_STOP_LOSS_BPS` | `300` | 静态止损。 |
| `T2_TAKE_PROFIT_CAPTURE_PCT` | `0.6` | 止盈前要吃掉模型 edge 的比例。 |
| `T2_MAX_HOLD_SEC` | `21600` | 时间止损（6h）。 |
| `T2_EXIT_EVAL_INTERVAL_SEC` | `30` | 退出评估周期。 |
| `T2_OPTIMAL_STOPPING_ENABLED` | `true` | Bellman 退出阈值。 |
| `T2_SCALE_OUT_TRANCHES` | `3` | 止盈与最优停止触发时每次卖 `size_remaining × (1/剩余份数)`。止损/时间止损/floor_dump 仍一次全平。`1` = 退化为旧的一次性行为。 |
| `T2_STOP_LOSS_DYNAMIC_ENABLED` | `false` | ATR 等价动态止损：`clamp(k × realised_vol_bps, min, max)`。未验证，见 [pending-validations.md](pending-validations.md)。 |
| `T2_STOP_LOSS_DYNAMIC_K` / `_MIN_BPS` / `_MAX_BPS` / `_WARMUP` | `2.0` / `100` / `1000` / `5` | 动态止损参数。 |
| `T2_POST_EXIT_COOLDOWN_SEC` | `86400` | 平仓后同市场重入冷却，跨重启持久化。影子阶段可缩到 1800 以更快积累样本。 |
| `T2_RECENT_EXITS_STATE_FILE` | `data/telemetry/recent_exits.json` | 冷却状态文件。 |

### UPDOWN（现货锚定短周期市场）

| 变量 | 默认 | 含义 |
|---|---|---|
| `T2_UPDOWN_ENABLED` | `false` | 在扫描池 / WS 订阅选择中给现货锚定市场优先级 boost。 |
| `T2_UPDOWN_PRIORITY_BOOST` | `1.0` | boost 权重。 |
| `T2_UPDOWN_SYMBOLS` | `btc,eth` | 探测的币种。 |
| `T2_UPDOWN_WINDOW_MINUTES` | `15` | 通过 `/events?slug={sym}-updown-{w}m-{slot}` 探测的窗口长度。Polymarket 在 2026 年内改成了 5 分钟。 |
| `T2_UPDOWN_SLOTS_AHEAD` | `4` | 向前拉几个窗口。 |
| `T2_UPDOWN_RTDS_MODE` | `shadow` | `off` / `shadow` / `primary`。shadow 仍用 Binance 定价，只产出 basis telemetry。 |
| `T2_UPDOWN_RTDS_STALENESS_SEC` | `30` | 超过该年龄回落 Binance。 |
| `T2_UPDOWN_BASIS_LOG_INTERVAL_SEC` | `300` | `updown_spot_basis` 行的写入间隔。 |
| `T2_UPDOWN_BASIS_ALERT_BPS` | `50` | 两源偏离超过该值告警。 |

UPDOWN 按 Polymarket 自己的价格源结算，不是 Binance。两者的 basis 恰恰在 UPDOWN 最敏感的
时候最大。

### 影子模式研究规则

默认全部只观测。

| 变量 | 默认 | 含义 |
|---|---|---|
| `T2_NEAR_CERTAINTY_SHADOW_MODE` | `true` | 只记录 `near_certainty.would_apply_*`，不改仓位/置信度。 |
| `T2_NEAR_CERTAINTY_HIGH_THRESHOLD` / `_LOW_THRESHOLD` | `0.92` / `0.08` | 视为近确定的区间。 |
| `T2_NEAR_CERTAINTY_SIZE_MULTIPLIER` / `_CONFIDENCE_DELTA` | `0.60` / `-0.08` | 晋级后生效的调整量。 |
| `T2_BARBELL_ENABLED` | `false` | 把 T2 资金切成 data-driven（~80%）和 tail（~15%）两桶，tail 桶有余量时放松尾部折扣。 |
| `T2_BARBELL_TAIL_BUDGET_PCT` / `_TAIL_RELAXED_MULTIPLIER` | `0.15` / `0.85` | barbell 参数。 |

### 天气子策略

| 变量 | 默认 | 含义 |
|---|---|---|
| `WEATHER_STRATEGY_ENABLED` | `false` | 用 Open-Meteo GFS 集合预报给温度合约定价。 |
| `WEATHER_MIN_EDGE` / `WEATHER_MIN_CONFIDENCE` | `0.10` / `0.70` | 入场阈值。 |
| `WEATHER_MAX_SPREAD_BPS` / `WEATHER_MIN_TOP_DEPTH` | `180` / `25` | 盘口质量门。 |
| `WEATHER_FORECAST_TTL_SEC` / `WEATHER_REQUEST_TIMEOUT_SEC` / `WEATHER_MAX_MARKETS` | `900` / `10` / `40` | 预报缓存与限额。 |

该策略在改用 lead-honest 预报后测出为负。保持关闭。

## T3 —— 做市

| 变量 | 默认 | 含义 |
|---|---|---|
| `MAKER_STRATEGY_ENABLED` | `false` | 启用 T3。默认关闭：影子数据 53/53 平仓全亏（见 [research-findings](research-findings.md)）。任何实盘都应保持关闭 —— post-only/GTC 行为也会在验证工程链路时引入多余变量。 |
| `MAKER_MAX_HOLD_SEC` | `21600` | 超时强平。 |
| `MAKER_STOP_LOSS_BPS` / `MAKER_TAKE_PROFIT_BPS` | `300` / `200` | 退出阈值。 |
| `MAKER_EXIT_EVAL_INTERVAL_SEC` | `30` | 退出评估周期。 |

### 流动性奖励带

| 变量 | 默认 | 含义 |
|---|---|---|
| `MAKER_REWARDS_ENABLED` | `true` | 读取每个市场的 `rewards_max_spread`，约束报价落进计分区间。 |
| `MAKER_REWARDS_TTL_SEC` / `_NEGATIVE_TTL_SEC` / `_TIMEOUT_SEC` | `900` / `300` / `5` | 缓存与超时。 |
| `MAKER_REWARDS_PREFETCH_PER_CYCLE` | `20` | 每周期后台线程预热的新市场数。 |
| `MAKER_REWARDS_ONLY` | `false` | 只在有奖励带的市场做市。 |

### 挂单计分校验

挂在带内 ≠ 真的在计分。

| 变量 | 默认 | 含义 |
|---|---|---|
| `MAKER_SCORING_AUDIT_ENABLED` | `true` | 轮询 `/orders-scoring`，把 `maker_scoring_audit` 写进 `risk_events`。 |
| `MAKER_SCORING_AUDIT_INTERVAL_SEC` | `60` | 审计周期。 |
| `MAKER_SCORING_CANCEL_UNSCORED` | `false` | 超过 grace 仍未计分是否撤单。默认只观测。 |
| `MAKER_SCORING_UNSCORED_GRACE_SEC` | `90` | 区分瞬时与持续未计分的宽限窗口。 |

### 抗狙击

阈值单位是 **tick 而非 bps** —— 1 tick 在 0.50 是 200bps、在 0.05 是 2000bps，用 bps 会让低价
市场永久暂停、高价市场形同虚设。

| 变量 | 默认 | 含义 |
|---|---|---|
| `T3_ANTI_SNIPE_ENABLED` | `true` | 总开关。 |
| `T3_ANTI_SNIPE_JUMP_TICKS` / `_JUMP_PAUSE_SEC` | `3` / `20` | mid 跳变超过该 tick 数则暂停该 token 报价。 |
| `T3_ANTI_SNIPE_STABLE_TICKS_REQUIRED` / `_STABLE_BAND_TICKS` | `2` / `1` | 恢复前需要的连续在带内观测次数。 |
| `T3_ANTI_SNIPE_EMA_ALPHA` / `_MID_HISTORY` | `0.3` / `7` | 报价锚点 = 中位数（抗单点异常）+ EMA（抗抖动）。`alpha=0` 关闭 EMA。 |
| `T3_ANTI_SNIPE_POST_FILL_COOLDOWN_SEC` | `15` | 成交后冷却 —— 刚被吃说明对手方可能有信息优势。 |
| `T3_ANTI_SNIPE_MAX_CHASE_TICKS` | `2` | 单次报价移动上限，`0` = 不限制。 |

### Flow bias

| 变量 | 默认 | 含义 |
|---|---|---|
| `T3_FLOW_BIAS_ENABLED` | `true` | 跟踪每市场 `taker_yes_share`。当前仅 telemetry。 |
| `T3_FLOW_BIAS_WINDOW_SEC` / `_MIN_TRADES` / `_STRONG_THRESHOLD` | `3600` / `20` / `0.55` | 聚合窗口与显著性阈值。 |
| `T3_FLOW_BIAS_INVENTORY_WEIGHT` | `0.5` | 把聚合流当作合成库存压力来引导报价的权重。`0.0` = 只观测。 |
| `T3_FLOW_STATE_FILE` | `data/telemetry/flow_state.json` | 持久化状态。 |

## T1 —— 跨平台

| 变量 | 默认 | 含义 |
|---|---|---|
| `CROSS_PLATFORM_PAIRS_JSON` | *(空)* | 显式 Polymarket↔Kalshi 配对表。留空即完全关闭 T1；bot 绝不猜映射。 |
| `CROSS_PLATFORM_ENTITY_VETO_ENABLED` | `true` | 否决问题文本在阈值/日期/方向上不一致的配对。只否决不建对；任一侧缺问题原文则放行。 |
| `CROSS_PLATFORM_MIN_TOKEN_OVERLAP` | `0` | 词元 Jaccard 下限。`0` = 关闭；跨平台措辞差异大，容易误伤。 |

手写配对表最危险的失败模式不是漏配，而是**配错**：阈值不同的两个市场，
`poly_yes + kalshi_no < 1` 看起来仍然像无风险套利。

## 量化输入热加载

主循环每个扫描周期重读这些 JSON（旁路 worker 原子写入）。文件暂时损坏或拉取失败时，
继续使用上一次有效内容。

| 变量 | 默认 |
|---|---|
| `LOGICAL_CONSTRAINTS_FILE` | `data/quant_inputs/logical_constraints.json` |
| `EVENT_BASELINES_FILE` | `data/quant_inputs/event_baselines.json` |
| `WALLET_ALPHA_PROFILES_FILE` | `data/quant_inputs/wallet_profiles.json` |
| `WALLET_ALPHA_OBSERVATIONS_FILE` | `data/quant_inputs/wallet_observations.json` |
| `RESEARCH_SIGNAL_FEEDS_FILE` | `data/quant_inputs/research_feeds.json` |

同名的内联 JSON 变量（`LOGICAL_CONSTRAINTS_JSON` 等）也存在，但改动需要重启，优先用文件形式。

| 变量 | 默认 | 含义 |
|---|---|---|
| `WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED` | `false` | 允许未验证钱包产生影子信号。仅 shadow/dry-run。 |
| `WALLET_ALPHA_SHADOW_VALIDATION_ENABLED` | `false` | live 进程内的候选钱包 shadow-only 验证通道，不会真下单。默认关闭：跟单跟到结算为 −28.6% ROI。 |
| `WALLET_ALPHA_SHADOW_MAX_SIGNALS_PER_CYCLE` | `5` | 每周期候选预算。 |
| `WALLET_ALPHA_SHADOW_MAX_EXEC_MS_PER_CYCLE` | `250` | 每周期时间预算。 |

只有晋级进 `wallet_profiles.json` 的钱包才会进实盘执行通道。注意整条策略线实测 −28.6% ROI，
见 [research-findings.md](research-findings.md#钱包跟单-wallet-alpha)。

## WebSocket

| 变量 | 默认 | 含义 |
|---|---|---|
| `WS_ENABLED` | `true` | 用 WebSocket 推送替代纯 REST 轮询。 |
| `WS_MAX_MARKETS` | `80` | 同时追踪的市场数（每个两个 token）。应不小于 `ARB_HOT_MARKET_POOL_SIZE`。 |
| `WS_REFRESH_CYCLES` | `200` | 每隔多少扫描周期重新选择追踪集合。 |
| `WS_VOL_FEED_INTERVAL_SEC` | `60` | mid price 喂入 `VolEstimator` 的间隔。 |
| `USER_WS_ENABLED` | `true` | user 频道：自己的成交直接回写风控与退出管理器，不等下一轮 REST 轮询。需要 L2 凭证，取不到则回退轮询。 |
| `USER_WS_QUEUE_SIZE` / `USER_WS_MAX_EVENTS_PER_CYCLE` | `2000` / `500` | user 频道队列限制。 |

调高 `WS_MAX_MARKETS` 而不同步调高 `ARB_HOT_MARKET_POOL_SIZE` 会浪费订阅槽位。反过来更糟：
热池里没被 WS 覆盖的市场每个周期都要走 REST，扫描周期会从秒级拖到数分钟（`WS_MAX_MARKETS`
偏小时启动日志会给出警告）。
Polymarket 已于 2025-05 移除 100 token 订阅上限；真正的瓶颈在客户端 —— `websockets` 库默认
`max_size=1MB`，而服务端 `initial_dump=true` 会一次性 dump 全簿，token 多时单包超过 1MB，
客户端主动以 1009（`MESSAGE_TOO_BIG`）关闭并陷入重连死循环。`websocket_feed.py` 已设
`max_size=16MB`。

## 订单簿拉取

| 变量 | 默认 | 含义 |
|---|---|---|
| `ORDERBOOK_SNAPSHOT_TTL_SEC` | `0.5` | REST 快照缓存 TTL。保持在 0.2–0.8：太小回到高频 REST 压力，太大让扫描看到的盘口变旧。 |
| `ORDERBOOK_WS_SNAPSHOT_MAX_AGE_SEC` | `10` | 超过该年龄放弃 WS 快照改走 REST —— WS 连接存活时除外（见下一行）。 |
| `ORDERBOOK_WS_LIVENESS_SEC` | `20` | Polymarket 只在盘口变化时推送，连接正常时安静盘口仍是最新的。只要 WS 在该窗口内处理过任何消息（含心跳），本次连接内刷新过的镜像盘口不论多久没变都直接使用；断线时回到按年龄判断。实盘 `feed_health` 门禁仍按单本盘口年龄判断。 |
| `ORDERBOOK_RETRY_COUNT` / `ORDERBOOK_RETRY_DELAY_SEC` | `2` / `0.15` | REST 重试策略。 |
| `ORDERBOOK_MISSING_COOLDOWN_SEC` | `300` | CLOB 明确返回 "No orderbook exists" 后对该 token 的冷却时长。 |

## 录制与 telemetry

| 变量 | 默认 | 含义 |
|---|---|---|
| `TICK_RECORD_ENABLED` / `TICK_RECORD_DIR` | `false` / `data/ticks` | 把每次 WS 订单簿更新写入 NDJSON。任何 tick 级回测都依赖它。 |
| `TELEMETRY_RECORD_ENABLED` / `TELEMETRY_RECORD_DIR` | `false` / `data/telemetry` | 机会 / 交易 / 风控事件录制。 |
| `TELEMETRY_ASYNC_WRITE` | `true` | 后台线程写；主循环只付 JSON 编码 + 入队，不阻塞在 fsync。队列满时丢最旧事件。 |
| `TELEMETRY_ASYNC_QUEUE_SIZE` | `10000` | 写队列深度。 |
| `SHADOW_MAKER_FILL_LATENCY_SEC` | `2.0` | dry-run 下模拟 maker 单挂出后必须等待的最小秒数（粗糙的队列位置损耗）。 |

任何无人值守的观测运行都应该两个都打开。不开的话事后无法回答"为什么没有信号"。

## 数据保留

长期运行会撑爆磁盘。

| 变量 | 默认 |
|---|---|
| `DATA_CLEANUP_ENABLED` | `true` |
| `DATA_CLEANUP_INTERVAL_SEC` | `3600` |
| `DATA_TICKS_RETENTION_DAYS` / `DATA_TICKS_MAX_GB` | `7` / `5` |
| `DATA_TELEMETRY_RETENTION_DAYS` / `DATA_TELEMETRY_MAX_GB` | `14` / `2` |
| `DATA_RESEARCH_CACHE_RETENTION_DAYS` / `_MAX_GB` | `14` / `1` |
| `DATA_BACKTEST_RETENTION_DAYS` / `DATA_BACKTEST_MAX_GB` | `30` / `2` |

## Research signal 层

| 变量 | 默认 | 含义 |
|---|---|---|
| `RESEARCH_SIGNAL_ENABLED` | `false` | 启用研究信号聚合。 |
| `RESEARCH_SIGNAL_WINDOW_SEC` | `86400` | 回看窗口。 |
| `RESEARCH_SIGNAL_MAX_ITEMS` | `5` | 每轮保留条数。 |
| `RESEARCH_SIGNAL_CACHE_TTL_SEC` / `_CACHE_DIR` | `300` / `data/research_signal` | 磁盘缓存。 |
| `RESEARCH_SIGNAL_HTTP_JSON_SOURCES` | *(空)* | 额外 HTTP JSON 源（JSON list）。 |
| `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED` | `true` | Fear & Greed collector。免费、无需鉴权、每个 crypto 主题一行。不是硬闸门。 |
| `RESEARCH_SIGNAL_MANIFOLD_ENABLED` | `true` | Manifold Markets 群体概率。二级源，主要在新闻 RSS 稀薄的政治/地缘/体育/颁奖类有用。判定：≥0.60 看多，≤0.40 看空。 |

## 回测

| 变量 | 默认 |
|---|---|
| `BACKTEST_ENABLED` | `false` |
| `BACKTEST_DATA_DIR` | `data/backtest` |
| `BACKTEST_DEFAULT_DATASET` | `default` |
| `BACKTEST_SLIPPAGE_BPS` | `5` |
| `BACKTEST_REPORTS_DIR` | `research/backtest/output` |

## Dashboard 与日志

| 变量 | 默认 | 含义 |
|---|---|---|
| `DASHBOARD_ENABLED` | `false` | FastAPI 监控后端。 |
| `DASHBOARD_PORT` | `8077` | 端口。**设计上只监听 loopback。** 用 SSH 隧道访问，不要改绑 `0.0.0.0`。 |
| `LOG_LEVEL` | `INFO` | `httpx`/`httpcore` 请求日志会自动降到 WARNING。 |
| `LOG_FILE` | `arb_bot.log` | 日志文件路径。 |

## 通知（飞书）

只要填了完整的飞书应用机器人配置就自动启用，否则自动关闭。

| 变量 | 默认 | 含义 |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | *(空)* | 应用机器人凭证。 |
| `FEISHU_OPEN_ID` | *(空)* | 单个固定接收目标，只支持 `open_id`。 |
| `FEISHU_API_BASE` | — | 飞书开放平台 API 基础地址。 |
| `NOTIFICATION_COOLDOWN_SEC` | `30` | 同类通知冷却。 |
| `NOTIFY_ON_ARB_FOUND` | `false` | 发现机会通知。默认关，会刷屏。 |
| `NOTIFY_ON_ARB_FOUND_IN_SHADOW` | `false` | dry-run 下也推机会。开启时所有 shadow 推送自动加 `🌓 [SHADOW]` 前缀。 |
| `NOTIFY_ON_TRADE_SUCCESS` / `NOTIFY_ON_TRADE_FAILURE` | `true` / `true` | 成交通知。 |
| `NOTIFY_ON_FATAL_ERROR` | `true` | API 连续错误、风控熔断等。 |
| `NOTIFY_ON_PNL_ALERT` | `true` | 基于已记录 `daily_pnl` 的阈值提醒。 |
| `NOTIFY_ON_DAILY_SUMMARY` | `true` | 每日汇总。 |
| `PNL_PROFIT_ALERT_USDC` / `PNL_LOSS_ALERT_USDC` | `20` / `10` | 提醒阈值。 |
| `FATAL_ERROR_COOLDOWN_SEC` | — | 同类严重错误冷却。 |
| `DAILY_SUMMARY_TIME_HHMM` / `DAILY_SUMMARY_TIMEZONE` | `08:05` / `Asia/Shanghai` | 日报时间。风控日切仍按 UTC。 |
| `NOTIFICATION_STATE_FILE` | `data/telemetry/notification_state.json` | 去重与计数，跨重启保留。 |

## LLM 与打分模型

两者都不在交易热路径上。见 [ai-configuration.md](ai-configuration.md)。

| 变量 | 默认 | 含义 |
|---|---|---|
| `AI_PROVIDER` | `openai` | `openai` / `anthropic` / `ollama` / `deepseek` / `gemini`。 |
| `AI_API_KEY` | *(空)* | 未设时回退 `OPENAI_API_KEY`。 |
| `AI_API_BASE` | *(空)* | 自定义端点，留空用 provider 默认。 |
| `AI_MODEL` | `gpt-4o` | 模型名。 |
| `AI_TEMPERATURE` | `0.1` | 采样温度。 |
| `TYPESAFE_API_KEY` | *(空)* | TypeSafe Jev key。 |
| `TYPESAFE_MODEL` | `jev-1.13.0` | 必须 pin —— `jev-latest` 会漂移，校准阈值随之失效。 |
| `TYPESAFE_RPS` | `10` | 客户端令牌桶。 |
| `TYPESAFE_TIMEOUT_SEC` / `TYPESAFE_MAX_RETRIES` | `10` / `3` | 超时与 429/529 重试。 |
| `TYPESAFE_BASE_URL` | *(空)* | 留空用 `https://api.typesafe.ai`。 |

---

## 观测档预设

在靠近实盘资金之前，先放宽扫描并把一切都录下来：

```dotenv
ARB_DRY_RUN=true
TICK_RECORD_ENABLED=true
TELEMETRY_RECORD_ENABLED=true
RESEARCH_SIGNAL_ENABLED=true

ARB_MARKET_FOCUS_KEYWORDS=          # 全市场，不做主题过滤
ARB_HOT_MARKET_POOL_SIZE=150
ARB_HOT_EVENT_POOL_SIZE=50
ARB_MIN_LIQUIDITY=500
ARB_MIN_VOLUME_24H=300
WS_MAX_MARKETS=150

T2_MIN_DEVIATION=0.01               # 仅影子期，见下
T2_MAX_SPREAD_BPS=1000
T2_MIN_TOP_DEPTH=30
T2_MAX_COMPLEMENT_ERROR_BPS=250
```

更宽的观测档不等于朝实盘走了一步。它唯一的目的是回答"到底有没有信号"。保持
`ARB_DRY_RUN=true`、保持小风险上限，在考虑真实下单前把阈值收回去。尤其
`T2_MIN_DEVIATION=0.01` 远低于任何能扛住费用的水平。
