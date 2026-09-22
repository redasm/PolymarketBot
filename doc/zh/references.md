[English](../en/references.md) · [中文](../zh/references.md)

# 外部参考

本项目用到的数据源、API 和相关工作，附上每一项实际可用到什么程度。

## Polymarket

| 资源 | URL | 备注 |
|---|---|---|
| CLOB API | `https://clob.polymarket.com` | 订单簿、下单、`/rewards/markets`、`/orders-scoring`、`/prices-history`。 |
| Gamma API | `https://gamma-api.polymarket.com` | 市场与事件元数据，`?closed=true` 拿已结算市场。 |
| Data API | `https://data-api.polymarket.com` | 持仓、成交、`/closed-positions`。 |
| 文档 | <https://docs.polymarket.com> | 含 deposit wallet 指南（签名类型 3 的依据）。 |

两个实质影响结论的平台事实：

- **Fee V2，2026-03-30。** taker 费 = `rate · p · (1-p)`，`p = 0.5` 时最高。`rate` 逐市场不同
  —— crypto Up/Down 是 `feeSchedule.rate = 0.07`，而常见默认值是 `0.005`。
- **CLOB V2 硬切换，2026-04-28。** 此前录的数据与此后不可比。账户资金变动后必须调用
  balance-allowance update（签名类型 3）。

试图验证历史性主张时实测到的免费层限制（2026-05-22）：

- `clob.polymarket.com/prices-history` 对已关闭 token 返回空，只有活跃市场保留历史。
- `data-api.polymarket.com/trades` 静默忽略逐市场过滤，只返回全局流。
- Goldsky `orderbook-subgraph/prod` 在重市场上 `orderBy: timestamp desc` 会 statement-timeout；
  更老的市场未被索引。

## 天气数据

用到的全部 Open-Meteo 端点，均免费且无需 key：

| 端点 | 用途 |
|---|---|
| `https://ensemble-api.open-meteo.com/v1/ensemble` | 驱动概率估计的 GFS 集合预报 |
| `https://historical-forecast-api.open-meteo.com/v1/forecast` | 回测用的历史预报归档 |
| `https://previous-runs-api.open-meteo.com/v1/forecast` | **lead-honest** 评估 —— 决策时点之前那一版预报 |
| `https://geocoding-api.open-meteo.com/v1/search` | 城市名 → 坐标 |

previous-runs 端点是诚实评估的关键。没有它，天气回测会静默使用某个日期的最优预报，而不是
决策时能拿到的预报 —— 那就是 look-ahead。做完这个修正之后策略转负，见
[research-findings.md](research-findings.md#天气)。

## 新闻与情绪

| 源 | 鉴权 | 备注 |
|---|---|---|
| GDELT DOC 2.0 | 无 | 信息层体检用的。限流很严：1s 间隔会产生假 0 命中，必须 7s + 退避。它的 `enddatetime` 不精确，客户端要按 `seendate` 再过滤一遍。 |
| alternative.me Fear & Greed | 无 | 每个 crypto 主题一行情绪数据，实现为 `CryptoMacroCollector`。 |
| Manifold Markets | 无 | 每个主题的群体概率；主要在新闻 RSS 稀薄的政治、地缘、体育、颁奖类有用。 |
| RSS feeds | 无 | 由 `research-feeds-auto` worker 自动维护，每个源都要通过真实 GET 验证才被接受。 |

## 评估过但没买的付费源

[pending-validations.md](pending-validations.md) 里的验证需要这些：

| 源 | 用途 | 备注 |
|---|---|---|
| Dune Analytics | Polymarket 历史成交事件、逐市场最高价 | 有免费档和约 $390/月档；已有现成的 Polymarket dashboard。 |
| Goldsky 付费层 | 同上，但没有 statement timeout | — |
| Glassnode | MVRV Z-Score、SOPR | 约 $30–40/月。 |
| CryptoQuant | 同样的指标，免费层有 1 天延迟 | 对日级 horizon 的市场，延迟可能可接受。 |
| SoSoValue / Coinglass | ETF 净流入 | 免费层约 60 req/min，配合缓存可用。 |
| FRED | M2、Fed funds | 带 API key 免费。 |

## 打分模型

- TypeSafe Jev —— <https://docs.typesafe.ai>。"System One" 模型，返回类型化的校准答案而不是
  文本。客户端实现在 `polymarket_arb/typesafe_provider.py`，直连 `POST /v1/systemone`。
  实测显著劣于盘口，见
  [research-findings.md](research-findings.md#typesafe-jev--一个校准过的打分模型能打败盘口吗)。

## 相关工作

构建过程中调研过的其他开源预测市场 bot 与工具包。列出不代表推荐 —— 没有做过 benchmark。

- <https://github.com/HarrierOnChain/Prediction-Markets-Trading-Bot-Toolkits>
- <https://github.com/warproxxx/poly-maker>
- <https://github.com/warproxxx/poly_data> —— 直接读链上 `OrderFilled` 事件，
  是重建 V2 成交的有用参考
- <https://github.com/MrFadiAi/Polymarket-bot>
- <https://github.com/suislanchez/polymarket-kalshi-weather-bot>
- <https://github.com/MoonsatProtocol/Polymarket-Weather-Bot>
- <https://github.com/yangyuan-zhen/PolyWeather>
- <https://github.com/nicolastinkl/hermes_weatherbot>
- <https://github.com/AruneshDev/Automated-Trading-System-Kalshi-Weather-Model>
- <https://github.com/bwjoke/BTC-Trading-Since-2020>
- <https://github.com/txbabaxyz/mlmodelpoly> —— T2 里的公允价 / 波动率 / microprice 思路
  参考了这个项目

## 用到的学术与教科书结论

| 结论 | 用在哪 |
|---|---|
| Kelly (1956) | `strategies/kelly.py`，Quarter-Kelly 仓位 |
| Snell envelope / 最优停止 (1965) | `strategies/optimal_stopping.py` |
| Kobylanski (2009)，多次停止优于单次停止 | `T2_SCALE_OUT_TRANCHES` 分批退出 |
| Taleb，杠铃式配置 | `T2_BARBELL_ENABLED`（关闭，未验证） |
| Becker (2025)，冷门/热门税 | `T2_REJECT_PRICE_BELOW` / `_ABOVE`，以及 T3 flow bias |
