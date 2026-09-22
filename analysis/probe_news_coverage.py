"""信息层体检：已结算市场在结算前的新闻覆盖到底有没有判断结果所需的信息.

回答"给 Jev 喂信息是否可行"的前置问题——不是模型问题，是信息问题。对每个已结算市场，
取它在 as-of 时刻（默认用离结算最近的那个快照）之前 N 天的 GDELT 新闻标题，统计覆盖度，
并抽样导出供人工判读。

    python analysis/probe_news_coverage.py --limit 5            # 先试几个看查询词好不好
    python analysis/probe_news_coverage.py                      # 全量
    python analysis/probe_news_coverage.py --sample 20 --sample-out data/typesafe_replay/news_sample.md

GDELT DOC 2.0（免费、无 key）按 startdatetime/enddatetime 精确截断，返回 seendate 到秒。
**as-of 之后的文章一律硬过滤**（不只依赖 API 的 enddatetime）——任何一条越界都会让下游变成
look-ahead，这是本仓库踩过的坑。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.shadow_typesafe_baselines import _strip_dates  # noqa: E402

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_SNAPSHOTS = "data/typesafe_replay/replay_snapshots.jsonl"
DEFAULT_OUTPUT = "data/typesafe_replay/news_coverage.ndjson"

# 市场文本里的模板词，对检索没贡献反而会把结果打成 0
_STOPWORDS = {
    "will", "would", "the", "a", "an", "any", "be", "is", "are", "was", "were", "do", "does", "did",
    "by", "in", "on", "at", "of", "to", "for", "from", "with", "and", "or", "but", "if", "then",
    "before", "after", "during", "between", "than", "as", "that", "this", "these", "those", "there",
    "happen", "happens", "occur", "occurs", "have", "has", "had", "get", "gets", "perform", "performs",
    "next", "first", "second", "round", "market", "resolve", "resolves", "yes", "no", "out",
    "who", "what", "when", "which", "how", "many", "more", "most", "least", "other", "s",
    # 姓名里的连接词（"Abelardo de la Espriella"），单独当检索词是噪声
    "de", "la", "le", "del", "della", "di", "du", "van", "von", "der", "den", "el", "al", "bin", "ibn",
}
# 这些词单独没意义，但和专有名词一起能收窄检索
_WEAK = {"win", "wins", "won", "launch", "launches", "token", "election", "elections", "sells", "sell"}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'&.-]*")


def build_queries(question: str, event_title: str = "") -> list[str]:
    """从市场文本抽检索词，从严到宽给几个候选（命中即止）.

    GDELT 默认把关键词 AND 起来，词一多就必然 0 命中，所以要有递进放宽。
    """
    text = _strip_dates(f"{question} {event_title}".strip())
    words = [w for w in _WORD_RE.findall(text)]
    # 专有名词：非首词且首字母大写（市场标题基本都是 "Will X ..." 句式）
    proper: list[str] = []
    others: list[str] = []
    for idx, word in enumerate(words):
        low = word.lower().strip(".'-&")
        if not low or low in _STOPWORDS or len(low) < 2:
            continue
        if word[0].isupper() and idx > 0:
            if low not in {p.lower() for p in proper}:
                proper.append(word)
        elif low not in _WEAK and low not in {o.lower() for o in others}:
            others.append(word)

    # 非专有名词里长词更可能是内容词（grocery > opens），短词放后面当兜底
    original_order = {w: i for i, w in enumerate(others)}
    others.sort(key=lambda w: (-len(w), original_order[w]))

    def _q(terms: list[str]) -> str:
        out: list[str] = []
        for term in terms:
            # 连字符/撇号在 GDELT 查询语法里不可靠，拆成短语并加引号
            clean = re.sub(r"[-'&.]+", " ", term).strip()
            if not clean:
                continue
            out.append(f'"{clean}"' if " " in clean else clean)
        return " ".join(out)

    queries: list[str] = []
    if len(proper) >= 2:
        queries.append(_q(proper[:3]))
        queries.append(_q(proper[:2]))
    if proper and others:
        queries.append(_q(proper[:1] + others[:1]))
    if len(others) >= 2:
        queries.append(_q(others[:2]))
    # 去重保序；**单词查询一律丢弃**——实测退化到 "Bitcoin" 这种泛词后，
    # 召回的是"Akwesasne 部落议会""OKX 下载"这类完全无关的条目，
    # 喂给下游比 0 命中更糟（噪声会被当成证据）。
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = q.strip()
        if not q or q in seen:
            continue
        if len(q.replace('"', "").split()) < 2:
            continue
        seen.add(q)
        out.append(q)
    return out


def gdelt_search(
    query: str,
    *,
    start: datetime,
    end: datetime,
    max_records: int,
    timeout: float,
    session: requests.Session,
    max_retries: int = 3,
    backoff_sec: float = 8.0,
) -> tuple[list[dict[str, Any]], str]:
    """查 GDELT；429 要退避重试.

    免费接口限流很紧（实测 1s 间隔就吃 429）。**不重试的话"0 命中"会和"被限流"混在一起**，
    直接把信息层体检的结论做成假阴性——这里把 429 单独作为错误码返回，调用方要区分对待。
    """
    params = {
        "query": f"{query} sourcelang:english",
        "mode": "ArtList",
        "maxrecords": max_records,
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        "format": "json",
        "sort": "DateDesc",
    }
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(GDELT_URL, params=params, timeout=timeout)
        except Exception as e:
            last_err = f"request_failed: {type(e).__name__}"
            if attempt < max_retries:
                time.sleep(backoff_sec * (attempt + 1))
                continue
            return [], last_err
        if resp.status_code == 429:
            last_err = "http_429"
            if attempt < max_retries:
                time.sleep(backoff_sec * (attempt + 1))
                continue
            return [], last_err
        if resp.status_code != 200:
            return [], f"http_{resp.status_code}"
        text = resp.text.strip()
        if not text:
            return [], "empty_body"
        try:
            payload = resp.json()
        except ValueError:
            # GDELT 对语法错误返回纯文本
            return [], f"non_json: {text[:80]}"
        articles = payload.get("articles") or []
        return (articles if isinstance(articles, list) else []), ""
    return [], last_err


def parse_seendate(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value).strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def probe_market(
    market: dict[str, Any],
    *,
    window_days: float,
    max_records: int,
    timeout: float,
    sleep_sec: float,
    session: requests.Session,
    max_queries: int = 2,
) -> dict[str, Any]:
    as_of = datetime.fromtimestamp(int(market["snapshot_ts_ms"]) / 1000, tz=timezone.utc)
    start = as_of - timedelta(days=window_days)
    queries = build_queries(market["question"], market.get("event_title") or "")[:max_queries]
    attempts: list[dict[str, Any]] = []
    articles: list[dict[str, Any]] = []
    used_query = ""
    for query in queries:
        found, err = gdelt_search(
            query, start=start, end=as_of, max_records=max_records, timeout=timeout, session=session
        )
        time.sleep(sleep_sec)
        attempts.append({"query": query, "n": len(found), "error": err})
        if found:
            articles = found
            used_query = query
            break

    # 硬过滤：只保留 as-of 之前的文章（不只信 API 的 enddatetime）
    kept: list[dict[str, Any]] = []
    leaked = 0
    undated = 0
    for art in articles:
        seen = parse_seendate(art.get("seendate", ""))
        if seen is None:
            undated += 1
            continue
        if seen > as_of:
            leaked += 1
            continue
        kept.append({
            "seendate": art.get("seendate"),
            "domain": art.get("domain"),
            "title": (art.get("title") or "").strip(),
            "url": art.get("url"),
        })
    kept.sort(key=lambda a: str(a.get("seendate")), reverse=True)
    # 三种"0 条"要分开，混在一起会把脚本自身的局限说成数据结论：
    #   no_query     — 检索词一个都没生成（市场文本全小写/只有一个词），**从未真正查过**
    #   rate_limited — 候选词全部被 429/报错打回，未测到
    #   ok + 0 条    — 真的查了、窗口内没新闻
    if not attempts:
        status = "no_query"
    elif not kept and all(a["error"] for a in attempts):
        status = "rate_limited"
    else:
        status = "ok"
    return {
        "status": status,
        "condition_id": market["condition_id"],
        "question": market["question"],
        "event_id": market.get("event_id"),
        "event_title": market.get("event_title"),
        "category": market.get("category"),
        "source": market.get("source"),
        "outcome": market.get("outcome"),
        "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "days_to_resolution": market.get("days_to_resolution"),
        "market_mid": market.get("market_mid"),
        "query_used": used_query,
        "query_attempts": attempts,
        "n_articles": len(kept),
        "n_leaked_filtered": leaked,
        "n_undated": undated,
        "domains": [d for d, _ in Counter(a["domain"] for a in kept if a.get("domain")).most_common(5)],
        "articles": kept[:20],
    }


def pick_as_of_rows(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个市场取离结算最近的那个快照当 as-of（信息最全的时点，对信息层最有利）."""
    best: dict[str, dict[str, Any]] = {}
    for row in snapshots:
        cid = str(row.get("condition_id") or "")
        if not cid or row.get("outcome") not in (0, 1) or row.get("snapshot_ts_ms") is None:
            continue
        cur = best.get(cid)
        if cur is None or (row.get("days_to_resolution") or 1e9) < (cur.get("days_to_resolution") or 1e9):
            best[cid] = row
    return sorted(best.values(), key=lambda r: str(r.get("category")))


