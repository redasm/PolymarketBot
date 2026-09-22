[English](../en/pending-validations.md) · [中文](../zh/pending-validations.md)

# 待验证清单

2026 年 5 月那次研究回顾里的若干优化项，是以**只跑影子模式**或**带着关闭的开关**合入的，
因为实现前的实证验证卡在拿不到数据上。这份文档是等数据可得之后，逐项完成验证的清单。

> 这些条目的状态是**未检验**，不是已验证。它和
> [research-findings.md](research-findings.md) 里那些"测了并且失败了"的策略是两种不同的状态。
> 这里没有任何一条是已知可行的。

每一项包含：

- **主张** —— 想验证的断言。
- **现状** —— bot 里目前实际在跑什么。
- **所需数据** —— 免费 API 拿不到的部分。
- **验证方法** —— 要算什么，以及晋级阈值。
- **脚本** —— 仓库里已有的可重跑入口。
- **要翻的开关** —— 主张验证通过后打开的环境变量。

---

## 1. 近确定性 92-98¢ 陷阱（Taleb / @stacyonchain）

- **主张**：成交价 ≥0.92 的 Polymarket 二元合约，实际结算为 YES 的比例系统性低于 92%，
  即群体在近确定性赌注上低估了尾部风险。
- **现状**：`NearCertaintyRule` 默认跑**影子模式**（`T2_NEAR_CERTAINTY_SHADOW_MODE=true`）。
  编排器会对每条方向性信号计算该规则"本来会做什么"，并在状态里以
  `meta.near_certainty.{would_apply_high, would_apply_longshot}` 暴露计数，但不修改生产的
  仓位/置信度。
- **所需数据**：
  - **已结算**二元市场的逐市场价格历史（需要每个市场生命周期内的 max(price)），配上结算结果
    （`outcomePrices`）。
  - **2026-05-22 实测的免费层障碍**：
    - `clob.polymarket.com/prices-history` → 对已关闭 token 返回空（只有活跃市场保留历史）。
    - `data-api.polymarket.com/trades` → 逐市场过滤被静默忽略，只返回全局流。
    - Goldsky `orderbook-subgraph/prod` → 重市场上 `orderBy: timestamp desc` 会
      statement-timeout；更老的市场未被索引。
  - **候选付费源**（待评估）：
    - Dune Analytics（已有 Polymarket dashboard）—— 约 $0 / $390 每月档；对已索引的成交事件
      跑 SQL。
    - Goldsky 付费层 —— 解决 statement-timeout。
    - Polymarket 官方分析团队（Discord 申请）—— 可能提供已结算市场的历史 OHLC。
- **验证方法**：
  1. 从 `gamma-api?closed=true` 拉已结算二元市场，配上 `outcomePrices`。
  2. 对每个市场，从付费源取生命周期内 max(YES price)。
  3. 分桶（0.92、0.95、0.97、0.98）：
     - cohort = max ≥ 该桶阈值的市场
     - 实现 YES 率 = (结算为 YES 的数量) / |cohort|
     - 对该比率算 95% Wilson 置信区间
  4. **晋级判据**：在 0.95 桶上，实现 YES 率必须 < 0.92，*且置信区间上界也 < 0.92*，
     cohort N ≥ 50。
  5. **拒绝判据**：比率 ≥ 0.92，或置信区间跨过 0.92 —— 说明 `NearCertaintyRule` 不代表真实
     edge，归档。
- **脚本**：`scripts/verify_near_certainty_trap.py` 已经搭好 gamma 侧拉取和 Wilson-CI 分桶，
  只差逐市场 max-price 的数据源。
- **要翻的开关**：`T2_NEAR_CERTAINTY_SHADOW_MODE=false`。阈值、乘数、置信度增量保持在验证过的
  取值（当前是 `T2_NEAR_CERTAINTY_{HIGH,LOW}_THRESHOLD`、
  `T2_NEAR_CERTAINTY_SIZE_MULTIPLIER`、`T2_NEAR_CERTAINTY_CONFIDENCE_DELTA`）。
- **本地数据兜底**：bot 的 `data/telemetry/*.strategy_signals.ndjson` 记录了每条 T2 方向性信号
  的 `market_prob` 和 `near_certainty` 块。运行 ≥3 个月后，可以把这些记录和 `gamma-api` 上的
  最终结算结果做 join，做一次局部验证。cohort 会更小（只有 bot 扫过的市场）且有偏（只有通过
  前置闸门的信号），但本地数据上方向一致的结果，是付费数据验证的强先验。

