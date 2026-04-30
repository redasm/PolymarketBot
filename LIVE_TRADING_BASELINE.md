# PolymarketBot 实盘改造基线文档

## 1. 文档目的

这份文档用于固定当前排查结论，并作为后续代码修改的唯一基线。

后续所有改动，优先围绕以下目标展开：

1. 让机器人从 `DRY RUN` 平稳切换到可控的小额实盘。
2. 先解决“数据稳定性”和“执行可靠性”，再放大资金。
3. 让面板、日志、风控、订单执行的口径一致，避免误判。


## 2. 当前结论

### 2.1 当前程序并不是“没执行”

当前主因不是“发现套利机会但程序没有进入执行逻辑”，而是：

- 当前配置为 `ARB_DRY_RUN=true`
- 代码已经进入 `execute_arbitrage(...)`
- 但执行器在 `dry_run` 下只记录模拟成交，不发真实订单

因此面板中的“最近套利机会”代表：

- 理论可套利机会
- 不是实盘已成交利润
- 不是已落袋收益


### 2.2 当前面板中的盈利含义

面板里类似 `+$0.03`、`+$0.04` 的数字，代表的是单份组合头寸的理论净利，不是整笔订单的最终净利润。

粗略换算：

- 若单次下单金额约 `$10`
- 且该机会利润率约 `3%~4%`
- 则单次理论总盈利约 `$0.30~$0.40`

真实收益通常还会再打折，受以下因素影响：

- 深度不足
- 滑点
- 部分成交
- 撤单失败
- WebSocket 数据不稳定


### 2.3 当前程序不适合直接放大资金实盘

虽然策略能持续识别机会，但当前版本仍存在实盘前必须处理的问题：

- WebSocket 长时间反复断连
- WebSocket 消息解析对部分返回格式兼容不完整
- 面板“机会”和“执行”统计口径容易误导
- 代码未看到严格的钱包可用余额校验
- 当前费率模型使用固定保守估算，和实际加密市场费率未完全对齐


## 3. 当前关键配置基线

基于当前仓库配置与排查结果，重要参数如下：

- `ARB_DRY_RUN=true`
- `ARB_MIN_EDGE_USD=0.01`
- `ARB_MIN_EDGE_PCT=0.6`
- `WS_ENABLED=true`
- `WS_MAX_MARKETS=5`
- `EDGE_MIN_CONFIDENCE=0.4`
- `AI_ENABLED=false`
- `RESEARCH_SIGNAL_ENABLED=true`

代码默认风控基线：

- `ARB_DEFAULT_ORDER_SIZE_USDC=10`
- `ARB_MAX_ORDER_SIZE_USDC=50`
- `RISK_MAX_EXPOSURE_PER_MARKET=100`
- `RISK_MAX_TOTAL_EXPOSURE=500`
- `RISK_MAX_DAILY_LOSS=50`


## 4. 实盘资金建议

### 4.1 如果不改参数直接切实盘

不建议直接这样做。

如果完全沿用当前参数，合理充值区间应至少覆盖总敞口上限并留缓冲：

- 建议资金：`600~800 USDC`

原因：

- 当前总敞口上限是 `500 USDC`
- 还需要覆盖手续费、滑点、失败重试和资金占用


### 4.2 更推荐的首阶段试跑方式

推荐先做“小额实盘验证”，不要一开始就跑大资金。

建议第一阶段资金：

- `150~250 USDC`

同时把参数先收紧到更保守的范围：

- `ARB_DRY_RUN=false`
- `ARB_DEFAULT_ORDER_SIZE_USDC=5`
- `ARB_MAX_ORDER_SIZE_USDC=10~15`
- `RISK_MAX_EXPOSURE_PER_MARKET=20~30`
- `RISK_MAX_TOTAL_EXPOSURE=100~150`
- `RISK_MAX_DAILY_LOSS=10~15`
- `RISK_MAX_CONSECUTIVE_FAILURES=3`


## 5. 当前已知问题清单

### P0：必须先处理

- `DRY RUN` 模式下永远不会产生真实执行统计
- WebSocket 存在持续断连问题
- WebSocket 存在 `list object has no attribute 'get'` 解析异常
- 未见明确的钱包可用余额硬校验

### P1：应在小额实盘前处理

- 手续费模型已改为 CLOB `feeRate * price * (1-price)` 本地估算；小额实盘前仍需按市场核实 `feesEnabled/getClobMarketInfo`
- 面板应明确区分“理论机会”“模拟执行”“真实执行”
- 需要把真实成交后的收益、失败、部分成交单独统计

### P2：实盘稳定后优化

- 增加事件级别去重和重复机会压缩显示
- 增加单市场连续失败保护
- 增加资金利用率和真实收益报表


## 6. 后续改造顺序

后续改造严格按以下顺序推进。

### 第一阶段：数据链路稳定化

目标：先保证“看到的机会”是稳定、可信、可复现的。

需要完成：

