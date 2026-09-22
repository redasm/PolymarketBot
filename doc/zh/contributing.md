[English](../../CONTRIBUTING.md) · [中文](../zh/contributing.md)

# 贡献指南

感谢关注。这是一个实盘交易系统的研究代码库，所以下面有几条规矩比一般项目严。

## 永远不要提交密钥

- `.env`、私钥、API key、真实钱包数据，绝不能进仓库，也不能贴进 issue。
- `.gitignore` 已覆盖 `.env`、`.env.*`（example 除外）、`*.pem`、`*.key`。不要绕过它。
- `PRIVATE_KEY` 泄漏等于钱包被清空。真发生了，先轮换密钥，再考虑 git 历史。
- `data/` 下录制的 telemetry 包含你自己的交易行为。它已被 gitignore，保持这样。

## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-base.txt -r requirements-dev.txt
pytest
```

`pyproject.toml` 里设了 `pythonpath = ["."]`，跑测试不需要先安装。外部 API 全部 mock，
测试不需要网络也不需要钱包。

## 提 PR 之前

- `pytest` 通过。
- 新行为有测试。共享 fixture 在 `tests/conftest.py`。
- 网络 / API / 部分成交 / 重连路径上的改动要有异常覆盖。这段代码是无人值守地对着真钱跑的。
- 热路径代码要打结构化日志，保持 `trace_id` 连续。
- 不要新增运行时依赖，除非你能说明标准库和现有依赖为什么不够用 —— 在 PR 描述里说明。
- 修 bug 只动相关代码，不要顺手重构无关模块。

## 需要格外小心的区域

这些改动影响真实订单和真实资金。PR 里要写清理由，而不只是给 diff：

- 信号生成、仓位 sizing、风控、执行
- 任何改变"何时下单/如何下单"的东西
- [architecture.md](architecture.md#跨文件设计契约) 里记录的跨文件契约 ——
  按方向决定订单类型、从成交而非信号解析持仓方向、风控状态同步窗口。每一条都对应一个进过生产的
  bug。

## 实证主张

如果一个改动是靠性能主张来论证的，那这个主张需要证据，而证据需要通过
[backtesting.md](backtesting.md#让回测可信) 里的检查：

- 延迟注入，并报告存活比例
- 逐市场解析费率，不用默认值
- 退出按可成交价计价，不用 mid
- 结果去重到结算单位
- 总和旁边同时给出中位单笔结果和胜率

"回测是正的"不够。本仓库里好几条策略都有正的回测，最后都是错的。

没看过底层数据分布之前，不要提阈值建议。

## 文档

文档在 `doc/en/` 和 `doc/zh/`，章节结构一一对应以便 diff。改了一边就改另一边 ——
或者在 PR 里说明你改不了，以及现在哪一份落后了。

两版里的代码、标识符、日志字符串都保持英文。

## 代码风格

跟着周围代码走：一样的命名、一样的注释密度、一样的写法。没有强制 formatter。
