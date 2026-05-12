# Polymarket Bot 优化路线图

> 文档目的：基于现状（bot能下单但未盈利，$10测试亏$2），梳理可执行的优化方向和验证流程，避免凭感觉调整策略。

---

## 一、现状盘点

### 已经具备的优势

| 维度 | 当前配置 | 评级 |
|---|---|---|
| 地理位置 | 都柏林机房（Polymarket同区） | 顶级 |
| 网络延迟 | 几十毫秒到CLOB API | 接近专业 |
| 数据接收 | WebSocket 实时流 | 正确选择 |
| 钱包架构 | MetaMask Safe wallet，自掌握私钥 | 干净 |
| Bot状态 | 能稳定下单，签名+认证已打通 | 工程通关 |
| 策略框架 | T0 套利 + T2 概率模型 + T3 做市 | 思路完整 |

### 当前的核心问题

**不是策略不行，是缺乏数据驱动的反馈循环**。

- `$10` 本金 + `$2` 亏损 = 完全在统计噪声范围内，**无法判断策略好坏**
- 没有详细的交易日志，**无法定位亏损的具体来源**
- 没有 shadow mode 测试，**策略真实期望未知**
- 把"工程测试金"当"策略验证金"用，**测试方法本身错位**

---

## 二、为什么 \$10 测试得不出结论

### 样本量问题

判断一个量化策略的真实期望是正是负，统计学要求 **至少 500-1000 笔独立交易**。

- `$10` 本金最多支撑几十到几百笔成交
- 这个区间内，盈亏完全可能由随机性决定
- 几笔不利成交（gas 费、滑点、随机方向）就能造成 `$2` 偏差

### 固定成本占比过高

```
$10 本金场景：
- 单笔 gas + 手续费：约 $0.01 - $0.05
- 单笔预期 spread 收入：$0.02 - $0.05
- 固定成本占比：30% - 100%
```

固定成本占比太高时，**根本测不出策略本身是否赚钱**。只有当资金量上到 `$500+`，每笔预期净利远大于固定成本，统计信号才有意义。

### 结论

**`$10` 的正确用途**：验证 bot 工程稳定性（能下单、能撤单、能处理异常）。  
**`$10` 不能验证的**：策略是否盈利、参数是否合理、市场是否选对。

---

## 三、分阶段优化路线

### 阶段 0：工程稳定性验证（当前阶段，1-2 周）

**目标**：确认 bot 能 7×24 稳定运行，不是验证盈亏。

#### 必须达到的指标

- WebSocket 断线次数：`< 5 次/天`
- 重连后状态恢复正常率：`100%`
- 订单提交成功率：`> 95%`
- 撤单成功率：`> 99%`
- 内存占用 24 小时稳定不增长
- 异常崩溃次数：`0`

#### 必须实现的功能

**1. WebSocket 心跳和重连**

```python
# 每秒检查最后一条消息时间
async def heartbeat_check():
    while True:
        if time.time() - last_message_time > 5:
            logger.warning("WebSocket stalled, reconnecting")
            await reconnect()
        await asyncio.sleep(1)

async def reconnect():
    await ws.close()
    await connect()
    await resubscribe_all()
    await fetch_snapshots()  # 关键：拉取所有市场当前 snapshot
```

**2. 消息序列号检查**

```python
class MarketState:
    sequence = 0
    
    def on_update(self, msg):
        if msg.seq != self.sequence + 1:
            logger.error(f"Sequence gap: expected {self.sequence + 1}, got {msg.seq}")
            self.resync_via_rest()
        self.sequence = msg.seq
```

如果检测到序列号丢失，立即用 REST API 拉取 snapshot 修正内存状态，否则会基于过期数据下错单。

**3. 完整的交易日志**

每笔成交必须记录：

```python
{
    "timestamp": "2026-05-12T...",
    "market_id": "...",
    "side": "BUY/SELL",
    "price": 0.4800,
    "size": 50,
    "order_type": "GTC",
    "is_maker": True,
    "fee": -0.02,
    "slippage": 0.001,
    "intended_price": 0.4799,
    "decision_context": {
        "best_bid_at_decision": 0.4799,
        "best_ask_at_decision": 0.4801,
        "my_inventory_before": 30,
        "model_signal": "...",
    },
    "result": {
        "filled_size": 50,
        "avg_fill_price": 0.4800,
        "realized_pnl": 0.15
    }
}
```

