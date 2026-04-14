# 服务器观测手册

适用场景：机器人长期运行在服务器上，重点关注“有没有稳定跑”“有没有跑偏”“有没有产生真实有效信号”。

## 先看什么

优先看 `arb_bot.log` 的这几类行：

- `Polymarket 套利机器人启动`
- `实例: run_id=... pid=...`
- `状态: 已扫描 ... 周期`
- `WebSocket 已连接`
- `扫描周期 #... 异常`
- `token=... 暂无 orderbook，进入 ...s 冷却`
- `机器人已停止: run_id=...`

如果一个 `run_id` 有启动、没有停止，通常说明：

- 进程被外部杀掉
- 机器重启
- 程序异常退出但来不及走优雅停机

## 推荐检查顺序

1. 先确认进程还活着。

```powershell
Get-Process python
```

2. 再看最近 100 行日志有没有持续心跳。

```powershell
Get-Content arb_bot.log | Select-Object -Last 100
```

3. 确认最近是否还在推进周期计数。

```powershell
Select-String -Path arb_bot.log -Pattern "状态: 已扫描" | Select-Object -Last 5
```

4. 确认最近一次启动的 `run_id`。

```powershell
Select-String -Path arb_bot.log -Pattern "实例: run_id=" | Select-Object -Last 5
```

5. 核对 telemetry 是否同时存在 `startup` / `cycle_summary` / `shutdown`。

```powershell
Get-ChildItem data/telemetry
Get-Content data/telemetry/*.risk_events.ndjson | Select-Object -Last 20
```

## 看到这些通常是正常的

- 偶发 `get_order_book 失败，准备重试`
- 零星网络请求失败后自恢复
- `token=... 暂无 orderbook，进入 300s 冷却`
- 长时间 `发现 0 机会`

前提是：

- 主循环状态日志还在持续推进
- WebSocket 没有持续断开
- 错误没有演变成 `扫描周期 #... 异常`

## 这些现象要重点排查

- 同一个时间段重复出现多次 `Polymarket 套利机器人启动`，但没有对应 `机器人已停止`
- `状态: 已扫描` 长时间不再更新
- `扫描周期 #... 异常` 连续出现
- `WebSocket 断开` 持续反复出现
- `run_id` 频繁变化
- telemetry 只有 `startup` 没有后续 `cycle_summary`

## 当前这版观测约定

- 请求级 `httpx` 日志默认压到 `WARNING`
- `No orderbook exists` 会对单个 token 冷却 `ORDERBOOK_MISSING_COOLDOWN_SEC`
- oversized 多腿事件只记 `DEBUG`
- focus 关键字采用词切分匹配，不会再把 `eth` 误匹配到 `Netherlands`

## 推荐参数组合

建议直接从 [.env.example](/abs/e:/AppProject/PolymarketBot/.env.example) 起步。当前统一模板已经合并了原 `.env.server-observe.example` 的说明，偏向：

- 先观察 T0/T2/T3 信号质量，不急着启用 T1
- 先聚焦 crypto 主题，而不是一开始就全市场铺开
- 先保守控制风险和日志噪音，让复盘更清晰

如果你想做两组对照，建议这样跑：

1. 第一组：保持模板默认值，聚焦 crypto
2. 第二组：只把 `ARB_MARKET_FOCUS_KEYWORDS=` 置空，做全市场对照

这样更容易判断“没有信号”到底是主题过滤问题，还是策略本身的问题。

## 建议保留的数据

- `arb_bot.log`
- `data/telemetry/*.ndjson`
- `data/ticks/*.ndjson`

如果要复盘“为什么没机会”，优先看 telemetry 和 ticks，而不是只盯主日志。
