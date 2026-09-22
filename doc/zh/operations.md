[English](../en/operations.md) · [中文](../zh/operations.md)

# 运维

## 安装

Python 3.10+（推荐 3.11 以上）。

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # base + AI
```

依赖是拆开的，可以只装需要的部分：

| 文件 | 内容 |
|---|---|
| `requirements-base.txt` | 运行时：CLOB 客户端、websockets、aiohttp、FastAPI |
| `requirements-ai.txt` | LLM provider（`openai`、`anthropic`、`httpx`） |
| `requirements-dev.txt` | 测试工具（`pytest`） |
| `requirements.txt` | 总入口：base + AI |

`py-clob-client` 是版本锁定的（`==0.34.6`，以及 `py-clob-client-v2>=1.0.0,<2.0.0`），因为
`ExecutionEngine` 通过 `_force_py_clob_http1` 触碰了模块私有的 `_http_client`。任何客户端升级都
必须重新验证 `tests/test_client_factory.py`。

## 配置

```bash
cp .env.example .env
$EDITOR .env
```

见 [configuration.md](configuration.md)。跑 dry run 的最低要求是什么都不填 —— 默认配置不需要
钱包就能扫描。

## 运行

```bash
# Dry run（默认）：只扫描，不下单
python run_arb_bot.py

# 等价的模块形式
python -m polymarket_arb.main_loop

# 单独跑研究信号层，不需要钱包
python run_research.py --limit 20 --show-markets
python run_research.py --query btc --json
python -m research_signal.refresh --limit 10

# 最小回测 runner
python -m research.backtest.run --dataset default
```

`run_research.py` 是单独验证研究层最快的方式：拉活跃市场、按 `--query` 过滤、打印聚合报告
（来源分布、cache hit 状态）。`--json` 输出结构化结果便于离线分析。

## 旁路 worker

推荐的常态是四条链路一直跑 —— 影子、实盘、扫描器、晋级器 —— 而实盘只消费已验证的数据。

一条命令拉起整条链：

```bash
python scripts/run_automated_quant_pipeline.py --dotenv-path .env
```

它启动一份 bot 主循环加若干旁路数据进程，不会复制你的 `.env`。LLM 调用只发生在旁路里。

| 进程 | 职责 |
|---|---|
| `bot` | 主循环。读 `.env`；live 模式下内部有针对未晋级钱包的 shadow-only 通道。 |
| `logical-rules-auto` | 从 Gamma 拉同事件候选关系，让 LLM 只保留确定性包含/上界关系，刷新 `logical_constraints.json`。 |
| `event-baselines-auto` | 从 Gamma 拉带时间信息的事件，估计独立 baseline，刷新 `event_baselines.json`。 |
| `wallet-scanner` | 从 Polymarket Data API 自动发现活跃钱包，刷新 `wallet_observations.json`。 |
| `wallet-markout-scanner` | 从影子 telemetry 生成钱包 markout 样本。 |
| `wallet-promoter` | 只把通过 min-trades / ROI / 集中度 / 回撤 的钱包写入 `wallet_profiles.json`。 |
| `research-feeds-auto` | 针对当前热门市场提议 RSS 源，逐个真实 GET 验证后写入 `RESEARCH_SIGNAL_FEEDS_FILE`。支持 `--seed-feeds-file`，让用户提供的种子源跨周期保留、并在 LLM 掉线时仍然存活。 |

只要 bot 加两条 LLM 输入旁路：

```bash
python run_bot_with_llm_inputs.py --dotenv-path .env
```

拆开单独跑：

```bash
# 持续发现钱包
python scripts/scan_quant_strategy_inputs.py auto-wallet-observations \
  --min-trades 3 --min-notional 100 --max-wallets 25 \
  --repeat-interval-sec 120 --repeat-count 0 \
  --output data/quant_inputs/wallet_observations.json

# 只晋级通过验证的钱包
python scripts/scan_quant_strategy_inputs.py auto-promote-wallet-profiles \
  --telemetry-dir data/telemetry --lookback-days 7 \
  --min-trades 30 --min-lagged-roi 0.04 \
  --max-concentration 0.35 --max-drawdown 0.35 \
  --repeat-interval-sec 300 --repeat-count 0 \
  --output data/quant_inputs/wallet_profiles.json