**没有这些数据，所有后续优化都是瞎猜**。

**4. 监控告警**

最简方案：
- Bot 把关键指标写入 SQLite/Redis
- 用 Streamlit 或 Grafana 做简单面板
- 异常时 Telegram bot 推送告警

监控的关键指标：
- 当前在线状态
- 最近 1 小时成交笔数
- 当日累计盈亏
- 各市场库存敞口
- WebSocket 健康度

#### 完成本阶段的判断标准

bot 在 \$10 测试金下连续运行 72 小时，上述所有指标达标。**此时不要看盈亏数字**，盈亏在此阶段没有诊断价值。

---

### 阶段 1：Shadow Mode 数据积累（1-2 周）

**目标**：在不实盘下单的情况下，积累 1000+ 模拟成交，得出策略真实期望。

#### 实现方式

```python
SHADOW_MODE = True

async def place_order(order):
    if SHADOW_MODE:
        return log_virtual_order(order)
    return await real_place_order(order)

def log_virtual_order(order):
    """记录虚拟订单，后台模拟撮合"""
    virtual_orders[order.id] = {
        **order.dict(),
        "placed_at": now(),
        "status": "OPEN"
    }
    return order.id

async def virtual_matching_engine():
    """监听真实订单簿变化，模拟自己的虚拟单是否会成交"""
    while True:
        for order in virtual_orders.values():
            if order.status == "OPEN":
                if would_be_filled(order, current_orderbook):
                    order.status = "FILLED"
                    record_virtual_fill(order)
        await asyncio.sleep(0.1)
```

#### Shadow Mode 要回答的问题

1. **策略期望值**：1000 笔模拟成交后，总盈亏是正是负？
2. **市场分布**：哪些市场友好（净盈利），哪些市场亏钱？
3. **时段分布**：哪些时段成交多、利润好？
4. **maker/taker 比例**：你的挂单实际能否成为 maker？
5. **撤单频率**：每个挂单平均存活多久？

#### Shadow Mode 的局限性

模拟撮合假设你的挂单不影响市场，但实盘中你的挂单会：
- 改变订单簿深度
- 可能被知情交易者反向选择
- 影响别人的报价策略

所以 shadow 结果是**乐观估计**，实盘结果会比 shadow 差 10-30%。但**方向性判断是可靠的**——如果 shadow 都亏钱，实盘必定亏更多；shadow 微赚，实盘可能亏。

---

### 阶段 2：小额实盘验证（2-3 周，\$200-500）

**目标**：验证 shadow 到实盘的 gap，确认策略真实可行。

#### 资金量选择理由

`$200-500` 这个区间的特点：
- 固定成本占比降到 5-10%（合理水平）
- 单个市场可以分配 `$30-50` 实盘资金
- 能在 1-2 周积累几百笔实盘成交
- 即使全亏，损失可承受

#### 实盘要做的对比分析

每天对比：

| 指标 | Shadow 预期 | 实盘实际 | Gap |
|---|---|---|---|
| 成交笔数 | 80 | 65 | -19% |
| 总 spread 收入 | $2.40 | $1.80 | -25% |
| Maker 比例 | 95% | 78% | -18% |
| 平均滑点 | 0.001 | 0.003 | +200% |

Gap 揭示的问题：
- **成交笔数减少**：你的挂单被知情交易者跳过了，他们只吃别人的单
- **Maker 比例下降**：你的限价单在等成交期间被快速移动的价格甩开，被迫撤单重挂时变成 taker
- **滑点增加**：实盘成交回报有微小延迟，价格已经变动

#### 实盘要排除的工程问题

如果实盘表现严重偏离 shadow，先排除工程问题：
- 订单类型对不对（GTC vs FOK，确认是 maker）
- 撤单/重挂逻辑有没有 bug
- 价格精度有没有问题
- 是否撞上了 Liquidity Rewards 的规则限制