- 修复 WebSocket 对 `list` 形态消息的兼容
- 降低断连后对 `EnhancedBookStore` 的污染风险
- 补充 WebSocket 状态与订阅状态日志
- 明确当前面板展示的是哪个市场、哪个事件、哪个 token

验收标准：

- 运行 2 小时内无持续性解析报错
- WebSocket 断连后可自动恢复
- 面板订阅市场和实际扫描市场口径一致


### 第二阶段：执行链路实盘化

目标：从“模拟执行”切到“小额真实执行”。

需要完成：

- 增加钱包余额或下单前可用资金检查
- 明确真实下单前后的状态机
- 区分 `PENDING / PARTIAL / FILLED / FAILED / CANCELLED`
- 对失败回滚与部分成交做单独告警

验收标准：

- 小额单笔订单可成功下发
- 失败时不会错误计入盈利
- 部分成交会被风控正确记录


### 第三阶段：面板与报表口径修正

目标：避免“看起来赚钱，实际上没成交”的误判。

需要完成：

- 面板新增“理论机会收益”
- 面板新增“模拟执行收益”
- 面板新增“真实已实现收益”
- 面板新增“真实未实现敞口”
- 面板按事件聚合重复机会

验收标准：

- 用户可一眼区分候选机会与真实落袋收益
- `已执行` 只统计真实成交完成的套利
- `日盈亏` 只反映真实执行结果


### 第四阶段：小额实盘参数模板

目标：形成一个可长期复用的实盘起步模板。

建议初始模板：

- 总资金：`200 USDC`
- 单笔默认：`5 USDC`
- 单笔上限：`10 USDC`
- 单市场上限：`25 USDC`
- 总敞口上限：`100 USDC`
- 日亏损上限：`10 USDC`

当前仓库已增加第一道实盘闸门：

- `ARB_DRY_RUN=false`
- `LIVE_TRADING_ACK=true`
- `PORTFOLIO_SYNC_ENABLED=true`（除非显式设置 `LIVE_REQUIRE_PORTFOLIO_SYNC=false`）
- `POLYMARKET_TAKER_FEE_RATE>0`（除非显式设置 `LIVE_ALLOW_ZERO_TAKER_FEE=true`）
- `ARB_MAX_ORDER_SIZE_USDC<=LIVE_MAX_ORDER_SIZE_USDC`
- `RISK_MAX_TOTAL_EXPOSURE<=LIVE_MAX_TOTAL_EXPOSURE_USDC`
- `LIVE_MIN_NET_EDGE_BPS` / `LIVE_MIN_NET_EDGE_USD`：实盘扣费后仍需保留滑点与延迟安全垫
- `LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC` / `LIVE_MIN_WS_HIT_RATIO`：实盘行情健康闸门，WS 过旧或命中率过低时拒单

首笔真钱验证建议使用 `polymarket_only_canary_10usd.env.example` 作为模板。该模板默认：

- 单笔上限 `1.5 USDC`
- 总敞口上限 `3 USDC`
- `MAKER_STRATEGY_ENABLED=false`
- `POLYMARKET_TAKER_FEE_RATE=0.072`
- `LIVE_MIN_NET_EDGE_BPS=35`
- `LIVE_MIN_NET_EDGE_USD=0.0035`
- `LIVE_MAX_ORDERBOOK_SNAPSHOT_AGE_SEC=1`
- `LIVE_MIN_WS_HIT_RATIO=0.35`
- `POLYMARKET_CLOB_CLIENT_VERSION=auto`
- CLOB V2/HTTP 传输错误仍需在 dry-run 与 1 USDC canary 中继续观察

这样先验证余额、签名、下单、失败处理、账户同步和通知链路，再考虑打开 T3 做市。

升级条件：

- 连续 3 天无异常执行错误
- WebSocket 稳定
- 真实成交统计与面板一致
- 扣除手续费后收益仍为正


## 7. 后续具体修改任务

后续应优先修改以下模块。

- `polymarket_arb/websocket_feed.py`
- `polymarket_arb/execution_engine.py`
- `polymarket_arb/main_loop.py`
- `polymarket_arb/risk_manager.py`
- `polymarket_arb/dashboard_api.py`
- `polymarket_arb/dashboard.html`
- `polymarket_arb/models.py`
- `.env.example`


## 8. 明确不在第一轮做的事情

为避免任务失控，第一轮不做以下改造：

- 大规模策略重写
- 引入新的交易所或新的跨平台套利逻辑
- 大改 AI 策略层
- 放大资金到 `500+ USDC` 的长期运行方案


## 9. 建议的下一步

下一步直接进入代码修改，按以下顺序执行：

1. 修复 WebSocket 解析和断连恢复。
2. 调整面板口径，显式区分“机会”和“真实执行”。
3. 增加余额检查与小额实盘保护。
4. 生成一份 `200 USDC` 小额实盘配置模板。


## 10. 本文档使用原则

从这一轮开始，后续修改、讨论、验收都以这份文档为准。

如果后面发现新问题，先更新本文档，再继续改代码。