```

只产生"观察"永远不会自动把钱包变成可跟单钱包。`WALLET_ALPHA_PROFILES_FILE` 必须来自影子验证
后的晋级路径。实盘实例上不要开 `WALLET_ALPHA_CANDIDATE_SHADOW_ENABLED`。

生成模板与 CSV 转换：

```bash
python scripts/quant_strategy_config_template.py --format env

python scripts/build_quant_strategy_inputs.py logical-constraints --input data/logical_constraints.csv
python scripts/build_quant_strategy_inputs.py event-baselines     --input data/event_baselines.csv
python scripts/build_quant_strategy_inputs.py wallet-observations --input data/wallet_observations.csv
```

CSV 字段约定：

- `logical-constraints`: `subject_market_id,bound_market_id,relation_type,min_violation_bps,tags,max_size_usdc`
- `event-baselines`: `condition_id,baseline_probability,confidence,time_to_event_sec`
  （脚本会写入 `generated_at`，运行时扣除已流逝时间；更推荐直接提供
  `resolution_at` / `resolution_ts`）
- `wallet-observations`: `wallet_address,market_id,category,action,observed_size_usdc`

## Dry-run 验证清单

第一次接通流水线时按顺序走一遍。

**1. 准备 `.env`**

```dotenv
ARB_DRY_RUN=true
RESEARCH_SIGNAL_ENABLED=true
TICK_RECORD_ENABLED=true
TELEMETRY_RECORD_ENABLED=true
AI_API_KEY=<provider key>
```

**2. 单独验证研究层**

```bash
python run_research.py --query btc --show-markets
python run_research.py --query btc --json
```

预期：输出里能看到 `source_counts`；第二次运行时 `cache_hit` 变成 `true`。

**3. 启动 bot + workers**

```bash
python run_bot_with_llm_inputs.py
```

预期：

- Dashboard 的 `Research Signals` 卡片有信号数量和摘要
- `strategy_status.meta.research_overlay` 开始累计 `applied / boosted / penalized / vetoed`
- 首轮 worker 跑完后 `data/quant_inputs/research_feeds.json` 被填充
- `data/quant_inputs/research_feeds_status.json` 显示 `status=ok`

**4. 离线回放**

```bash
python -m research.backtest.run --dataset default
```

预期：不需要钱包私钥也能运行，并生成 report、trade log 和推荐参数。

**5. 最后再考虑实盘**

确认 research 没有系统性反向误导、overlay 更多是在降噪而不是频繁 veto 全部信号、风控/执行/
dashboard 状态都稳定。然后回去重读 [research-findings.md](research-findings.md)，
再判断实盘是否值得 —— 在这份代码自己的数据上，答案是不值得。

## 无人值守运行

### tmux

```bash
cd ~/PolymarketBot
source .venv/bin/activate
tmux new -s arb
python run_arb_bot.py
```

```bash
# Ctrl+b 再按 d 断开，bot 继续运行
tmux ls                  # 查看会话
tmux attach -t arb       # 接回
tmux kill-session -t arb # 停止
```

### Dashboard 访问

Dashboard 默认关闭；开启后也只在本机监听：

```
http://127.0.0.1:8077
```

**不要改绑到 `0.0.0.0`。** 在本地机器上用 SSH 隧道访问：

```bash
ssh -N -L 18077:127.0.0.1:8077 -i /path/to/key.pem user@server
```

```powershell
# Windows PowerShell
ssh -N -L 18077:127.0.0.1:8077 -i C:\path\to\key.pem user@server
```

然后本地打开 `http://127.0.0.1:18077`。

如果出现 `channel ... open failed: connect failed: Connection refused`，说明 SSH 通了但服务器
端 8077 没有监听：

```bash
grep DASHBOARD_ENABLED .env
grep DASHBOARD_PORT .env
ss -lntp | grep 8077
tail -n 50 arb_bot.log
```

## 可观测性

### 永远不要整读日志

`arb_bot.log` 和 NDJSON telemetry 从几百 KB 到几百 MB 不等。整读既慢，也是更差的定位方式。
永远先聚合，再钻进聚合指出的那个窗口。