---

### 阶段 3：策略层优化（持续）

阶段 2 跑通后才进入。**没跑通阶段 1-2 之前不要做这些**。

#### T3 做市优化方向

**1. 动态 spread**

```python
def calculate_spread(market_state):
    base_spread = 0.02  # 基础 2¢
    
    # 波动率高时加宽
    volatility = market_state.recent_volatility(window=60)
    spread = base_spread * (1 + volatility * 2)
    
    # 库存偏离零时，向有利方向倾斜
    inventory_skew = market_state.my_inventory / max_inventory
    bid_offset = -inventory_skew * 0.01
    ask_offset = -inventory_skew * 0.01
    
    return spread, bid_offset, ask_offset
```

**2. 跨市场库存对冲**

识别相关市场（同事件不同表述），用相关市场的反向持仓对冲：

```
A 市场：「Trump 2024 当选」YES = +500 股
B 市场：「Trump 在某州赢」YES = -500 股
净 delta：接近零，只赚 spread
```

需要先建立市场关联图谱。

**3. 申请 Liquidity Rewards Program**

`https://docs.polymarket.com/market-makers/liquidity-rewards`

满足条件的做市商获得 POL token 奖励，奖励通常占总收入的 50-70%。要求：
- 双边挂单
- spread 在规定范围内
- 95%+ uptime
- 最小 depth

**没有 rewards 的做市策略经济性差很多，这是核心补贴**。

**4. 选对市场**

适合做市的市场特征：
- 日成交量 `$10k - $100k`
- 到期时间 1 周 - 3 个月
- 没有明显内幕信息（避开政治、地缘）
- Resolution 条件清晰

不要碰：
- 体育市场临赛前（专业博彩 pricing 压制）
- 政治市场临选举前（内幕信息泛滥）
- 超小流动性市场（你独扛库存）

#### T0 套利优化方向

**1. 提高单笔阈值**

当前 `$0.13` 预期净利已经被滑点+gas 吃光。把阈值提到 `$1.0+`：

```python
MIN_NET_PROFIT_THRESHOLD = 1.0  # 美元

if predicted_profit < MIN_NET_PROFIT_THRESHOLD:
    return  # 跳过这次机会
```

**2. 用 maker 单**

套利不一定要 taker。挂在 best bid/ask 内侧的限价单，等几秒可能就成交，既赚价差又赚 maker 返佣。

**3. 加诱饵单滤镜**

```python
def is_real_opportunity(opportunity):
    # 价差必须持续多个 tick
    if opportunity.duration_ms < 200:
        return False
    
    # 深度必须足够
    if opportunity.depth < min_depth:
        return False
    
    # 检查是不是同一个地址挂的（疑似洗盘）
    if opportunity.maker_address in suspicious_list:
        return False
    
    return True
```

#### T2 概率模型优化方向

**T2 最难做好，建议长期保持 shadow 模式**。

如果一定要实盘：

**1. 不要让 LLM 直接给概率**

LLM 在二元事件的概率估计上系统性偏差严重。改用 LLM 做**结构化分类**：

```python
llm_output = {
    "event_type": "policy_announcement",
    "affected_market_ids": ["market_xxx"],
    "direction": "yes" | "no" | "neutral",
    "confidence": "high" | "medium" | "low",  # 不是数字概率
    "uncertainty_factors": ["..."],
    "time_horizon": "hours" | "days" | "weeks"
}
```

**2. 决策用规则代码，不要让 LLM 决定**

```python
if (llm_output.confidence == "high" 
    and llm_output.direction != market_consensus()
    and price_discrepancy > 0.05
    and not in_blackout_period(llm_output.event_type)):
    place_order(...)
```

**3. 信源差异化**

避开人人都用的 Twitter/Reuters。寻找：
- 政府公告 RSS（FDA、CFTC、央行）
- 小众 Telegram 频道
- 链上数据（针对加密市场）
- 特定语言信源

**信源是 T2 唯一可能的护城河**。

#### 代码层优化方向

**1. JSON 解析提速**

```python
import orjson  # 比标准 json 快 3-5 倍

async def on_message(msg):
    data = orjson.loads(msg)
    ...
```