---

## 2. 链上 BTC 信号（MVRV / SOPR / ETF / 宏观）

- **主张**：MVRV Z-Score、SOPR 28 日均线、ETF 净流入、宏观流动性四个维度的共振可以预测 BTC
  方向。原文提议只在 4 个维度中 ≥3 个同向时才触发。
- **现状**：实现了免费层的子集 —— `CryptoMacroCollector` 把 Fear & Greed（alternative.me）
  作为一行 `research_signal` 暴露给任何含 crypto 关键词的主题。默认关闭：
  `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED=false`。完整四维共振**未实现**，因为其余三维需要
  付费数据。
- **所需数据**：
  - **MVRV Z-Score** —— Glassnode `/v1/metrics/market/mvrv_z_score`（付费，约 $30-40/月），
    或 CryptoQuant 免费层（1 天延迟）。
  - **SOPR 28d MA** —— Glassnode `/v1/metrics/indicators/sopr`（付费）或 CryptoQuant 免费层。
  - **ETF 净流入** —— SoSoValue / Coinglass 免费层限流约 60 req/min；可行但需要仔细缓存。
  - **宏观（M2 / Fed funds）** —— FRED API，带 key 免费。
  - **BTC 价格** —— CoinGecko / Binance 公开接口，免费。
- **验证方法**：
  1. 从 `gamma-api` 拉所有已结算的 BTC/ETH 价格类二元市场（"BTC 在 Y 日期前 > $X"），
     要求 ≥12 个月历史覆盖。用 `volume24hr > 1M` 过滤以保证 cohort 有意义（低成交量市场是噪声）。
  2. 对每个市场，在*入场日*（或市场首个活跃日）采样四维信号。
  3. 按结算结果给每个市场打标（YES = 1，NO = 0）。
  4. 拟合逻辑回归：`P(YES) ~ MVRV_z + SOPR_28d + ETF_5d + Fed_dovish + Fear_Greed`，
     训练/测试 80/20 切分。
  5. **晋级判据**：holdout AUC > 0.60，cohort N ≥ 80。
  6. **拒绝判据**：AUC ≤ 0.55，或系数符号与原文的方向性主张不一致 —— 归档该想法。
- **脚本**：`scripts/verify_onchain_signal_predictive_power.py` 目前是桩，订阅数据源后再扩展。
  该脚本有意与交易 bot 分离，只写
  `data/research/onchain_signal_verification.json`。
- **成功后要翻的开关**：
  - `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED=true`（本来就免费，可独立开启 —— 见下方说明）。
  - 一个新的 collector 把 MVRV/SOPR/ETF 作为额外行接入，待补，照 `CryptoMacroCollector` 的
    模式写。
- **本地数据兜底**：单独的 Fear & Greed 太弱，验证不了多维共振这个主张。但你仍然可以独立开启
  `RESEARCH_SIGNAL_CRYPTO_MACRO_ENABLED=true` —— 它只是给每个 crypto 主题加一行情绪数据，
  没有决策权。仲裁者是编排器已有的 research_overlay 共振打分。

---

## 3. 动态 ATR 等价止损（HyperLiquid）

- **主张**：把固定百分比止损换成 `k × ATR`，在 236 个 PineScript 策略上改善了 PnL；
  被"救回来"的策略都收敛到了这条规则。
- **现状**：实现为 `T2_STOP_LOSS_DYNAMIC_ENABLED`，默认**关闭**。退出管理器里已经接好了逐持仓
  滚动波动率追踪器，只是在开关翻开之前不用它来算止损。有效止损以
  `decisions[i].effective_stop_bps` 和 `vol_bps` 暴露在每行 `t2_exit_telemetry` 上。
- **所需数据**：**无需外部数据**。完全可以用 bot 自己产出的本地数据完成验证。
- **验证方法**：
  1. 以 `T2_STOP_LOSS_DYNAMIC_ENABLED=false` 跑 ≥2 周影子/dry-run（记录静态
     `T2_STOP_LOSS_BPS` 基线），并行跑一份 `T2_STOP_LOSS_DYNAMIC_ENABLED=true`
     （`shadow_t2_exit_manager` 已经接到同一个 provider）。
  2. 对每个已退出持仓，比较：
     - 逐持仓最大回撤
     - 过早止损次数（入场后 3× 评估间隔内就触发止损的持仓）
     - `effective_stop_bps` 相对静态 300 的分布
  3. **晋级判据**：动态配置在 N ≥ 50 对配对持仓上，把过早止损减少 ≥15%，且净 PnL 不劣化。
  4. **拒绝判据**：在同等规模 cohort 上净 PnL 退化，或 `vol_bps` 中位数与
     `T2_STOP_LOSS_BPS` 差得离谱（说明 k=2 选错了 —— 先重标 `T2_STOP_LOSS_DYNAMIC_K` 再翻开关）。