```bash
# 错误码分布
grep -oE 'code=[A-Z_]+' arb_bot.log | sort | uniq -c | sort -rn

# skip 原因排序
jq -r '.skip_reasons // {} | to_entries[] | .key' \
  data/telemetry/*.strategy_executions.ndjson | sort | uniq -c | sort -rn

# 按小时看盘口来源构成：WebSocket 到底有没有在工作
jq -r '[.ts[0:13], .book_stats.ws_hit, .book_stats.cache_hit, .book_stats.rest_fallback] | @tsv' \
  data/telemetry/*.cycle_metrics.ndjson | awk -F'\t' '
    {h[$1]++; ws[$1]+=$2; c[$1]+=$3; r[$1]+=$4}
    END {for (k in h) printf "%s ws=%d cache=%d rest=%d\n", k, ws[k], c[k], r[k]}' | sort

# UPDOWN 现货 basis 行
jq 'select(.event=="updown_spot_basis")' data/telemetry/*.risk_events.ndjson | head
```

只有在 summary 锁定了某个窗口或某个 `trace_id` 之后，才去拉原始行。一次性分析脚本放
`analysis/`。

### 每天要看的四个数

| 指标 | 位置 | 健康状态 |
|---|---|---|
| `book_stats.ws_hit` 占比 | `cycle_metrics` | 高；`rest_fallback` 占比上升说明 WS 订阅在丢 |
| `skip_reasons` 分布 | `strategy_executions` | 稳定；`per_market_rate_cap` 或 `tier_budget_below_min_order` 突增说明资金或上限配置不匹配 |
| `arbs_found_total` vs `t0_opportunities_total` | `cycle_metrics` | 前者是全部方向性信号，后者才是 T0 —— 不要把 "0 arbs" 读成"哪层都没信号" |
| `timing_stats` 里的延迟尖峰 | `cycle_metrics` | 平稳；尖峰通常意味着 REST fallback 或限流 |

### 标准排查流程

1. **明确范围** —— 时间窗、tier、market、token。模糊就先定下来，不要猜。
2. **跑 summary** —— PnL 曲线、`skip_reason_counts`、`book_stats` 比例、错误码统计。
3. **找尾部** —— PnL 突变、skip 突增、`rest_fallback` 飙升、延迟尖峰的那几个小时。
4. **深挖样本** —— 只对那个窗口拉原始行，用 `trace_id` / `run_id` 串联。
5. **归因** —— 区分市场原因（流动性下降、行情结构变化）和系统原因（bug、延迟、限流、订阅丢失）。
6. **给动作** —— 参数问题给具体调整方向 + 预期影响 + 风险；bug 直接定位到
   `file_path:line_number`。

### 重启语义

`run_id=run-<pid>-<UTC start>` 出现在每条日志和每行 telemetry 上。改模块对运行中的进程没有任何
影响。如果改完 `run_id` 没变，你看的还是旧代码。

## 走向实盘

实盘需要两道独立确认：`ARB_DRY_RUN=false` **且** `LIVE_TRADING_ACK=true`。缺一个，进程会在
安全校验处退出。

一个合理的首配：

- `LIVE_MAX_ORDER_SIZE_USDC` 和 `LIVE_MAX_TOTAL_EXPOSURE_USDC` 设小
- `MAKER_STRATEGY_ENABLED=false` —— 验证链路时不需要 T3 的 post-only/GTC 行为这个额外变量
- `PORTFOLIO_SYNC_ENABLED=true` 和 `LIVE_REQUIRE_PORTFOLIO_SYNC=true`
- 录制打开，事后能复原一切

要清楚小额资金能说明什么：$10 能验证 bot 会下单、会撤单、会重连、会处理异常。它验证不了策略
是否盈利 —— 见
[research-findings.md](research-findings.md#10-验证不了策略)。

两条 V2 相关的运维注意：

- Polymarket 于 2026-04-28 硬切到 CLOB V2。账户资金发生变化后必须调用 balance-allowance
  update（签名类型 3）。
- V2 之前录的数据和之后的不可比；任何基于旧数据标定的参数都要重新推。

另外，那份非常严格的 canary 模板（同时只允许一个持仓、`RISK_MAX_TOTAL_EXPOSURE` 约 $8）
意味着一个孤儿退出就会把 bot 卡住，直到手工清理或重启。这是有意的取舍，不是 bug。
