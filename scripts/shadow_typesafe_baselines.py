"""TypeSafe Jev 影子打标：Jev 概率 vs 盘口价格 vs 最终结算（只写 NDJSON，不碰 quant_inputs）.

影子打标入口；设计与结论见 doc/zh/ai-configuration.md 与 doc/zh/research-findings.md。

    # 一轮打标（默认候选 100 个非数值二元市场）
    python scripts/shadow_typesafe_baselines.py scan --limit 100

    # 每 30 分钟一轮，常驻
    python scripts/shadow_typesafe_baselines.py scan --repeat-interval-sec 1800

    # 结算回填：给已结算市场补 outcome ∈ {0,1}
    python scripts/shadow_typesafe_baselines.py backfill

    # 历史回放（已结算市场的历史快照，标签已知）：先建快照再打标
    python analysis/build_typesafe_replay_snapshots.py --ticks-dir "E:/PolymarketData/*/data/ticks"
    python scripts/shadow_typesafe_baselines.py replay --variant both

输出：
- ``data/telemetry/typesafe_shadow.ndjson``       每市场每轮一行（jev_p_yes / market_mid / days_to_resolution …）
- ``data/telemetry/typesafe_shadow_settlements.ndjson``  backfill 写入的结算结果
- ``data/telemetry/typesafe_requests.ndjson``     每次 API 请求一行（usage / 429 / latency）
- ``data/telemetry/typesafe_shadow_status.json``  最近一轮汇总（skip 原因分布、stats）
- ``data/typesafe_replay/*``                      历史回放产物（刻意不放 telemetry 目录：
  ``DataJanitor`` 对 ``data/telemetry`` 有 14 天淘汰 + 2GB 上限，回测数据要长期保留）

state 里**不放**盘口价格（§5 决定），否则 Brier 对比失去独立性；价格只记进 NDJSON。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.config import ArbConfig
from polymarket_arb.market_scanner import MarketScanner, _safe_parse_market
from polymarket_arb.models import MarketInfo
from polymarket_arb.typesafe_provider import (
    Choice,
    Noul,
    SystemOneResponse,
    TypeSafeConfig,
    TypeSafeError,
    TypeSafeJevClient,
)

LOG = logging.getLogger("shadow_typesafe")

DEFAULT_OUTPUT = "data/telemetry/typesafe_shadow.ndjson"
DEFAULT_SETTLEMENTS = "data/telemetry/typesafe_shadow_settlements.ndjson"
DEFAULT_REQUESTS_TELEMETRY = "data/telemetry/typesafe_requests.ndjson"
DEFAULT_STATUS = "data/telemetry/typesafe_shadow_status.json"

ROW_KIND = "typesafe_shadow"
SETTLEMENT_KIND = "typesafe_shadow_settlement"

# ---------------------------------------------------------------------------
# 市场筛选：数值类市场不给 Jev（文档明说 "Jev is not a calculator"）
# ---------------------------------------------------------------------------

_NUMERIC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\$\s?\d",                                   # $100k / $ 3.5
        r"\d+(?:\.\d+)?\s?%",                          # 5% / 0.25 %
        r"\bupdown\b|\bup or down\b",                  # 15m updown
        r"\b(?:above|below|higher than|lower than|at least|at most|more than|less than|"
        r"greater than|exceed|exceeds|reach|reaches|hit|hits|close at|closes at|between|"
        r"over|under)\b[^.?]{0,40}\d",                 # "above 5,000", "reach 100"
        r"\bprice of\b|\bmarket cap\b|\bmarket capitalization\b|\bapy\b|\btvl\b",
        r"°|\bdegrees?\b|\btemperature\b|\bcelsius\b|\bfahrenheit\b",
        r"\bby \d+\+? (?:points?|goals?|runs?|votes?)\b",  # 体育/选举差额
        r"\b(?:cpi|gdp|unemployment rate|basis points|bps)\b",  # 利率决议（cut/hold）是分类事件，不算数值
        r"\d+\s*[-–]\s*\d+",                            # 180-199 tweets（区间计数）
        r"[<>≤≥]\s*\d",                                 # <40 tweets
        r"\b\d+\s+or\s+(?:more|fewer|less)\b|\bfewer than\b|\b(?:more|less) than \d",
        r"\ball[- ]time high\b|\bath\b",                # 价格新高
        r"\b\d{2,}(?:[,.]\d+)?[kmb]?\b",                # 去日期后仍剩 ≥2 位数字 → 阈值/计数
    )
)

# 顺序即优先级：先特化（地缘/政治）再泛化（体育里的 "win" 会误吃选举）
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("geopolitics", ("war", "ceasefire", "missile", "invasion", "military", "sanction", "nato", "troops", "strike on",
                     "hostage", "blockade", "strait", "regime", "airspace", "capture", "captures", "leadership change")),
    ("politics", ("election", "elections", "president", "senate", "congress", "governor", "vote", "poll", "nominee", "primary",
                  "impeach", "cabinet", "resign", "prime minister", "parliament", "parliamentary", "duma", "seats",
                  "bill ", "executive order", "supreme court", "out as")),
    ("macro", ("fed ", "fomc", "rate cut", "rate hike", "interest rate", "inflation", "recession", "tariff", "gdp",
               "treasury", "ecb", "boj", "bank of japan", "bank of england")),
    ("crypto", ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "token", "etf approval", "airdrop", "blockchain")),
    ("sports", ("nba", "nfl", "mlb", "nhl", "premier league", "champions league", "ufc", "f1", "grand prix", "world cup",
                "super bowl", "playoff", "playoffs", "vs.", " vs ", "tournament", "ballon d'or", "fc", "calcio",
                "win on", "spread:", "moneyline", "grand slam", "open final")),
    ("entertainment", ("oscar", "grammy", "emmy", "box office", "album", "movie", "netflix", "spotify", "billboard",
                       "tv show", "performs")),
    ("science_tech", ("spacex", "launch", "nasa", "openai", "anthropic", "gpt", "apple", "iphone", "tesla", "ai model",
                      "millennium prize")),
)


_DATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?",  # ISO 日期（体育 "win on 2026-09-18"）
        r"\b(?:19|20)\d{2}\b",                                     # 年份先剥，避免 "September 2026" 被当成 "September 20" + "26"
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?!\d)(?:st|nd|rd|th)?",
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b",
        r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
        r"\bq[1-4]\b",
        r"\b\d{1,2}:\d{2}\s*(?:am|pm|et|pt|utc)?\b",
        r"\bweek\s+\d{1,2}\b|\bround\s+\d{1,2}\b|\bgame\s+\d\b|\bseason\s+\d{1,2}\b|\bday\s+\d{1,3}\b",
    )
)


def _strip_dates(text: str) -> str:
    """去掉日期 / 年份 / 时间 / 轮次编号，避免 "by June 30" 被当成数值阈值."""
    out = text
    for p in _DATE_PATTERNS:
        out = p.sub(" ", out)
    return out


def is_numeric_market(question: str, description: str = "") -> bool:
    """价格阈值 / 百分比 / 温度 / 差额类市场 → True（跳过 Jev）.

    只看 question；description 里常有与结算无关的价格背景，容易误杀。
    """
    text = (question or "").strip()
    if not text:
        return False
    stripped = _strip_dates(text)
    return any(p.search(stripped) for p in _NUMERIC_PATTERNS)


def _keyword_regex(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword.strip())
    prefix = r"\b" if keyword[:1].isalnum() else ""
    suffix = r"\b" if keyword[-1:].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


_CATEGORY_REGEX: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (name, tuple(_keyword_regex(k) for k in keywords)) for name, keywords in _CATEGORY_KEYWORDS
)


def guess_category(question: str, event_title: str = "", tags: Optional[list[str]] = None) -> str:
    """粗分类，只为评估时分桶；存进 NDJSON 后随时可重分."""
    haystack = " ".join([question or "", event_title or "", " ".join(tags or [])])
    for name, patterns in _CATEGORY_REGEX:
        if any(p.search(haystack) for p in patterns):
            return name
    return "other"


def extract_tags(raw: Any) -> list[str]:
    """Gamma 行里的 tags / events[0].tags → label 列表（best-effort）."""
    if not isinstance(raw, dict):
        return []
    sources: list[Any] = [raw.get("tags")]
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        sources.append(events[0].get("tags"))
    labels: list[str] = []
    for tags in sources:
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if isinstance(tag, dict):
                label = str(tag.get("label") or tag.get("slug") or "").strip()
            else:
                label = str(tag or "").strip()
            if label and label not in labels:
                labels.append(label)
    return labels


def parse_iso_ts(value: str) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def days_to_resolution(end_date: str, *, now: float) -> Optional[float]:
    ts = parse_iso_ts(end_date)
    if ts is None:
        return None
    return round((ts - now) / 86400.0, 3)


def yes_token(market: MarketInfo):
    for token in market.tokens:
        if (token.outcome or "").strip().lower() == "yes":
            return token
    return None


def is_binary_yes_no(market: MarketInfo) -> bool:
    outcomes = {(t.outcome or "").strip().lower() for t in market.tokens}
    return {"yes", "no"}.issubset(outcomes) and len(market.tokens) == 2


def _coerce_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def market_prices(market: MarketInfo) -> dict[str, Optional[float]]:
    """YES 侧 last / bestBid / bestAsk / mid（Gamma 行里的字段，best-effort）."""
    raw = market.raw if isinstance(market.raw, dict) else {}
    token = yes_token(market)
    yes_price = _coerce_float(token.price) if token is not None else None
    best_bid = _coerce_float(raw.get("bestBid"))
    best_ask = _coerce_float(raw.get("bestAsk"))
    mid: Optional[float] = None
    if best_bid is not None and best_ask is not None and 0.0 <= best_bid <= best_ask <= 1.0:
        mid = round((best_bid + best_ask) / 2.0, 4)
    elif yes_price is not None:
        mid = round(yes_price, 4)
    return {"market_yes_price": yes_price, "best_bid": best_bid, "best_ask": best_ask, "market_mid": mid}


# ---------------------------------------------------------------------------
# 候选选择
# ---------------------------------------------------------------------------


def select_candidates(
    markets: list[MarketInfo],
    *,
    now: float,
    limit: int,
    min_days: float,
    max_days: float,
) -> tuple[list[dict[str, Any]], Counter]:
    """过滤出可打标市场；返回 (候选, skip 原因计数)."""
    skips: Counter = Counter()
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for market in markets:
        if len(candidates) >= limit:
            skips["limit_reached"] += 1
            continue
        if not market.condition_id or market.condition_id in seen:
            skips["duplicate"] += 1
            continue
        seen.add(market.condition_id)
        if not market.active or market.closed:
            skips["inactive"] += 1
            continue
        if not is_binary_yes_no(market):
            skips["not_binary_yes_no"] += 1
            continue
        if not market.question.strip():
            skips["no_question"] += 1
            continue
        raw = market.raw if isinstance(market.raw, dict) else {}
        description = str(raw.get("description") or "").strip()
        if is_numeric_market(market.question, description):
            skips["numeric"] += 1
            continue
        days = days_to_resolution(market.end_date, now=now)
        if days is None:
            skips["no_end_date"] += 1
            continue
        if days < min_days:
            skips["resolves_too_soon"] += 1
            continue
        if days > max_days:
            skips["resolves_too_late"] += 1
            continue
        tags = extract_tags(raw)
        candidates.append(
            {
                "market": market,
                "condition_id": market.condition_id,
                "slug": market.slug,
                "event_id": market.event_id,
                "event_title": market.event_title,
                "question": market.question,
                "description": description[:4000],
                "category": guess_category(market.question, market.event_title, tags),
                "tags": tags,
                "days_to_resolution": days,
                "resolution_at": market.end_date,
                "liquidity": market.liquidity,
                "volume_24h": market.volume_24h,
                **market_prices(market),
            }
        )
    return candidates, skips


# ---------------------------------------------------------------------------
# state / questions
# ---------------------------------------------------------------------------


def build_state(
    candidate: dict[str, Any],
    news: list[dict[str, Any]],
    *,
    as_of_date: Optional[str] = None,
) -> dict[str, Any]:
    """英文结构化 state。日期比较在代码里做，只给 days_to_resolution；不放盘口价.

    ``as_of_date``（``YYYY-MM-DD``）只用于历史回放的 A/B 变体：告诉模型"当时"是哪天，
    配合 :func:`build_questions` 的同名参数限制它只用该日期前的信息。默认 ``None``
    时输出与前向影子逐字节一致。
    """
    days = candidate["days_to_resolution"]
    state: dict[str, Any] = {
        "market_question": candidate["question"],
        "resolution_criteria": candidate["description"] or "(no description provided)",
        "days_to_resolution": round(float(days), 1),
    }
    if as_of_date:
        state["as_of_date"] = as_of_date
    if candidate.get("event_title") and candidate["event_title"] != candidate["question"]:
        state["event_title"] = candidate["event_title"]
    if news:
        state["recent_news"] = [
            {
                "headline": str(item.get("summary") or "")[:300],
                "source": str(item.get("source") or ""),
                "published_at": str(item.get("published_at") or ""),
            }
            for item in news[:5]
        ]
    return state


def build_questions(*, has_news: bool, as_of_date: Optional[str] = None) -> dict[str, Any]:
    """构造 question 集合；``as_of_date`` 非空时给主问题加一句时点约束（历史回放 A/B 用）."""
    as_of_clause = (
        f" Answer as of {as_of_date}: judge only from information available on or before that date, "
        "and ignore anything you know about what happened afterwards."
        if as_of_date
        else ""
    )
    questions: dict[str, Any] = {
        "resolves_yes": Choice(
            instructions=(
                "Based on the market question and its resolution criteria, will this market resolve YES? "
                "Judge the literal resolution criteria, the base rate for this kind of event, "
                "and how much time remains." + as_of_clause
            ),
            criteria={
                "yes": "The market resolves YES under its stated resolution criteria",
                "no": "The market resolves NO under its stated resolution criteria",
            },
        ),
        "ambiguous_resolution": Noul(
            instructions=(
                "Are the resolution criteria ambiguous enough that reasonable people could dispute "
                "which outcome occurred?"
            ),
            criteria={
                "true": "Key terms are undefined, the data source is unclear, or edge cases are not covered",
                "false": "The outcome is objectively verifiable from a clearly named source",
            },
        ),
    }
    if has_news:
        questions["news_relevant"] = Noul(
            instructions="Does the recent news materially bear on this market's outcome?",
            criteria={
                "true": "At least one item directly changes the likelihood of YES or NO",
                "false": "The news is irrelevant, generic, or about a different question",
            },
        )
    return questions


def build_row(
    candidate: dict[str, Any],
    response: Optional[SystemOneResponse],
    *,
    run_id: str,
    round_id: int,
    ts: float,
    news_count: int,
    error: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": ROW_KIND,
        "ts": ts,
        "run_id": run_id,
        "round_id": round_id,
        "condition_id": candidate["condition_id"],
        "slug": candidate["slug"],
        "event_id": candidate["event_id"],
        "question": candidate["question"],
        "category": candidate["category"],
        "tags": candidate["tags"],
        "days_to_resolution": candidate["days_to_resolution"],
        "resolution_at": candidate["resolution_at"],
        "news_count": news_count,
        "market_yes_price": candidate["market_yes_price"],
        "best_bid": candidate["best_bid"],
        "best_ask": candidate["best_ask"],
        "market_mid": candidate["market_mid"],
        "liquidity": candidate["liquidity"],
        "volume_24h": candidate["volume_24h"],
        "jev_model": None,
        "jev_p_yes": None,
        "jev_confidence": None,
        "jev_choice": None,
        "jev_ambiguous": None,
        "jev_news_relevant": None,
        "latency_ms": None,
        "input_tokens": None,
        "request_id": None,
        "attempts": None,
    }
    if response is not None:
        resolves = response.choices.get("resolves_yes")
        ambiguous = response.nouls.get("ambiguous_resolution")
        news_rel = response.nouls.get("news_relevant")
        row.update(
            {
                "jev_model": response.model,
                "jev_p_yes": round(resolves.probabilities.get("yes", 0.0), 4) if resolves else None,
                "jev_confidence": round(resolves.confidence, 4) if resolves else None,
                "jev_choice": resolves.choice if resolves else None,
                "jev_ambiguous": round(ambiguous.noul, 4) if ambiguous else None,
                "jev_news_relevant": round(news_rel.noul, 4) if news_rel else None,
                "latency_ms": response.latency_ms,
                "input_tokens": response.input_tokens,
                "request_id": response.request_id,
                "attempts": response.attempts,
            }
        )
    if error:
        row["error"] = error
    return row


# ---------------------------------------------------------------------------
# 新闻（可选：复用 research_feeds.json 的 RSS 模板）
# ---------------------------------------------------------------------------


def load_feeds(path: Optional[Path]) -> list[tuple[str, str]]:
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        LOG.warning("feeds 文件解析失败 path=%s err=%s", path, e)
        return []
    rows = payload.get("feeds", []) if isinstance(payload, dict) else []
    feeds: list[tuple[str, str]] = []
    for idx, row in enumerate(rows if isinstance(rows, list) else [], start=1):
        if not isinstance(row, dict):
            continue
        template = str(row.get("url_template") or row.get("url") or "").strip()
        if not template or "{query}" not in template:
            continue
        feeds.append((str(row.get("name") or f"feed_{idx}"), template))
    return feeds


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def append_ndjson(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def write_json_atomically(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def make_run_id() -> str:
    return f"shadow-{os.getpid()}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


async def score_candidates(
    client: TypeSafeJevClient,
    candidates: list[dict[str, Any]],
    *,
    run_id: str,
    round_id: int,
    feeds: list[tuple[str, str]],
    concurrency: int,
    news_per_market: int,
) -> list[dict[str, Any]]:
    """并发打标.

    candidate 上可选的 ``as_of_date`` 键会透传给 :func:`build_state` /
    :func:`build_questions`（历史回放的 A/B 变体用）；前向影子不设这个键，行为不变。
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    collector = None
    if feeds:
        from research_signal.collectors.base import GenericRSSCollector

        collector = GenericRSSCollector(feeds, max_items_per_feed=max(1, news_per_market))

    async def _one(candidate: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            news: list[dict[str, Any]] = []
            if collector is not None:
                try:
                    news = await asyncio.to_thread(collector.collect, [candidate["question"]])
                except Exception as e:
                    LOG.warning("news 拉取失败 cid=%s: %s", candidate["condition_id"][:10], e)
                    news = []
            as_of_date = candidate.get("as_of_date") or None
            state = build_state(candidate, news, as_of_date=as_of_date)
            questions = build_questions(has_news=bool(news), as_of_date=as_of_date)
            ts = time.time()
            try:
                response = await client.system_one(state, questions, tag=candidate["condition_id"])
            except TypeSafeError as e:
                LOG.warning("Jev 打标失败 cid=%s: %s", candidate["condition_id"][:10], e)
                return build_row(candidate, None, run_id=run_id, round_id=round_id, ts=ts, news_count=len(news), error=str(e)[:300])
            return build_row(candidate, response, run_id=run_id, round_id=round_id, ts=ts, news_count=len(news))

    return list(await asyncio.gather(*(_one(c) for c in candidates)))


def _telemetry_sink(path: Path):
    def _sink(row: dict[str, Any]) -> None:
        append_ndjson(path, [row])

    return _sink


# ---------------------------------------------------------------------------
# replay：历史快照打标（结论见 doc/zh/research-findings.md）
# ---------------------------------------------------------------------------

DEFAULT_REPLAY_SNAPSHOTS = "data/typesafe_replay/replay_snapshots.jsonl"
DEFAULT_REPLAY_OUTPUT = "data/typesafe_replay/typesafe_replay.ndjson"
DEFAULT_REPLAY_STATUS = "data/typesafe_replay/typesafe_replay_status.json"
DEFAULT_REPLAY_REQUESTS = "data/typesafe_replay/typesafe_replay_requests.ndjson"

REPLAY_ROW_KIND = "typesafe_replay"
REPLAY_STRATA_DAYS: tuple[float, ...] = (30.0, 7.0, 1.0)


def pick_strata(
    ts_ms_list: Sequence[int],
    end_ts_ms: int,
    targets_days: Sequence[float] = REPLAY_STRATA_DAYS,
) -> list[tuple[str, int]]:
    """给一个市场的可用快照时刻挑分层样本点.

    对每个目标提前量（默认 T-30d / T-7d / T-1d）取 ``|ts - (end - target)|`` 最小的快照，
    去重后按时间升序返回 ``[(stratum_label, ts_ms), ...]``。

    - 只考虑 ``ts_ms < end_ts_ms`` 的快照（结算后的 tick 不是预测输入）
    - 同一条快照被多个目标选中时只保留一次，标签取提前量最小的那个目标
    - 无可用快照返回空列表
    """
    usable = sorted({int(ts) for ts in ts_ms_list if ts is not None and int(ts) < int(end_ts_ms)})
    if not usable:
        return []
    chosen: dict[int, str] = {}
    for target in sorted(targets_days):  # 先小提前量，保证重复命中时标签是更近的那个
        target_ts = int(end_ts_ms) - int(target * 86_400_000)
        best = min(usable, key=lambda ts: abs(ts - target_ts))
        label = f"T-{target:g}"
        chosen.setdefault(best, label)
    return sorted(((label, ts) for ts, label in chosen.items()), key=lambda item: item[1])


def build_replay_candidate(snapshot: dict[str, Any], *, as_of_date: Optional[str] = None) -> dict[str, Any]:
    """把 ``replay_snapshots.jsonl`` 的一行转成 candidate 契约（与 select_candidates 同构）.

    ``market`` 置 None（下游不解引用）；``liquidity`` / ``volume_24h`` 置 None——历史流动性
    不在 tick 行里，不拿今天的 Gamma 值冒充当时的值。
    """
    candidate: dict[str, Any] = {
        "market": None,
        "condition_id": str(snapshot.get("condition_id") or ""),
        "slug": str(snapshot.get("slug") or ""),
        "event_id": snapshot.get("event_id"),
        "event_title": str(snapshot.get("event_title") or ""),
        "question": str(snapshot.get("question") or ""),
        "description": str(snapshot.get("description") or "")[:4000],
        "category": str(snapshot.get("category") or "other"),
        "tags": list(snapshot.get("tags") or []),
        "days_to_resolution": snapshot.get("days_to_resolution"),
        "resolution_at": str(snapshot.get("end_date") or ""),
        "liquidity": None,
        "volume_24h": None,
        "market_yes_price": _coerce_float(snapshot.get("market_yes_price")),
        "best_bid": _coerce_float(snapshot.get("best_bid")),
        "best_ask": _coerce_float(snapshot.get("best_ask")),
        "market_mid": _coerce_float(snapshot.get("market_mid")),
    }
    if as_of_date:
        candidate["as_of_date"] = as_of_date
    return candidate


def snapshot_as_of_date(snapshot: dict[str, Any]) -> str:
    """快照时刻的 UTC 日期（YYYY-MM-DD），给 as_of 变体用."""
    ts_ms = snapshot.get("snapshot_ts_ms")
    if ts_ms is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _replay_row_extras(snapshot: dict[str, Any], variant: str) -> dict[str, Any]:
    return {
        "kind": REPLAY_ROW_KIND,
        "variant": variant,
        "source": snapshot.get("source"),
        "stratum": snapshot.get("stratum"),
        "stratum_target_days": snapshot.get("stratum_target_days"),
        "stratum_gap_days": snapshot.get("stratum_gap_days"),
        "snapshot_ts_ms": snapshot.get("snapshot_ts_ms"),
        "mid_source": snapshot.get("mid_source"),
        "outcome": snapshot.get("outcome"),
        "end_date": snapshot.get("end_date"),
        "as_of_date": snapshot_as_of_date(snapshot) if variant == "as_of" else None,
    }


async def run_replay(args: argparse.Namespace) -> int:
    ArbConfig.from_env(args.dotenv_path, require_wallet=False)  # 只为 load_dotenv 的副作用
    snapshots = read_ndjson(Path(args.snapshots))
    if not snapshots:
        LOG.error("快照文件为空或不存在：%s（先跑 analysis/build_typesafe_replay_snapshots.py）", args.snapshots)
        return 2
    if args.source:
        snapshots = [s for s in snapshots if s.get("source") == args.source]
    snapshots = [s for s in snapshots if s.get("outcome") in (0, 1)]
    if args.limit > 0:
        snapshots = snapshots[: args.limit]

    variants = ["plain", "as_of"] if args.variant == "both" else [args.variant]
    output = Path(args.output)
    run_id = make_run_id()

    ts_config = TypeSafeConfig.from_env()
    client = TypeSafeJevClient(ts_config, telemetry_sink=_telemetry_sink(Path(args.requests_telemetry)))
    LOG.info(
        "replay run_id=%s model=%s snapshots=%d variants=%s output=%s",
        run_id, ts_config.model, len(snapshots), variants, output,
    )
    started = time.time()
    written = 0
    errors = 0
    try:
        for round_id, variant in enumerate(variants, start=1):
            candidates: list[dict[str, Any]] = []
            usable: list[dict[str, Any]] = []
            for snap in snapshots:
                as_of = snapshot_as_of_date(snap) if variant == "as_of" else None
                if variant == "as_of" and not as_of:
                    LOG.warning("跳过缺 snapshot_ts_ms 的快照 cid=%s", str(snap.get("condition_id"))[:10])
                    continue
                candidate = build_replay_candidate(snap, as_of_date=as_of)
                if candidate["days_to_resolution"] is None:
                    LOG.warning("跳过缺 days_to_resolution 的快照 cid=%s", candidate["condition_id"][:10])
                    continue
                candidates.append(candidate)
                usable.append(snap)
            if not candidates:
                continue
            rows = await score_candidates(
                client,
                candidates,
                run_id=run_id,
                round_id=round_id,
                feeds=[],  # 历史新闻无存档，replay 恒定无新闻
                concurrency=args.concurrency,
                news_per_market=0,
            )
            for row, snap in zip(rows, usable):
                row.update(_replay_row_extras(snap, variant))
            append_ndjson(output, rows)
            written += len(rows)
            errors += sum(1 for r in rows if r.get("error"))
            LOG.info("variant=%s scored=%d errors=%d", variant, len(rows), sum(1 for r in rows if r.get("error")))
    finally:
        await client.aclose()

    summary = {
        "generated_at": time.time(),
        "run_id": run_id,
        "elapsed_sec": round(time.time() - started, 1),
        "snapshots": len(snapshots),
        "variants": variants,
        "rows_written": written,
        "errors": errors,
        "news": False,  # 无历史新闻存档，不要与带新闻的前向影子混比
        "model": ts_config.model,
        "source_filter": args.source or "",
        "typesafe": client.stats.to_dict(),
        "output": str(output),
    }
    write_json_atomically(Path(args.status_output), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


async def run_scan(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env(args.dotenv_path, require_wallet=False)
    scanner = MarketScanner(config)
    feeds = load_feeds(Path(args.news_feeds_file)) if args.news_feeds_file else []
    output = Path(args.output)
    status_path = Path(args.status_output)
    run_id = make_run_id()

    client: Optional[TypeSafeJevClient] = None
    model_label = "(dry-run)"
    if not args.dry_run:
        # dry-run 只看候选，不需要 API key
        ts_config = TypeSafeConfig.from_env()
        client = TypeSafeJevClient(ts_config, telemetry_sink=_telemetry_sink(Path(args.requests_telemetry)))
        model_label = ts_config.model
    LOG.info("run_id=%s model=%s output=%s feeds=%d", run_id, model_label, output, len(feeds))
    round_id = 0
    try:
        while True:
            round_id += 1
            round_started = time.time()
            markets = await asyncio.to_thread(
                scanner.fetch_active_markets,
                limit=args.fetch_limit,
                min_liquidity=args.min_liquidity,
                min_volume_24h=args.min_volume_24h,
            )
            candidates, skips = select_candidates(
                markets,
                now=round_started,
                limit=args.limit,
                min_days=args.min_days_to_resolution,
                max_days=args.max_days_to_resolution,
            )
            rows: list[dict[str, Any]] = []
            if client is None:
                for c in candidates:
                    print(json.dumps({k: c[k] for k in ("condition_id", "category", "days_to_resolution", "market_mid", "question")}, ensure_ascii=False))
            elif candidates:
                rows = await score_candidates(
                    client,
                    candidates,
                    run_id=run_id,
                    round_id=round_id,
                    feeds=feeds,
                    concurrency=args.concurrency,
                    news_per_market=args.news_per_market,
                )
                append_ndjson(output, rows)
            errors = sum(1 for r in rows if r.get("error"))
            summary = {
                "generated_at": time.time(),
                "run_id": run_id,
                "round_id": round_id,
                "round_sec": round(time.time() - round_started, 1),
                "fetched_markets": len(markets),
                "candidates": len(candidates),
                "scored": len(rows) - errors,
                "errors": errors,
                "skip_reasons": dict(skips),
                "category_counts": dict(Counter(c["category"] for c in candidates)),
                "dry_run": client is None,
                "model": model_label,
                "typesafe": client.stats.to_dict() if client is not None else {},
                "output": str(output),
            }
            write_json_atomically(status_path, summary)
            LOG.info(
                "round=%d fetched=%d candidates=%d scored=%d errors=%d 429=%d cost=$%.4f skips=%s",
                round_id, len(markets), len(candidates), summary["scored"], errors,
                client.stats.rate_limited_429 if client is not None else 0,
                client.stats.cost_usd if client is not None else 0.0,
                dict(skips),
            )
            if args.repeat_interval_sec <= 0 or (args.repeat_count > 0 and round_id >= args.repeat_count):
                break
            await asyncio.sleep(args.repeat_interval_sec)
    finally:
        if client is not None:
            await client.aclose()
    return 0


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def resolve_outcome(raw: dict[str, Any]) -> tuple[Optional[int], str]:
    """从 Gamma market 行判断结算结果：(outcome, status).

    outcome=1 → YES；0 → NO；None → 未结算 / 无法判断。
    status ∈ {resolved, closed_unresolved, open, unparseable}。
    """
    market = _safe_parse_market(raw, context="backfill")
    if market is None:
        return None, "unparseable"
    token = yes_token(market)
    if token is None:
        return None, "unparseable"
    uma_status = str(raw.get("umaResolutionStatus") or "").strip().lower()
    closed = bool(market.closed)
    if token.winner is True:
        return 1, "resolved"
    if token.winner is False and any(t.winner is True for t in market.tokens):
        return 0, "resolved"
    price = _coerce_float(token.price)
    if closed or uma_status == "resolved":
        if price is not None and price >= 0.99:
            return 1, "resolved"
        if price is not None and price <= 0.01:
            return 0, "resolved"
        return None, "closed_unresolved"
    return None, "open"


def fetch_market_raw(gamma_host: str, condition_id: str, *, timeout: float = 15.0) -> Optional[dict[str, Any]]:
    """按 condition_id 取 Gamma market 行.

    Gamma ``/markets`` 默认只返回未结算市场：已结算的市场不带 ``closed=true``
    会返回空数组（实测 2026-09-19），于是 backfill 永远拿不到 outcome。
    先按默认查（未结算市场走这条），再带 ``closed=true`` 补查一次。
    """
    import requests

    base_params: dict[str, Any] = {"condition_ids": condition_id, "limit": 1}
    for params in (base_params, {**base_params, "closed": "true"}):
        try:
            resp = requests.get(
                f"{gamma_host.rstrip('/')}/markets",
                params=params,
                headers={"User-Agent": "polymarket-arb-typesafe-shadow"},
                timeout=timeout,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as e:
            LOG.warning("Gamma 查询失败 cid=%s closed=%s: %s", condition_id[:10], params.get("closed", ""), e)
            continue
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    return None


def run_backfill(args: argparse.Namespace) -> int:
    config = ArbConfig.from_env(args.dotenv_path, require_wallet=False)
    shadow_rows = read_ndjson(Path(args.input))
    settlements_path = Path(args.settlements_output)
    existing = read_ndjson(settlements_path)
    already = {r.get("condition_id") for r in existing if r.get("outcome") in (0, 1)}
    unique_ids: list[str] = []
    seen: set[str] = set()
    for row in shadow_rows:
        cid = str(row.get("condition_id") or "")
        if cid and cid not in seen:
            seen.add(cid)
            unique_ids.append(cid)
    pending_ids = [cid for cid in unique_ids if cid not in already]
    LOG.info(
        "backfill: shadow rows=%d unique=%d already_settled=%d pending=%d",
        len(shadow_rows), len(unique_ids), len(unique_ids) - len(pending_ids), len(pending_ids),
    )

    out_rows: list[dict[str, Any]] = []
    counts: Counter = Counter()
    for idx, cid in enumerate(pending_ids):
        if args.max_lookups > 0 and idx >= args.max_lookups:
            break
        raw = fetch_market_raw(config.gamma_host, cid)
        time.sleep(max(0.0, args.lookup_interval_sec))  # 未结算/失败也要限速，否则会连打 Gamma
        if raw is None:
            counts["fetch_failed"] += 1
            continue
        outcome, status = resolve_outcome(raw)
        counts[status] += 1
        if outcome is None and not args.record_open:
            continue
        out_rows.append(
            {
                "kind": SETTLEMENT_KIND,
                "ts": time.time(),
                "condition_id": cid,
                "outcome": outcome,
                "status": status,
                "closed": bool(raw.get("closed", False)),
                "end_date": str(raw.get("endDate") or raw.get("end_date_iso") or ""),
                "uma_resolution_status": str(raw.get("umaResolutionStatus") or ""),
            }
        )
    append_ndjson(settlements_path, out_rows)
    print(json.dumps({"pending": len(pending_ids), "written": len(out_rows), "status_counts": dict(counts)}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TypeSafe Jev shadow labelling for Polymarket event markets")
    sub = parser.add_subparsers(dest="kind", required=True)

    p_scan = sub.add_parser("scan", help="Score candidate markets with Jev and append NDJSON rows")
    p_scan.add_argument("--dotenv-path", default=None)
    p_scan.add_argument("--output", default=DEFAULT_OUTPUT)
    p_scan.add_argument("--requests-telemetry", default=DEFAULT_REQUESTS_TELEMETRY)
    p_scan.add_argument("--status-output", default=DEFAULT_STATUS)
    p_scan.add_argument("--limit", type=int, default=100, help="Max markets scored per round")
    p_scan.add_argument("--fetch-limit", type=int, default=500, help="Gamma /markets rows to pull before filtering")
    p_scan.add_argument("--min-liquidity", type=float, default=1000.0)
    p_scan.add_argument("--min-volume-24h", type=float, default=0.0)
    p_scan.add_argument("--min-days-to-resolution", type=float, default=0.25)
    p_scan.add_argument("--max-days-to-resolution", type=float, default=45.0)
    p_scan.add_argument("--news-feeds-file", default=None, help="research_feeds.json; omit to skip news enrichment")
    p_scan.add_argument("--news-per-market", type=int, default=2, help="Items per feed per market")
    p_scan.add_argument("--concurrency", type=int, default=4)
    p_scan.add_argument("--repeat-interval-sec", type=float, default=0.0, help="0 = run once")
    p_scan.add_argument("--repeat-count", type=int, default=0, help="Stop after N rounds (0 = unlimited)")
    p_scan.add_argument("--dry-run", action="store_true", help="Print candidates, no API calls")

    p_backfill = sub.add_parser("backfill", help="Fill settlement outcomes for previously scored markets")
    p_backfill.add_argument("--dotenv-path", default=None)
    p_backfill.add_argument("--input", default=DEFAULT_OUTPUT)
    p_backfill.add_argument("--settlements-output", default=DEFAULT_SETTLEMENTS)
    p_backfill.add_argument("--lookup-interval-sec", type=float, default=0.2)
    p_backfill.add_argument("--max-lookups", type=int, default=0, help="0 = unlimited")
    p_backfill.add_argument("--record-open", action="store_true", help="Also write rows for still-open markets")

    p_replay = sub.add_parser("replay", help="Score historical snapshots (already settled markets) with Jev")
    p_replay.add_argument("--dotenv-path", default=None)
    p_replay.add_argument("--snapshots", default=DEFAULT_REPLAY_SNAPSHOTS,
                          help="analysis/build_typesafe_replay_snapshots.py 的输出")
    p_replay.add_argument("--output", default=DEFAULT_REPLAY_OUTPUT)
    p_replay.add_argument("--status-output", default=DEFAULT_REPLAY_STATUS)
    p_replay.add_argument("--requests-telemetry", default=DEFAULT_REPLAY_REQUESTS)
    p_replay.add_argument("--variant", choices=("plain", "as_of", "both"), default="both",
                          help="plain=不给时点提示；as_of=state 加 as_of_date 并要求只用该日期前信息")
    p_replay.add_argument("--source", default="", help="只跑某个来源（tick / hf），留空跑全部")
    p_replay.add_argument("--limit", type=int, default=0, help="只跑前 N 个快照（0 = 全部），用于小样本试跑")
    p_replay.add_argument("--concurrency", type=int, default=4)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.kind == "scan":
        return asyncio.run(run_scan(args))
    if args.kind == "backfill":
        return run_backfill(args)
    if args.kind == "replay":
        return asyncio.run(run_replay(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