**2. 关键路径异步化**

```python
# 不要这样
await db.write(data)
await place_order(order)

# 而是
asyncio.create_task(db.write(data))  # fire-and-forget
asyncio.create_task(place_order(order))
```

**3. 预构建订单模板**

避免每次都重新构造订单对象：

```python
order_templates = {
    market_id: build_template(market_id) 
    for market_id in active_markets
}

def quick_place(market_id, side, price, size):
    template = order_templates[market_id].copy()
    template.update(side=side, price=price, size=size)
    return submit(template)
```

**4. 考虑关键路径 Rust 化**

Polymarket 的 SDK 有 Rust 版本（`polymarket_client_sdk_v2`）。如果延迟仍是瓶颈，把"接收 WebSocket → 决策 → 下单"这条关键路径改用 Rust 重写，能从几十毫秒压到几毫秒。

---

## 四、资金分配策略

### 当前阶段（阶段 0-1）

```
总本金：10 万元人民币（约 $14000）

分配：
├─ $13900 → 余额宝/逆回购（年化 2-3% 安全垫）
└─ $100  → bot 测试金（用于工程验证 + shadow mode）
```

### 阶段 2 完成后（实盘小额验证）

```
分配：
├─ $13500 → 余额宝/逆回购
└─ $500   → 实盘验证（T3 做市为主）
```

### 阶段 3 完成后（如果策略验证有效）

```
策略表现稳定（月化 > 2%、最大回撤 < 10%）：
├─ $10000 → 余额宝
└─ $4000  → 实盘运行

策略表现不稳定或亏损：
└─ 全部退回理财，停止实盘
```

### 永远不要做的

- 把 10 万一次性投入 bot
- 用任何形式的杠杆
- 跳过 shadow mode 直接实盘
- 亏损时加资金摊薄成本

---

## 五、关键诊断指标

bot 跑起来后，每天必须看这些指标。任何一个异常立即停机分析。

### 工程指标

| 指标 | 健康范围 | 异常处理 |
|---|---|---|
| WebSocket uptime | `> 99%` | 检查网络、API 限流 |
| 订单提交成功率 | `> 95%` | 检查签名、余额、精度 |
| 撤单成功率 | `> 99%` | 检查 nonce、订单状态同步 |
| 内存占用变化 | `< 5% / 天` | 检查内存泄漏 |
| 消息处理延迟 | `< 50ms` | 检查异步处理是否阻塞 |

### 策略指标

| 指标 | 健康范围 | 异常处理 |
|---|---|---|
| Maker 比例 | `> 80%` | 撤单/重挂逻辑、定价策略 |
| 平均滑点 | `< 0.002` | 价格预测、订单路由 |
| 单市场最大敞口 | `< 总资金 5%` | 风控阈值、库存对冲 |
| 当日最大回撤 | `< 3%` | 停机分析 |
| 日均成交笔数 | `> 50` | 市场选择、spread 设置 |

### 经济指标

| 指标 | 健康范围 | 含义 |
|---|---|---|
| 每笔净利（扣完成本）| `> $0.05` | 太低被噪声主导 |
| 月化收益率 | `> 2%` | 低于此值不如逆回购 |
| 夏普比率 | `> 1.0` | 风险调整后收益 |
| 最大回撤 | `< 15%` | 风控有效性 |

---

## 六、止损规则

明确的退出条件，不留模糊空间。

### 单笔止损

- 单笔交易亏损超过 `$5` 立即停止该市场做市
- 单市场净持仓达到资金 10% 立即停止单边挂单

### 单日止损

- 当日累计亏损超过 `2% 实盘资金`，bot 自动停机
- 停机后必须人工分析原因才能重启

### 策略止损

- shadow mode 跑满 2 周仍负期望 → 放弃该策略
- 实盘 4 周累计亏损超过 `5%` → 退回 shadow mode 重新调整
- 实盘 8 周累计亏损超过 `10%` → 完全放弃，资金回理财

### 时间止损

- 投入半年仍未跑通盈利 → 承认这条路对你不通，止损
- 不要因为"已经投入这么多时间"而陷入沉没成本陷阱

