# Polymarket 套利机器人

[English](README.md) · [中文](README.zh-CN.md)

面向 Polymarket 预测市场的多层交易系统：结构性套利、跨平台套利、模型驱动的统计套利、做市 ——
外加一整套风控、执行、telemetry 和回测基础设施，用来真正测量这些东西到底有没有用。

> ## 先读这一段
>
> **本仓库里的每一条策略都在真实数据上被检验过，并且都失败了。**
>
> T0 结构性套利：7 天 tick 数据里 clean 机会为 0 —— 每一个看似信号的都是交叉簿。
> T3 做市：53 笔平仓全部亏损。钱包跟单：跟到结算 −28.6% ROI。
> 最好看的那个结果 —— 15 分钟 UPDOWN 市场上的 +$443/周 —— 在注入 1 秒入场延迟后只剩 9%。
>
> 它作为**研究基础设施与负面结果记录**发布，不是一个能赚钱的交易系统。用它做的实盘已停止。
>
> 完整数字、方法和复现脚本：
> **[doc/zh/research-findings.md](doc/zh/research-findings.md)**

## 那为什么还要开源

有价值的不是策略，而是杀死这些策略的那套测量工具：

- **延迟注入**作为标准门禁 —— 揭穿本项目最大一个"edge"是 look-ahead 假象的那一个测试。
- **逐市场解析费率** —— 默认费率把 crypto Up/Down 成本低估了 14 倍，这就是回测正负的分界。
- **套利检测里的交叉簿拒绝** —— 没有它，交易所数据流的瞬时抖动会被读成免费的钱。
- **tick 级录制**而非快照采样 —— 快照拼接会造出从未同时存在过的机会。
- 诚实记账：总和旁边同时报中位单笔结果和胜率，并且先去重到结算单位。

这几条每一条都在本项目里抓到过一个假阳性，全部记录在
[doc/zh/research-findings.md](doc/zh/research-findings.md#可复用的方法论)。

## 策略分层

| 层 | 策略 | 机制 | 判决 |
|---|---|---|---|
| T0 | 结构性套利 | `Σ ask < 1 - fee` 时买下所有结果 | 没找到真实机会 |
| T1 | 跨平台 | Polymarket vs Kalshi 价差 | 从未跑到可检验样本 |
| T2 | 统计 / 模型驱动 | 贝叶斯公允价 vs 盘口 | 扣真实费率后为负 |
| T3 | 做市 | 在模型公允价两侧挂 maker 单 | 死于逆向选择 |

检测由 WebSocket 驱动：`OrderBookMirror` 在每次 best bid/ask 变动时触发，T0 直接进风控层，
因为结构性套利是毫秒级的游戏。其余全部走定时扫描。热路径上没有任何一层调用语言模型。

完整设计见 [doc/zh/architecture.md](doc/zh/architecture.md)。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # 默认 ARB_DRY_RUN=true

python run_arb_bot.py              # 只扫描，不下单
```

dry run 不需要钱包。真实下单需要两道独立确认（`ARB_DRY_RUN=false` **且**
`LIVE_TRADING_ACK=true`）。

```bash
python run_research.py --limit 20 --show-markets   # 单独跑研究层
python -m research.backtest.run --dataset default  # 离线回放
pytest                                             # 测试
```

## 文档

全部文档在 [`doc/`](doc/) 下，中英双语。

| 文档 | 内容 |
|---|---|
| [research-findings.md](doc/zh/research-findings.md) | **测了什么、什么被证伪、以及证伪它的方法论** |
| [architecture.md](doc/zh/architecture.md) | 分层设计、执行流、模块地图、跨文件契约 |
| [configuration.md](doc/zh/configuration.md) | 完整环境变量参考 |
| [operations.md](doc/zh/operations.md) | 运行、旁路 worker、dry-run 清单、可观测性 |
| [backtesting.md](doc/zh/backtesting.md) | tick 录制、回放 runner、如何不骗自己 |
| [ai-configuration.md](doc/zh/ai-configuration.md) | LLM provider 与 TypeSafe 打分模型 —— 全部在主循环之外 |
| [pending-validations.md](doc/zh/pending-validations.md) | 带开关合入的特性，以及各自的晋级判据 |
| [references.md](doc/zh/references.md) | 数据源、API、相关工作 |

## 项目结构

```
polymarket_arb/      主包：配置、数据流、检测、风控、执行
  └── strategies/    T0–T3，以及 Kelly、最优停止、编排
research/backtest/   离线回测 runner
research_signal/     研究信号 collectors / normalizers / scorers
analysis/            一次性实证研究脚本（见 analysis/README.md）
scripts/             旁路 worker 与验证入口
tests/               pytest 测试，外部 API 全部 mock
doc/                 文档，en + zh
```

约 71k 行 Python，1,190 个测试。

## 环境要求

Python 3.10+（推荐 3.11 以上）。`py-clob-client` 是版本锁定的，因为 `ExecutionEngine` 触碰了
模块私有的 HTTP 客户端属性，见
[doc/zh/operations.md](doc/zh/operations.md#安装)。

## 贡献

见 [doc/zh/contributing.md](doc/zh/contributing.md)（[English](CONTRIBUTING.md)）。
唯一的硬规矩：**永远不要提交 `.env`、私钥或真实钱包数据。**

## 免责声明

- 本项目仅供学习研究，不构成投资建议。
- 本仓库中的每一条策略都已在本项目自己的数据上被实证证伪，作者已停止向它们投入资金。
- 结构性套利机会在实际市场中极其稀缺 —— 专业做市商在毫秒级修正偏差。
- 统计套利的上限就是模型质量，模型错了只会更高效地亏钱。
- 做市面临逆向选择和库存风险，在本项目数据里这两者占了压倒性主导。
- 是否使用请自行判断、自担风险，作者不对任何损失负责。
- 请遵守 Polymarket 服务条款和所在地法律法规（含地理限制）。

## 许可证

[MIT](LICENSE)
