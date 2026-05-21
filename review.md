 严重（会直接打偏策略或长期累积失败）

  1. quant_input_scanner.py:208-211 — BUY_NO if side == "SELL" 把"卖出"当成"反向开仓"

  if outcome in {"no", "false"}:
      action = "BUY_NO" if side == "BUY" else "BUY_YES"
  else:
      action = "BUY_YES" if side == "BUY" else "BUY_NO"
  被跟随的钱包卖掉 NO 仅意味着它在平掉空头，并不是"它看多 YES"。逻辑上把 close-position 当作
  open-opposite，跟单方向会被系统性反向。建议：
  - 只信 side == "BUY" 的事件；SELL 完全丢弃；或
  - SELL 时仅作为"平仓信号"喂给 T2ExitManager，不要作为新 observation。

  2. quant_input_scanner.py:336-337 — lagged_follow_pnl_usdc = realized_pnl

  "realized_pnl_usdc": round(realized, 8),
  "lagged_follow_pnl_usdc": round(realized, 8),
  "lagged follow" 整套门槛的统计前提是延迟成交后还赚钱（说明 alpha 不是抢前 ms
  抢出来的）。这里两个字段直接相等，等于完全没有 lag 验证；下游 WalletAlphaScorer.min_lagged_roi=0.04
  永远等价于"realized_roi ≥4%"，会让大量靠 latency edge 的钱包通过门槛而上线后失败。必须用真正的"在 obs_ts + Δt
  重建价格"来算 lagged pnl。

  3. StrategyOrchestrator.current_exposure 单调递增 — T2/T3 平仓后 tier 预算不释放

  - record_execution 加 exposure（strategy_orchestrator.py:310-311），
  - record_settlement 是减的入口，但全代码库无任何调用方（grep 仅命中 tests）。
  - T2 平仓走 T2ExitManager._issue_exit →risk_manager.release_market_exposure（t2_exit_manager.py:420, 616），从未通知
  orchestrator。

  结果：跑得越久，_available_by_tier 报给 T2/T3 的可用预算越接近 0，最后 process_signals 全部命中
  tier_budget_below_min_order。这是典型的"测试过但生产没接线"的释放漏洞，建议在 T2ExitManager / maker fill 路径里同步调用
   orchestrator.record_settlement(tier, amount, pnl)，或干脆让 orchestrator 从 RiskManager 单一权威读 exposure。

  4. WalletAlphaDecision.size_multiplier 完全没接上信号

  wallet_alpha.py:62-67 计算 0.75-1.25 的 multiplier，但 signal_collectors.py:271 写死
  recommended_size_usdc=config.default_order_size_usdc。同样，LogicalConstraintDetector 也没有按 violation
  大小放缩，仅在规则里给一个 max_size_usdc。结果：信心 0.45 和 0.95 的钱包下单一样大。

  高（直接削弱策略 EV）

  5. EVENT_BASELINES_JSON 用静态 time_to_event_sec

  signal_collectors.py:201-208 直接把 JSON 里的 time_to_event_sec 透传给模型。JSON
  是手填或脚本生成的，文件写入到信号触发可能间隔数小时甚至几天，_urgency_from_time（event_calendar_model.py:74-80）与窗口
  过滤都会基于"过期的剩余时间"算。建议存绝对结算时间 ISO 字符串，运行时 now - resolution_at 计算
  time_to_event_sec，并在到期时间过去时 hard-veto。

  6. LogicalConstraintDetector 只买便宜的一腿，没有对冲

  本质这是一个"违反 P(A) ≤P(B)"的关系套利，理论无风险的做法是 short subject + long bound。Polymarket
  只能做多，所以代码改成只买 bound（logical_constraints.py:64-86）。问题：
  - 价差并不会"必然"收敛到 fair value（subject 可能继续溢价直到结算），只有 bound 真正涨到自身的 fair value
  才赚；其实是个"bound 被错杀，反弹"的方向性赌注，不是套利。
  - 信心曲线 0.60 + (violation-200)/2000 给到 0.95，但与真实的反弹概率没有任何映射。

  建议：把 signal_type / tier 改成 "directional"，不要走 T0/T1 的高 confidence Kelly；并在 SniperGate.min_net_edge_bps
  里给这个 signal_type 单独的、更高的门槛。

  7. SniperGate 与文档行为不一致

  sniper_gate.py:71-72 真实行为是 0.0-1.25 双向调整 size。注释和文档明确说"intentionally conservative ... only blocks or
  sizes ... too weak"。结果：高 edge 信号反而被放大 25%，这与"狙击门"的语义冲突，且与 Quarter-Kelly
  的方差控制理念矛盾（已经用 1/4 Kelly 缩了，又乘 1.25 又把方差吃回来）。要么改成 min(1.0, ...)，要么把文档改清楚。

  8. T2 信号防抖与 orchestrator rate cap 重复治理同一问题

  signal_collectors.py:55-88 加了 60s/0.005 deviation 防抖，strategy_orchestrator.py:445-458 又有 hourly
  cap，二者各自维护一份内存状态。两个 throttle 互相不感知，会出现：
  - 节流缓存清空（如 reset_statistical_signal_throttle 在测试中触发），但 orchestrator 的 hourly history 仍 cap 住；
  - 进程重启后两个状态都丢，但 cooldown_store 落盘，三套时序耦合不一致。

  不是 bug，但维护成本高。建议合并到一个单一的 emission-throttle 层。

  中（容易踩、但不一定立即出事）

  9. WalletAlphaScorer._confidence 基线 0.45 太宽松

  wallet_alpha.py:75-78：所有 sub-score 全 0 仍返回 0.45，再乘 size_multiplier 后能过 SNIPER_MIN_CONFIDENCE=0.75
  默认门槛之外的弱门槛配置。在没有真正的样本外验证（见上面 #2）的情况下，门槛全部是"虚高"。

  10. wallet_alpha "candidate shadow" 信号写死 expected_edge=300.0, confidence=0.10

  signal_collectors.py:300-313。如果 WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED=true 同时 SNIPER_GATE_ENABLED=true，sniper
  gate 的 min_confidence=0.75 必然 reject 全部 candidate；候选影子流就废了。两个开关需要显式互斥说明。

  11. discover_wallets_from_trades notional 算法不防御

  quant_input_scanner.py:187 用 price*size，没有验证 0 < price < 1、没有截 outliers。Data API 偶尔返回字符串/None；现在
  _float 把异常吞成 0，但若有"测试单"价格异常（如 1e6）的脏数据，notional 排序会被一两单污染，进而排到 max_wallets
  前几位。

  12. select_logical_constraints_with_llm 用 asyncio.run() 强制起新 loop

  quant_input_scanner.py:70。若调用方已在事件循环内（main_loop async 上下文里直接调），会抛
  RuntimeError。当前只在脚本入口调用是安全的，但模块被 import 后看不出这个约束。建议要么暴露 async 版本，要么文档化"only
  sync context"。

  13. _event_baseline_for_market 回退到 market.raw

  signal_collectors.py:469-485 当未在 baseline_map 找到时，会从 Gamma API 原始字段读 baseline_probability /
  confidence。Polymarket 官方 schema 没有这些字段，但若未来加上同名字段就会被误用。建议加白名单（only if condition_id 在
  baseline_map 里）。

  14. _FileValue 用 st_mtime_ns + UTF-8-sig 判断

  quant_input_store.py:36-46：
  - mtime 一致就用缓存，但脚本写入用 tmp.replace(path) 原子替换，mtime 会更新，没问题；
  - 但 read_text 后立刻 json.loads(text) 验证，验证失败 fallback 到 cached_text or fallback，没有
  telemetry/告警把"配置长期解析失败"暴露给操作者，会出现"我以为已经更新了规则，结果一直跑旧的 fallback"的 silent
  模式。建议补一次性 LOG.error。

  低 / 风格

  - EventCalendarModel.__init__ 默认 taker_fee_rate=0.05 与 config.polymarket_taker_fee_rate 默认重复；建议从 config
  注入，避免两处不同步漂移。
  - LogicalConstraintDetector.detect 内层 for subject for bound 全笛卡尔积 + emitted >= max_pairs_per_event
  的循环条件位置怪（quant_input_scanner.py:36-56），同 event 有 20 个市场就是 400 对，全 Gamma events 跑出来是几万行
  candidate，喂给 LLM 截 40 个 — 浪费 token；建议先按 question/keyword 去重。
  - cli_setup.parse_extra_rss_feeds 改动是干净的；runtime_analysis._summarize_quant_strategy_signals 已被加入摘要 — OK。

  策略层面的整体观察

  1. "研究 →策略"路径过度依赖人工/LLM 输入：logical_constraints、event_baselines、wallet_profiles 三类全部是预生成 JSON
  文件，bot 内部只做"读 + 校验"，没有任何 in-bot 自我学习。这与 auto_* 脚本的"重复跑、滚动覆盖"配合 OK，但缺一个版本号 +
  信号源审计，事后无法回放是哪一版 baseline 触发了哪笔单。建议每个 input 文件加 generated_at / source_run_id，并写入
  strategy_signals payload。
  2. T2 + 三个新策略全部用 StrategyTier.STATISTICAL_ARB：资金池共用，单一 tier 内只按 priority_score 排序（urgency 0.5 +
  conf 0.35 + edge 0.15）。Wallet alpha 的 urgency=0.65、logical 的 urgency=0.7、event_calendar 的 urgency 最高可 0.95 —
  后者会抢占真正模型驱动的 T2 mispricing signal。这三类的 EV 性质、相关性、爆雷模式完全不同，混在 0.15
  预算里互踩，建议拆出子预算或自定义 tier。
  3. Wallet-alpha + research overlay 同向加成放大尾部风险：研究 resonance 给 +1.10x size、wallet alpha 自带 +
  1.25x（如果接上）、sniper boost 也给 +1.25x — 三个都打满时是 1.72x。Quarter-Kelly 的安全垫被吃掉一半。建议各路
  multiplier 进入一个总 cap，例如 final_size = base × min(1.5, Πmultipliers)。
  4. wallet_alpha 数据通路是个闭环风险：build_wallet_markouts_from_shadow_rows 从自己的 virtual_fills.ndjson +
  positions_lifecycle.ndjson 反推 wallet 是否盈利（scan_quant_strategy_inputs.py:226-236）。换句话说，用 bot
  自己的影子成交结果给钱包打分，再回来跟单这些钱包。如果影子模型偏乐观，会出现"自我强化"的 false-positive
  钱包；建议把"validation 数据源"换成与跟单回路完全独立的 Polymarket 历史成交回放（按 wallet 公开成交价 + 1-5 min 后的
  mid 计算 lagged）。