---

## 七、几个不要做的事

### 不要做的策略层操作

- **不要在亏损时加大仓位**（摊薄成本心理是散户量化的头号杀手）
- **不要在没有详细日志的情况下调参数**（你不知道为什么改）
- **不要同时调多个变量**（无法归因效果）
- **不要相信回测漂亮就一定实盘漂亮**（回测和实盘 gap 是常态）

### 不要做的工程层操作

- **不要在生产 bot 上直接改代码**（先在测试环境验证）
- **不要把私钥写死在代码里**（用环境变量）
- **不要把代码 push 到 public repo**（即使没有私钥，策略本身也是 alpha）
- **不要忽视 WebSocket 重连**（迟早出大问题）

### 不要做的资金层操作

- **不要 all-in**（10 万本金不要全投 bot）
- **不要用借款资金做量化**（杠杆放大亏损）
- **不要跨平台搬资金**（在一个平台稳定盈利后再扩展）

---

## 八、行动清单

### 本周必做

- [ ] 检查 bot 工程稳定性（重连、心跳、监控）
- [ ] 实现完整的交易日志记录
- [ ] 搭建最简监控面板
- [ ] 让 bot 在 \$10 测试金下连续运行 72 小时
- [ ] 不要再加钱进 bot

### 下周必做

- [ ] 实现 shadow mode 开关
- [ ] 启动 shadow mode 运行
- [ ] 每天检查 shadow 数据，分析市场分布
- [ ] 写一份简短日报：今日成交笔数、shadow 盈亏、异常事件

### 两周后评估

- [ ] Shadow mode 累计 500+ 模拟成交
- [ ] 分析：策略期望、最佳市场、问题点
- [ ] 决定：是否进入阶段 2 实盘 \$500

### 长期目标

- [ ] 找到至少 1 个稳定正期望的策略
- [ ] 建立完整的数据驱动反馈循环
- [ ] 月化 2-5% 稳定收益，最大回撤 < 10%
- [ ] 当 bot 稳定运行 3 个月后，考虑扩展到其他平台

---

## 九、心态管理

### 接受的事实

1. **量化是长期工程，不是短期赌博**：3-6 个月跑不出来很正常
2. **80% 的散户量化亏完本金**：你需要做对的事，不是别人都做的事
3. **数据 > 直觉**：每一个决策都应该有数据支持
4. **小步快跑 > 一次到位**：每周一个小改进，半年后回头看会很惊讶

### 拒绝的陷阱

1. **追求复杂**：简单的策略往往比复杂的稳定
2. **追求高频**：你不是 HFT 团队，慢一点没关系
3. **追求收益**：先追求"不亏"，再追求"盈利"
4. **追求确定**：量化没有确定，只有概率

---

## 附：当前 bot 配置检查清单

确认这些都对了：

```env
# 钱包
PRIVATE_KEY=0x...                  # 新 MetaMask 的 EOA 私钥，不是 Magic 旧的
POLYMARKET_FUNDER=0x...            # 新 Proxy Wallet 地址（Polymarket 充值按钮显示的那个）
POLYMARKET_SIGNATURE_TYPE=2        # MetaMask Safe 架构，值为 2
CHAIN_ID=137                       # Polygon 主网

# 端点
CLOB_HOST=https://clob.polymarket.com
GAMMA_HOST=https://gamma-api.polymarket.com

# WebSocket 必须订阅
SUBSCRIPTIONS=market,user          # market 看行情，user 看自己订单状态

# 订单参数（做市）
DEFAULT_ORDER_TYPE=GTC             # 必须是 GTC 才能当 maker
SPREAD_MIN=0.02                    # 最小 spread 2¢
MAKER_FEE_TARGET=true              # 优先 maker 单

# 风控
MAX_INVENTORY_PER_MARKET=50        # 单市场最大持仓（美元）
MAX_TOTAL_INVENTORY=200            # 总持仓上限
MAX_DAILY_LOSS=10                  # 日最大亏损（美元），触发停机
```

---

**文档版本**：v1.0  
**最后更新**：2026-05-12  
**适用阶段**：bot 已能下单，但未稳定盈利