def write_sample_markdown(rows: list[dict[str, Any]], path: Path, *, sample: int) -> None:
    """按类别分层抽样，导出人工判读用的 markdown."""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_cat[str(row.get("category"))].append(row)
    picked: list[dict] = []
    cats = sorted(by_cat, key=lambda c: -len(by_cat[c]))
    while len(picked) < sample and any(by_cat[c] for c in cats):
        for cat in cats:
            if not by_cat[cat] or len(picked) >= sample:
                continue
            # 每类优先取新闻条数多的（最有利于信息层的样本）
            by_cat[cat].sort(key=lambda r: -r["n_articles"])
            picked.append(by_cat[cat].pop(0))

    lines = [
        "# 信息层人工判读样本",
        "",
        "对每个市场：只看 `as_of` 之前的标题，判断**不知道结果的人**能否据此判断结算方向。",
        "三分类：`A 人能判断` / `B 人也判断不出（新闻里没这个信息）` / `C 新闻不相关（检索词问题）`。",
        "`outcome` 列是真实结果，请**先判后看**。",
        "",
    ]
    for idx, row in enumerate(picked, start=1):
        lines += [
            f"## {idx}. [{row['category']}] {row['question']}",
            "",
            f"- as_of `{row['as_of']}`（距结算 {row['days_to_resolution']} 天）｜窗口起点 `{row['window_start']}`",
            f"- 检索词 `{row['query_used'] or '(全部候选都 0 命中)'}`｜新闻 **{row['n_articles']}** 条"
            f"｜越界过滤 {row['n_leaked_filtered']} 条",
            f"- 当时盘口 mid `{row['market_mid']}`｜真实结果 **{'YES' if row['outcome'] == 1 else 'NO'}**（判完再看）",
            "",
        ]
        if not row["articles"]:
            lines += ["  (窗口内没有命中任何新闻)", ""]
            continue
        for art in row["articles"][:8]:
            lines.append(f"  - `{art['seendate']}` [{art['domain']}] {art['title']}")
        lines.append("")
        lines.append("  判读：____")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe historical news coverage for settled markets (GDELT)")
    ap.add_argument("--snapshots", default=DEFAULT_SNAPSHOTS)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--window-days", type=float, default=7.0)
    ap.add_argument("--max-records", type=int, default=25)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--max-queries", type=int, default=2, help="每市场最多试几个检索词（越多越容易吃 429）")
    ap.add_argument("--sleep-sec", type=float, default=7.0, help="GDELT 免费接口限流很紧，实测 1s 就吃 429")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个市场（0=全部）")
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--sample-out", default="data/typesafe_replay/news_sample.md")
    args = ap.parse_args()

    snapshots = []
    path = Path(args.snapshots)
    if not path.exists():
        print(f"快照不存在：{path}")
        return 2
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    markets = pick_as_of_rows(snapshots)
    if args.limit > 0:
        markets = markets[: args.limit]
    print(f"已结算市场 {len(markets)} 个，窗口 {args.window_days} 天，GDELT 间隔 {args.sleep_sec}s", flush=True)

    session = requests.Session()
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    for idx, market in enumerate(markets, start=1):
        row = probe_market(
            market,
            window_days=args.window_days,
            max_records=args.max_records,
            timeout=args.timeout,
            sleep_sec=args.sleep_sec,
            session=session,
            max_queries=args.max_queries,
        )
        rows.append(row)
        if idx % 10 == 0 or idx == len(markets):
            print(f"  {idx}/{len(markets)} elapsed={time.time()-t0:.0f}s", flush=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    tested = [r for r in rows if r.get("status") == "ok"]
    rate_limited = [r for r in rows if r.get("status") == "rate_limited"]
    no_query = [r for r in rows if r.get("status") == "no_query"]
    counts = [r["n_articles"] for r in tested]
    zero = sum(1 for c in counts if c == 0)
    print("\n### 新闻覆盖度")
    if rate_limited:
        print(f"⚠ {len(rate_limited)} 个市场候选词全部被限流/报错，**未测到**，不计入统计")
    if no_query:
        print(f"⚠ {len(no_query)} 个市场一个检索词都没生成出来（市场文本全小写/只有一个词），"
              f"**从未真正查过**，不计入统计——这是脚本局限，不是「窗口内没有新闻」")
    if not counts:
        print("没有任何市场查询成功，把 --sleep-sec 调大后重跑")
        return 1
    print(f"已测市场 {len(tested)}｜真 0 命中 {zero}（{zero/len(tested):.0%}）｜"
          f"中位 {statistics.median(counts):.0f}｜均值 {statistics.mean(counts):.1f}｜最大 {max(counts)}")
    print(f"越界（seendate > as_of）被硬过滤：{sum(r['n_leaked_filtered'] for r in rows)} 条；"
          f"无日期丢弃：{sum(r['n_undated'] for r in rows)} 条")
    by_cat: dict[str, list[int]] = defaultdict(list)
    for row in tested:
        by_cat[str(row.get("category"))].append(row["n_articles"])
    # 条数多 ≠ 有信息：实测 "Lindsey Horvath" 那类人名检索会把整座城市的本地新闻
    # （WeHo Pride / Hollywood Fringe）都召回来，25 条里一条都不指向结算方向。
    # 所以这张表只回答"有没有新闻"，"有没有信息"必须人工判读 sample。
    print("\n| category | 市场 | 0命中 | 中位新闻数 | 最大 |")
    print("|---|---|---|---|---|")
    for cat, vals in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        print(f"| {cat} | {len(vals)} | {sum(1 for v in vals if v == 0)} | {statistics.median(vals):.0f} | {max(vals)} |")
    tried = Counter()
    for row in rows:
        tried[len(row["query_attempts"])] += 1
    print(f"\n检索词递进次数分布（1=第一个词组就命中）：{dict(sorted(tried.items()))}")
    errs = Counter(a["error"] for r in rows for a in r["query_attempts"] if a["error"])
    if errs:
        print(f"GDELT 错误：{dict(errs)}")

    if args.sample > 0:
        sample_path = Path(args.sample_out)
        write_sample_markdown(rows, sample_path, sample=args.sample)
        print(f"\n人工判读样本已写入 {sample_path}")
    print(f"明细 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