- **Telemetry**：`data/telemetry/*.t2_exit.ndjson`（或 `t2_exit_telemetry()` 落盘的位置）带着
  逐决策的 `effective_stop_bps` 和 `vol_bps`。与 `*.strategy_executions.ndjson` 配对得到
  入场/退出 PnL。
- **成功后要翻的开关**：`T2_STOP_LOSS_DYNAMIC_ENABLED=true`。根据观察到的波动率分布调
  `T2_STOP_LOSS_DYNAMIC_K`（默认 2.0）、`T2_STOP_LOSS_MIN_BPS`（100）、
  `T2_STOP_LOSS_MAX_BPS`（1000）。

---

## 4. 杠铃式资金分配（Taleb）

- **主张**：把 T2 资金切成约 80% 数据驱动 + 约 15% 尾部 + 约 5% 储备，并对尾部赌注放松仓位
  折扣（因为组合层面的集中度已被桶大小封顶），优于带统一尾部折扣的平坦 T2 分配。
- **现状**：实现为 `T2_BARBELL_ENABLED`，默认**关闭**。逐类敞口台账已经建好并在编排器状态的
  `meta.barbell` 下暴露。放松逻辑已接好但在关闭状态下不生效。
- **所需数据**：**无需外部数据**。与第 3 项一样用本地 telemetry。
- **验证方法**：
  1. 先以 `T2_BARBELL_ENABLED=false` 跑 ≥2 周基线，记录 `meta.barbell.exposure_usdc` 和
     `meta.tail_risk`。即使策略关闭，敞口台账仍会填充 —— 这是有意设计的，好让你看到"本来会
     发生什么"。
  2. 计算基线：
     - T2 PnL 按尾部风险类别的分布
     - 尾部类最大回撤
     - high_tail 桶里的连续亏损次数
  3. 翻到 `T2_BARBELL_ENABLED=true` 跑等长的一段。
  4. **晋级判据**：尾部类 PnL 波动下降且中位 PnL 不退化，同时尾部类回撤 ≤ 基线。
  5. **注意 —— 按 signal-ID 释放**：当前敞口台账在生产中会多记，因为
     `record_settlement` 没有透传 `signal_id`。在翻到实盘之前，需要把 `signal_id` 从
     `t2_exit_manager._release_orchestrator_exposure` → 编排器 →
     `_release_barbell_exposure(signal_id)` 打通。不修这个，尾部桶会比现实更快显示为满，
     放松逻辑就不再生效。钩子已经在 `_release_barbell_exposure(signal_id, amount)` 里，
     只差接上调用方。测试在 `tests/test_barbell_policy.py`。
- **Telemetry**：编排器状态里的 `meta.barbell.exposure_usdc.{data_driven, tail}`；
  每条信号 payload 上的 `barbell.{applied, bucket, multiplier_override}`。
- **成功后要翻的开关**：`T2_BARBELL_ENABLED=true`。根据观察到的尾部敞口利用率调
  `T2_BARBELL_TAIL_BUDGET_PCT`（默认 0.15 的 T2 分配）和
  `T2_BARBELL_TAIL_RELAXED_MULTIPLIER`（默认 0.85）。

---

## 5. 用 RTDS crypto_prices 作为 UPDOWN 结算口径

- **主张**：UPDOWN 市场按 Polymarket 自己的价格源结算，所以用 Binance 定价会引入一个 basis，
  而这个 basis 恰恰在 UPDOWN 最敏感（高波动）时最大。
- **现状**：`RtdsSpotFeed` + `CompositeSpotFeed` 以**影子模式**发布
  （`T2_UPDOWN_RTDS_MODE=shadow`）。定价仍用 Binance；RTDS 只每隔
  `T2_UPDOWN_BASIS_LOG_INTERVAL_SEC` 往 `risk_events` 里写一行 `updown_spot_basis`。
- **所需数据**：`crypto_prices` payload 的权威样本。订阅协议已确认
  （`{"action": "subscribe", "subscriptions": [{"topic", "type"}]}`，消息形如
  `{"topic", "type", "timestamp", "payload"}`），但 payload 的字段名未确认。因此
  `parse_rtds_crypto_payload` 对字段名做宽松匹配（`symbol`/`pair`/`asset`、
  `value`/`price`/`close` 等），读不出来的就丢弃而不是猜。
