# Polymarket 套利机器人 — 配置文档

## 目录

- [前置条件](#前置条件)
- [快速开始](#快速开始)
- [API 体系与认证](#api-体系与认证)
- [配置参数详解](#配置参数详解)
  - [钱包与认证](#钱包与认证)
  - [API 端点](#api-端点)
  - [套利参数](#套利参数)
  - [风险管理](#风险管理)
  - [Telegram 通知](#telegram-通知)
  - [波动率估算](#波动率估算)
  - [Edge 引擎](#edge-引擎)
  - [Tick 录制](#tick-录制)
  - [监控仪表盘](#监控仪表盘)
  - [日志](#日志)
- [认证流程详解](#认证流程详解)
- [WebSocket 连接](#websocket-连接)
- [运行模式](#运行模式)
- [调参建议](#调参建议)
- [常见问题](#常见问题)
- [API 参考链接](#api-参考链接)

---

## 前置条件

1. **Python 3.11+**
2. **Polygon 链钱包**：需要私钥和代理钱包地址
3. **Polymarket 账户**：在 [polymarket.com](https://polymarket.com) 登录一次，系统会自动部署 Gnosis Safe 代理钱包
4. **USDC.e 余额**：代理钱包中需要有 USDC.e（Polygon 上的 USDC）用于交易
5. **网络环境**：Polymarket 有[地理封锁](https://docs.polymarket.com/api-reference/geoblock)，部分地区（含美国）无法交易

## 快速开始

```bash
# 1. 创建虚拟环境
cd PolymarketBot
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 填写 PRIVATE_KEY 和 POLYMARKET_FUNDER

# 4. 运行（默认 dry run 模式，只扫描不交易）
python run_arb_bot.py
```

---

## API 体系与认证

Polymarket 由三套独立 API 组成：

| API | 端点 | 用途 | 认证要求 |
|-----|------|------|----------|
| **Gamma API** | `https://gamma-api.polymarket.com` | 市场发现、事件列表、搜索、标签、评论 | 无 |
| **Data API** | `https://data-api.polymarket.com` | 持仓、交易记录、排行榜、open interest | 无 |
| **CLOB API** | `https://clob.polymarket.com` | 订单簿、价格、下单/撤单/心跳 | 读端点无需认证；交易端点需 L2 认证 |

本机器人使用的 API 分布：

| 模块 | 使用的 API | 端点 |
|------|-----------|------|
| `market_scanner.py` | Gamma API | `GET /markets`, `GET /events` |
| `orderbook_analyzer.py` | CLOB API（只读） | `GET /book`, `GET /midpoint` |
| `websocket_feed.py` | CLOB WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| `execution_engine.py` | CLOB API（交易） | `POST /order`, `DELETE /order/{id}` |
| `client_factory.py` | CLOB API（L1/L2） | `POST /auth/api-key`, `GET /auth/derive-api-key` |

---

## 配置参数详解

所有配置通过环境变量管理，从 `.env` 文件加载。配置类 `ArbConfig`（`config.py`）使用 frozen dataclass 保证运行时不可变。

### 钱包与认证

| 环境变量 | 类型 | 默认值 | 必填 | 说明 |
|----------|------|--------|------|------|
| `PRIVATE_KEY` | str | — | **是** | 钱包私钥（hex 格式，`0x` 开头）。也可用 `POLYMARKET_PRIVATE_KEY` |
| `POLYMARKET_FUNDER` | str | — | **是** | 代理钱包地址（在 [polymarket.com/settings](https://polymarket.com/settings) 查看） |
| `POLYMARKET_SIGNATURE_TYPE` | int | `2` | 否 | 签名类型，见下表 |
| `CHAIN_ID` | int | `137` | 否 | 链 ID，137 = Polygon 主网 |

**签名类型对照表：**

| 值 | 类型 | 适用场景 |
|----|------|----------|
| `0` | EOA | MetaMask 等标准钱包。需要 POL 付 gas |
| `1` | POLY_PROXY | Magic Link（邮箱/Google）登录用户。需从 Polymarket.com 导出私钥 |
| `2` | GNOSIS_SAFE | Gnosis Safe 代理钱包。**绝大多数用户使用此类型** |

### API 端点

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `CLOB_HOST` | str | `https://clob.polymarket.com` | CLOB API 主端点 |
| `GAMMA_HOST` | str | `https://gamma-api.polymarket.com` | Gamma API 端点 |

一般无需修改，除非 Polymarket 更换了域名或你使用代理。

### 套利参数

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `ARB_MIN_EDGE_USD` | float | `0.005` | 最小净利润（USD），低于此值不执行 |
| `ARB_MIN_EDGE_PCT` | float | `0.3` | 最小利润率（%），低于此值不执行 |
| `ARB_MAX_ORDER_SIZE_USDC` | float | `50.0` | 单笔最大下单量（USDC） |
| `ARB_DEFAULT_ORDER_SIZE_USDC` | float | `10.0` | 默认下单量（USDC） |
| `ARB_SCAN_INTERVAL_SEC` | float | `5.0` | 主循环扫描间隔（秒）。越小越灵敏，但 API 调用越频繁 |
| `ARB_MARKET_FETCH_LIMIT` | int | `100` | Gamma API 拉取市场的批量大小 |
| `ARB_DRY_RUN` | bool | `true` | `true` = 只扫描不交易。**强烈建议初次运行使用** |
| `ARB_MIN_LIQUIDITY` | float | `1000` | 最小市场流动性（USDC），低于此值的市场被跳过 |
| `ARB_MIN_VOLUME_24H` | float | `500` | 最小 24h 交易量（USDC），低于此值的市场被跳过 |

**利润计算公式（T0 结构性套利）：**

```
二元市场: profit = $1.00 - ask_yes - ask_no - taker_fee(2%)
多结果:   profit = $1.00 - Σ(ask_i) - taker_fee(2%)
```

### 风险管理

`RiskManager` 在每笔交易前执行预检查，任一规则触发即拒绝交易。

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `RISK_MAX_OPEN_POSITIONS` | int | `10` | 最大同时持仓数量，超出后不开新仓 |
| `RISK_MAX_EXPOSURE_PER_MARKET` | float | `100.0` | 单市场最大敞口（USDC）。按 `condition_id` 计算 |
| `RISK_MAX_TOTAL_EXPOSURE` | float | `500.0` | 全局最大敞口（USDC）。所有持仓总成本上限 |
| `RISK_MAX_DAILY_LOSS` | float | `50.0` | 日亏损止损线（USDC）。触发后暂停全部交易，UTC 0:00 重置 |
| `RISK_MAX_CONSECUTIVE_FAILURES` | int | `5` | 连续执行失败次数。达到上限触发熔断，需手动解除 |

**额外的内置风控规则（不可配置）：**

| 规则 | 值 | 说明 |
|------|-----|------|
| 市场冷却 | 60 秒 | 同一 `event_id` 60 秒内不重复执行 |
| 腿失败回滚 | 自动 | 多腿套利中任一腿失败，尝试撤销已提交的腿 |
| API 连续错误暂停 | 10 次后暂停 60s | 连续 10 次 API 请求异常后休眠 60 秒 |

### Telegram 通知

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `TELEGRAM_ENABLED` | bool | `false` | 是否启用 Telegram 通知 |
| `TELEGRAM_BOT_TOKEN` | str | — | Bot Token（从 [@BotFather](https://t.me/BotFather) 获取） |
| `TELEGRAM_CHAT_ID` | str | — | 目标聊天 ID（个人或群组） |
| `TELEGRAM_NOTIFY_ON_ARB_FOUND` | bool | `true` | 发现套利机会时通知 |
| `TELEGRAM_NOTIFY_ON_TRADE` | bool | `true` | 交易执行时通知（跳过冷却期，每笔必发） |
| `TELEGRAM_NOTIFY_ON_ERROR` | bool | `true` | 错误告警通知 |
| `TELEGRAM_NOTIFY_COOLDOWN_SEC` | float | `30` | 同类消息冷却时间（秒）。防止刷屏 |

**通知消息类别：**

| 类别 | 触发时机 | 遵守冷却 |
|------|----------|----------|
| `startup` | 机器人启动 | 否（force） |
| `shutdown` | 机器人停止 | 否（force） |
| `arb_found` | 发现套利机会 | 是 |
| `trade` | 交易执行完成 | 否（force） |
| `error` | 错误告警 | 是 |
| `status` | 定期状态报告（每 100 周期） | 是 |

### 波动率估算

`VolEstimator` 提供双时间尺度的波动率估算，驱动做市策略的 spread 和 Edge 引擎的置信度。

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `VOL_FAST_MINUTES` | int | `60` | 快速波动率窗口（分钟）。响应短期市场变化 |
| `VOL_SLOW_MINUTES` | int | `360` | 慢速波动率窗口（分钟）。捕捉长期波动趋势 |
| `VOL_MIN_BARS` | int | `20` | warmup 最少数据点。数据不足时返回 NaN |

**波动率融合公式：**

```
sigma_blend = 0.7 × sigma_fast + 0.3 × sigma_slow
```

### Edge 引擎

`EdgeEngine` 融合订单簿微观结构信号和波动率，输出方向性交易信号。

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `EDGE_MIN_BPS` | float | `100` | 最小 edge 阈值（基点）。低于此值不触发信号 |
| `EDGE_MAX_SPREAD_BPS` | float | `500` | 最大可接受 spread（基点）。超过则 veto |
| `EDGE_MIN_CONFIDENCE` | float | `0.4` | 最小置信度。低于此值不触发信号 |

**Edge 引擎信号源：**

| 信号 | 来源 | 权重 |
|------|------|------|
| 订单簿不平衡 (OBI) | `EnhancedBookStore` 5 档 bid/ask depth | 短期方向 |
| Microprice | 成交量加权中间价 | 短期方向 |
| 价格动量 | 短期趋势延续 | 趋势跟随 |
| 跨市场逻辑约束 | 事件间逻辑关系 | Veto check |
| 现货锚定 | UPDOWN 市场 z-score | 定价锚 |

**Veto 机制（任一触发即拒绝信号）：**

- spread > `EDGE_MAX_SPREAD_BPS`
- depth < `ARB_MIN_LIQUIDITY × 0.05`
- confidence < `EDGE_MIN_CONFIDENCE`

### Tick 录制

`TickRecorder` 将 WebSocket 推送的订单簿数据写入 NDJSON 文件，用于回测。

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `TICK_RECORD_ENABLED` | bool | `false` | 是否开启录制 |
| `TICK_RECORD_DIR` | str | `data/ticks` | 输出目录（自动创建） |

**文件格式：**

- 文件名：`{UTC日期}.ndjson`，如 `2026-04-12.ndjson`
- 单文件超过 200MB 自动滚动：`2026-04-12.1.ndjson`
- 每行一个 JSON 对象，包含 `ts_ms`, `token_id`, `best_bid`, `best_ask`, `microprice`, `spread_bps`, `bids_top3`, `asks_top3` 等字段

### 监控仪表盘

FastAPI 后端 + HTML 前端，只读访问，绑定 `127.0.0.1`。

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `DASHBOARD_ENABLED` | bool | `true` | 是否启用仪表盘 |
| `DASHBOARD_PORT` | int | `8077` | HTTP 端口 |

**API 端点：**

| 路径 | 说明 |
|------|------|
| `GET /` | HTML 仪表盘页面 |
| `GET /api/status` | 完整状态快照 |
| `GET /api/opportunities` | 最近发现的套利机会 |
| `GET /api/trades` | 最近的交易记录 |
| `GET /api/pnl` | 盈亏曲线数据 |
| `GET /api/risk` | 风控状态 |
| `GET /api/strategies` | 策略状态 |
| `GET /api/positions` | 当前持仓 |
| `GET /api/volatility` | 波动率快照 |
| `GET /api/edge` | Edge 引擎最近决策 |
| `GET /api/book` | 订单簿摘要 |

### 日志

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `LOG_LEVEL` | str | `INFO` | 日志级别：`DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FILE` | str | `arb_bot.log` | 日志文件路径。控制台和文件双输出 |

---

## 认证流程详解

CLOB API 采用两层认证模型，`client_factory.py` 已封装了完整流程：

```
┌─────────────────────────────────────────────────────────┐
│                    L1 认证（私钥签名）                     │
│                                                         │
│  1. 用私钥签名 EIP-712 消息（ClobAuthDomain）             │
│  2. 调用 POST /auth/api-key 或 GET /auth/derive-api-key │
│  3. 获得 apiKey + secret + passphrase                    │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│                    L2 认证（API Key）                     │
│                                                         │
│  每个请求附带 5 个 HTTP Header：                          │
│    POLY_ADDRESS    = 钱包地址                             │
│    POLY_SIGNATURE  = HMAC-SHA256(secret, request_body)  │
│    POLY_TIMESTAMP  = 当前 UNIX 时间戳                     │
│    POLY_API_KEY    = apiKey                               │
│    POLY_PASSPHRASE = passphrase                          │
└─────────────────────────────────────────────────────────┘
```

**在本机器人中的调用路径：**

```python
# client_factory.py

# 只读客户端 — 无需任何认证
ClobClient(host, chain_id=137)

# 交易客户端 — L1 派生凭证 + L2 初始化
temp = ClobClient(host, key=PRIVATE_KEY, chain_id=137)
creds = temp.derive_api_key()  # L1 → 获取 apiKey/secret/passphrase
client = ClobClient(host, key=PRIVATE_KEY, chain_id=137,
                    creds=creds, signature_type=2, funder=FUNDER)
```

---

## WebSocket 连接

WebSocket 端点：`wss://ws-subscriptions-clob.polymarket.com/ws/market`

**订阅格式：**

```json
{
  "type": "subscribe",
  "channel": "market",
  "assets_ids": ["<token_id>"]
}
```

**消息类型：**

| type | 说明 | 处理方式 |
|------|------|----------|
| `book` | 完整订单簿快照 | `OrderBookMirror.apply_snapshot()` |
| `price_change` | 单价位增量更新 | `OrderBookMirror.apply_delta()` |
| `pong` | 心跳响应 | 忽略 |
| `subscribed` | 订阅确认 | 忽略 |

**重连策略：** 指数退避，初始 1 秒，最大 30 秒。

---

## 运行模式

### Dry Run（默认）

```bash
# .env
ARB_DRY_RUN=true
```

- 只读 CLOB 客户端，不派生 API 凭证
- 正常扫描市场、检测套利、计算 Kelly 仓位
- 交易结果全部标记为 `FILLED`（模拟成功）
- Dashboard、Telegram、Tick 录制均正常工作

### Live（实盘）

```bash
# .env
ARB_DRY_RUN=false
```

- 派生 API 凭证，构建交易客户端
- 实际提交订单到 CLOB
- 多腿失败自动回滚
- 确保风控参数已正确配置

---

## 调参建议

### 保守配置（新手/小资金）

```env
ARB_DRY_RUN=true
ARB_MIN_EDGE_USD=0.01
ARB_MIN_EDGE_PCT=0.5
ARB_MAX_ORDER_SIZE_USDC=20.0
ARB_DEFAULT_ORDER_SIZE_USDC=5.0
RISK_MAX_TOTAL_EXPOSURE=200.0
RISK_MAX_DAILY_LOSS=20.0
RISK_MAX_CONSECUTIVE_FAILURES=3
EDGE_MIN_BPS=150
```

### 激进配置（有经验/大资金）

```env
ARB_DRY_RUN=false
ARB_MIN_EDGE_USD=0.003
ARB_MIN_EDGE_PCT=0.2
ARB_MAX_ORDER_SIZE_USDC=100.0
ARB_DEFAULT_ORDER_SIZE_USDC=25.0
ARB_SCAN_INTERVAL_SEC=3
RISK_MAX_TOTAL_EXPOSURE=2000.0
RISK_MAX_DAILY_LOSS=200.0
EDGE_MIN_BPS=80
EDGE_MAX_SPREAD_BPS=800
```

### 纯数据采集配置

```env
ARB_DRY_RUN=true
TICK_RECORD_ENABLED=true
TICK_RECORD_DIR=data/ticks
DASHBOARD_ENABLED=true
ARB_SCAN_INTERVAL_SEC=10
```

---

## 常见问题

### Q: `assert "必须设置 PRIVATE_KEY"` 报错

确保 `.env` 文件中 `PRIVATE_KEY` 已填写，且以 `0x` 开头。也支持 `POLYMARKET_PRIVATE_KEY`。

### Q: `assert "必须设置 POLYMARKET_FUNDER"` 报错

你需要先在 [polymarket.com](https://polymarket.com) 登录一次，系统会自动部署代理钱包。然后在 Settings 页面找到钱包地址，填入 `POLYMARKET_FUNDER`。

### Q: 签名类型怎么选？

- 全新的 Polymarket 用户 → `2`（Gnosis Safe）
- 用邮箱/Google 登录的老用户 → `1`（Poly Proxy）
- 用 MetaMask 直连的用户 → `0`（EOA）

### Q: WebSocket 不断重连

检查网络连接和地理位置。Polymarket WebSocket 在部分地区不可用。重连是正常的降级行为，指数退避最大 30 秒。

### Q: API 返回 429 或频率限制

降低扫描频率：增大 `ARB_SCAN_INTERVAL_SEC`，减小 `ARB_MARKET_FETCH_LIMIT`。参考 [Rate Limits](https://docs.polymarket.com/api-reference/rate-limits) 文档。

### Q: Dry Run 模式下看不到套利机会

正常现象。Polymarket 有专业做市商（如 Wintermute）在毫秒级修正价差，结构性套利机会极其稀缺。建议关注 T2 统计套利信号。

---

## API 参考链接

| 文档 | URL |
|------|-----|
| 总览 | https://docs.polymarket.com/api-reference/introduction |
| 认证 | https://docs.polymarket.com/api-reference/authentication |
| SDK | https://docs.polymarket.com/api-reference/clients-sdks |
| 地理限制 | https://docs.polymarket.com/api-reference/geoblock |
| 频率限制 | https://docs.polymarket.com/api-reference/rate-limits |
| 交易费率 | https://docs.polymarket.com/trading/fees |
| 下单 | https://docs.polymarket.com/trading/orders/create |
| WebSocket Market | https://docs.polymarket.com/market-data/websocket/market-channel |
| WebSocket User | https://docs.polymarket.com/market-data/websocket/user-channel |
| Negative Risk | https://docs.polymarket.com/advanced/neg-risk |
| 做市入门 | https://docs.polymarket.com/market-makers/getting-started |
| 流动性奖励 | https://docs.polymarket.com/market-makers/liquidity-rewards |
| Maker 返佣 | https://docs.polymarket.com/market-makers/maker-rebates |
| 错误码 | https://docs.polymarket.com/resources/error-codes |
| 合约地址 | https://docs.polymarket.com/resources/contract-addresses |
| Python SDK 源码 | https://github.com/Polymarket/py-clob-client |
| TypeScript SDK | https://github.com/Polymarket/clob-client |
| Rust SDK | https://github.com/Polymarket/rs-clob-client |
| OpenAPI Spec (Gamma) | https://docs.polymarket.com/api-spec/gamma-openapi.yaml |
| OpenAPI Spec (CLOB) | https://docs.polymarket.com/api-spec/clob-openapi.yaml |
| OpenAPI Spec (Data) | https://docs.polymarket.com/api-spec/data-openapi.yaml |