- **验证方法**：在 ≥24h 的 `updown_spot_basis` 行上，要求
  (a) 每个配置的 symbol 都有 RTDS tick 到达，且 `rtds_age_sec` 始终低于
  `T2_UPDOWN_RTDS_STALENESS_SEC`；(b) basis 分布中心接近 0 且没有无法解释的 regime 断裂。
  持续非零的 basis 意味着两个源在报不同的东西 —— 先查清楚，不要切。
- **脚本**：对 `data/telemetry/*.risk_events.ndjson` 跑
  `jq 'select(.event=="updown_spot_basis")'`。
- **要翻的开关**：`T2_UPDOWN_RTDS_MODE=primary`。

---

## 6. 钱包盈利质量阈值

- **主张**：按胜率 / 盈亏比 / 一致性 / 单笔集中度筛选被跟随的钱包，比按活跃度排序得到更好的
  跟单信号。
- **现状**：`strategies/wallet_quality.py` 以**关闭**状态发布（不传 `--quality-filter`）。
  离线 worker 无论如何都会为每个候选算完整画像，所以在任何阈值生效之前分布就是可观察的。
- **所需数据**：我们自己候选池上的 `/closed-positions` 指标分布。当前默认阈值
  （胜率 ≥ 0.60、盈亏比 ≥ 1.5、一致性 ≥ 0.70、最大单笔占比 ≤ 0.30）取自一个公开参考实现，
  **未在本项目数据上验证过**。
- **验证方法**：跑
  `python scripts/scan_quant_strategy_inputs.py wallet-quality --output data/quant_inputs/wallet_quality.json`，
  读每个指标的分布，按分位数设阈值，而不是照抄默认值。
- **要翻的开关**：给 `auto-wallet-observations` 传 `--quality-filter`（以及上面推出来的
  `--min-*` 覆盖值）。

> 需要注意：整条钱包跟单策略线在跟到结算的口径上是 −28.6% ROI，见
> [research-findings.md](research-findings.md#钱包跟单-wallet-alpha)。改进筛选质量能否救回来，
> 本身就是个开放问题。

---

## 7. 撤销未计分的 maker 单

- **主张**：`/orders-scoring` 报告为未计分的挂单赚不到流动性奖励，应当撤掉重挂。
- **现状**：审计以**只观测**方式发布（`MAKER_SCORING_CANCEL_UNSCORED=false`）。每个周期写一行
  `maker_scoring_audit`，含 `scoring / not_scoring / unknown / scoring_ratio`。
- **所需数据**：足够多的 `maker_scoring_audit` 行，用来确定基线 `scoring_ratio` 以及一个订单
  瞬时变成未计分的频率。在瞬时读数上撤单会白白churn 订单并丢掉队列位置。
- **验证方法**：确认被标为未计分的订单在 `MAKER_SCORING_UNSCORED_GRACE_SEC` 之后仍然未计分
  （即宽限窗口确实能区分瞬时与持续），并且 `unknown` 占比保持很小。
- **要翻的开关**：`MAKER_SCORING_CANCEL_UNSCORED=true`。

---

## 拿到付费数据后的快速复验清单

```bash
# 1. 安装选定的付费客户端（Dune SDK、Glassnode SDK 等）。
# 2. 更新验证脚本使用它：
#    - scripts/verify_near_certainty_trap.py        # 补 fetch_max_price()
#    - scripts/verify_onchain_signal_predictive_power.py
# 3. 运行并读 data/research/ 下的 JSON 报告。
# 4. 套用上面的晋级判据。通过就翻开关并重新部署。
```

## 无需数据验证、已经成立的部分

为完整起见，下面这些是**不需要**进一步数据验证就合入的，因为它们建立在教科书数学之上：

| 项 | 状态 | 依据 |
|---|---|---|
| Bellman 最优停止递归 | 已发布，默认开启 | Snell envelope (1965)，教科书结论 |
| `d-stop > 1-stop` 分批退出 | 已发布，默认开启 | Kobylanski 2009，已发表定理 |
| Bellman 的滚动 `p_t` 更新 | 已发布，默认开启 | 基础动态规划 |
| Quarter-Kelly | 2026 年前就已发布 | 直接来自 Kelly (1956) |

它们不出现在上面的待验证清单里。
