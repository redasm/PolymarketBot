"""评估 TypeSafe Jev 历史回放：Brier / 校准 / 事件内一致性 / edge 模拟 / 污染探针.

打分脚本；结论见 doc/zh/research-findings.md（回放版；前向影子版复用同样的表）。

    python analysis/eval_typesafe_replay.py
    python analysis/eval_typesafe_replay.py --input data/typesafe_replay/typesafe_replay.ndjson --min-conf 0.1

**这份报告不产出 go/no-go 判据**：样本量和污染两条限制见输出顶部的 GUARDRAILS 段。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_arb.models import FeeStructure  # noqa: E402

DEFAULT_INPUT = "data/typesafe_replay/typesafe_replay.ndjson"
# 默认 taker 费率：Polymarket V2 是 rate·p·(1-p)，普通事件市场 rate 用配置默认值
# （crypto up/down 那类 feeSchedule.rate=0.07 的市场已被 is_numeric_market 过滤掉）
DEFAULT_FEE_RATE = 0.005
THETAS = (0.05, 0.10, 0.15, 0.20)


def days_bucket(days: Optional[float]) -> str:
    if days is None:
        return "?"
    if days < 2:
        return "<2d"
    if days < 14:
        return "2-14d"
    if days < 60:
        return "14-60d"
    return ">60d"


def brier(pairs: list[tuple[float, int]]) -> Optional[float]:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _fmt(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value:.{digits}f}"


def _table(title: str, header: list[str], rows: list[list[Any]]) -> None:
    print(f"\n### {title}")
    if not rows:
        print("(无数据)")
        return
    widths = [len(h) for h in header]
    text_rows = [[str(c) for c in row] for row in rows]
    for row in text_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(header)) + " |")
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in text_rows:
        print("| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |")


def load_rows(path: Path, *, min_conf: float, mid_sources: set[str]) -> tuple[list[dict], dict[str, int]]:
    rows: list[dict] = []
    drops: Counter = Counter()
    if not path.exists():
        print(f"输入不存在：{path}")
        return rows, dict(drops)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                drops["bad_json"] += 1
                continue
            if row.get("error"):
                drops["api_error"] += 1
                continue
            if row.get("jev_p_yes") is None:
                drops["no_p_yes"] += 1
                continue
            if row.get("outcome") not in (0, 1):
                drops["no_outcome"] += 1
                continue
            conf = row.get("jev_confidence")
            if min_conf > 0 and (conf is None or float(conf) < min_conf):
                drops["low_confidence"] += 1
                continue
            # 只在行里确实带 mid_source 时才过滤，避免任何 schema 漂移把全部行静默丢掉
            if mid_sources and row.get("mid_source") is not None and row["mid_source"] not in mid_sources:
                drops[f"mid_source_{row['mid_source']}"] += 1
                continue
            rows.append(row)
    return rows, dict(drops)


def section_coverage(all_rows: list[dict], kept: list[dict], drops: dict[str, int]) -> None:
    print("\n### 覆盖与丢弃")
    print(f"NDJSON 行数 {len(all_rows)}，进入统计 {len(kept)}，丢弃 {dict(drops)}")
    if not all_rows:
        return
    lat = [float(r["latency_ms"]) for r in all_rows if r.get("latency_ms") is not None]
    if lat:
        lat.sort()
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]  # noqa: E731
        print(f"延迟 ms: min {lat[0]:.0f} / p50 {p(0.5):.0f} / p90 {p(0.9):.0f} / max {lat[-1]:.0f}")
    models = Counter(str(r.get("jev_model")) for r in all_rows)
    print(f"jev_model: {dict(models)}")
    toks = [int(r["input_tokens"]) for r in all_rows if r.get("input_tokens") is not None]
    if toks:
        print(f"input_tokens 合计 {sum(toks):,} → 估算成本 ${sum(toks) * 0.042 / 1e6:.4f}")
    _table(
        "样本结构",
        ["variant", "source", "rows", "markets", "events", "outcome=1"],
        [
            [
                v,
                s,
                len(grp),
                len({r["condition_id"] for r in grp}),
                len({r.get("event_id") for r in grp}),
                sum(1 for r in grp if r["outcome"] == 1),
            ]
            for (v, s), grp in sorted(_group(kept, lambda r: (r.get("variant"), r.get("source"))).items())
        ],
    )


def _group(rows: list[dict], key) -> dict[Any, list[dict]]:
    out: dict[Any, list[dict]] = defaultdict(list)
    for row in rows:
        out[key(row)].append(row)
    return out


def section_brier(rows: list[dict]) -> None:
    """Jev vs 盘口的 Brier；只用双边盘口的行做对比（单边没有可信 mid）."""
    def _stats(grp: list[dict]) -> list[Any]:
        jev_pairs = [(float(r["jev_p_yes"]), int(r["outcome"])) for r in grp]
        mkt_pairs = [
            (float(r["market_mid"]), int(r["outcome"]))
            for r in grp
            if r.get("market_mid") is not None
        ]
        jev_b = brier(jev_pairs)
        # 与盘口比必须同一子集，否则是拿不同样本比分数
        jev_on_mkt = brier(
            [(float(r["jev_p_yes"]), int(r["outcome"])) for r in grp if r.get("market_mid") is not None]
        )
        mkt_b = brier(mkt_pairs)
        skill = None
        if jev_on_mkt is not None and mkt_b is not None and mkt_b > 0:
            skill = 1 - jev_on_mkt / mkt_b
        base = sum(int(r["outcome"]) for r in grp) / len(grp)
        return [
            len(grp),
            len(mkt_pairs),
            _fmt(jev_b),
            _fmt(jev_on_mkt),
            _fmt(mkt_b),
            _fmt(skill, 3),
            _fmt(base, 3),
        ]

    header = ["分组", "n", "n(有mid)", "Brier_jev", "Brier_jev|mid", "Brier_mkt", "skill", "实际YES率"]
    for label, keyfn in (
        ("按 variant", lambda r: str(r.get("variant"))),
        ("按 variant × source", lambda r: f"{r.get('variant')} × {r.get('source')}"),
        ("按 variant × category", lambda r: f"{r.get('variant')} × {r.get('category')}"),
        ("按 variant × 剩余天数", lambda r: f"{r.get('variant')} × {days_bucket(r.get('days_to_resolution'))}"),
    ):
        groups = _group(rows, keyfn)
        _table(
            f"Brier（{label}）— skill>0 才是 Jev 优于盘口",
            header,
            [[k] + _stats(v) for k, v in sorted(groups.items())],
        )


def section_calibration(rows: list[dict], *, buckets: int) -> None:
    edges = [i / buckets for i in range(buckets + 1)]
    for variant, grp in sorted(_group(rows, lambda r: str(r.get("variant"))).items()):
        table = []
        for i in range(buckets):
            lo, hi = edges[i], edges[i + 1]
            sel = [r for r in grp if lo <= float(r["jev_p_yes"]) < hi or (i == buckets - 1 and float(r["jev_p_yes"]) == 1.0)]
            if not sel:
                table.append([f"[{lo:.1f},{hi:.1f})", 0, "n/a", "n/a", "n/a"])
                continue
            mean_p = statistics.mean(float(r["jev_p_yes"]) for r in sel)
            actual = sum(int(r["outcome"]) for r in sel) / len(sel)
            mids = [float(r["market_mid"]) for r in sel if r.get("market_mid") is not None]
            table.append([
                f"[{lo:.1f},{hi:.1f})",
                len(sel),
                _fmt(mean_p, 3),
                _fmt(actual, 3),
                _fmt(statistics.mean(mids), 3) if mids else "n/a",
            ])
        _table(
            f"校准曲线 variant={variant}（预测均值 vs 实际 YES 率）",
            ["p_yes 桶", "n", "预测均值", "实际YES率", "同桶盘口均值"],
            table,
        )


def section_event_consistency(rows: list[dict], *, top: int) -> None:
    """同一 event 下互斥市场的概率和：盘口 ≈1，Jev 不做跨市场归一化."""
    table = []
    for variant, grp in sorted(_group(rows, lambda r: str(r.get("variant"))).items()):
        # 每个 (event, 市场) 只取剩余天数最小的那条，避免同市场多分层重复计数
        latest: dict[tuple[Any, str], dict] = {}
        for row in grp:
            key = (row.get("event_id"), row["condition_id"])
            cur = latest.get(key)
            if cur is None or (row.get("days_to_resolution") or 1e9) < (cur.get("days_to_resolution") or 1e9):
                latest[key] = row
        by_event: dict[Any, list[dict]] = defaultdict(list)
        for (event_id, _cid), row in latest.items():
            by_event[event_id].append(row)
        multi = {k: v for k, v in by_event.items() if len(v) >= 3}
        for event_id, members in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:top]:
            jev_sum = sum(float(r["jev_p_yes"]) for r in members)
            mids = [float(r["market_mid"]) for r in members if r.get("market_mid") is not None]
            table.append([
                variant,
                str(event_id),
                len(members),
                _fmt(jev_sum, 3),
                _fmt(sum(mids), 3) if mids else "n/a",
                str(members[0].get("event_title") or members[0]["question"])[:44],
            ])
    _table(
        "事件内概率和（互斥候选，理论应 ≈1.0）",
        ["variant", "event_id", "市场数", "Σjev_p_yes", "Σ盘口mid", "事件"],
        table,
    )


def _one_row_per_market(rows: list[dict]) -> list[dict]:
    """同一市场的多个分层不是独立下注，取离结算最近的那条代表该市场."""
    best: dict[str, dict] = {}
    for row in rows:
        cid = str(row["condition_id"])
        cur = best.get(cid)
        if cur is None or (row.get("days_to_resolution") or 1e9) < (cur.get("days_to_resolution") or 1e9):
            best[cid] = row
    return list(best.values())


def section_edge(rows: list[dict], *, fee_rate: float) -> None:
    """按 |jev - mid| > θ 下注：命中率 + 扣 V2 taker 费后的每份期望.

    费用形状 rate·p·(1-p) 来自 ``polymarket_arb/models.py:FeeStructure``（V2，2026-03-30 起），
    不是文档早期写的「2% 名义」。

    两处会把结果看成假阳性的坑，这里都显式拆开：
    - **伪重复**：同一市场的 T-30/T-7/T-1 三条共享同一个结算结果，按行下注会把同一笔赌注算 3 次
      → ``dedup=market`` 行才是可信的那一档；
    - **长尾单笔**：低价 NO 腿一旦命中就 +0.8 以上，几笔就能盖住几十笔小亏
      → 一并给中位数、正收益笔数、去掉最大单笔后的合计。
    """
    fees = FeeStructure(taker_fee_rate=fee_rate)
    table = []
    for variant, grp in sorted(_group(rows, lambda r: str(r.get("variant"))).items()):
        usable = [r for r in grp if r.get("market_mid") is not None]
        for dedup, pool in (("row", usable), ("market", _one_row_per_market(usable))):
            for theta in THETAS:
                picks = [r for r in pool if abs(float(r["jev_p_yes"]) - float(r["market_mid"])) > theta]
                if not picks:
                    table.append([variant, dedup, f"{theta:.2f}", 0, "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"])
                    continue
                hits = 0
                buy_yes = 0
                pnls: list[float] = []
                for row in picks:
                    p_jev = float(row["jev_p_yes"])
                    mid = float(row["market_mid"])
                    outcome = int(row["outcome"])
                    if p_jev > mid:  # 买 YES，成交价近似取 mid（回放没有可信深度，已是乐观假设）
                        entry = mid
                        payoff = 1.0 if outcome == 1 else 0.0
                        hits += int(outcome == 1)
                        buy_yes += 1
                    else:            # 买 NO
                        entry = 1.0 - mid
                        payoff = 1.0 if outcome == 0 else 0.0
                        hits += int(outcome == 0)
                    pnls.append(payoff - entry - fees.estimate_price_fee(entry, size=1.0))
                total = sum(pnls)
                table.append([
                    variant,
                    dedup,
                    f"{theta:.2f}",
                    len(picks),
                    f"{buy_yes}/{len(picks) - buy_yes}",
                    _fmt(hits / len(picks), 3),
                    _fmt(total / len(picks), 4),
                    _fmt(statistics.median(pnls), 4),
                    f"{sum(1 for x in pnls if x > 0)}/{len(pnls)}",
                    _fmt(total - max(pnls), 3),
                ])
    _table(
        f"Edge 模拟（每份 1 股，taker 费率 {fee_rate:g}·p·(1−p)）",
        ["variant", "dedup", "θ", "下注数", "买YES/买NO", "命中率", "均值每份", "中位每份", "正收益笔数", "去掉最大单笔合计"],
        table,
    )
    print("  dedup=row 会把同一市场的多个分层当独立赌注（伪重复），看结论只看 dedup=market 那几行。")
    print("  「均值每份」为正而「中位每份」为负 = 收益全靠极少数长尾单笔，不是可复现的 edge。")


def section_contamination(rows: list[dict]) -> None:
    """污染探针：as_of 提示是否改变答案，以及"极端且正确"的比例是否离谱."""
    by_variant = _group(rows, lambda r: str(r.get("variant")))
    plain = {(r["condition_id"], r.get("snapshot_ts_ms")): r for r in by_variant.get("plain", [])}
    as_of = {(r["condition_id"], r.get("snapshot_ts_ms")): r for r in by_variant.get("as_of", [])}
    shared = sorted(set(plain) & set(as_of))
    print("\n### 污染探针")
    if shared:
        diffs = [float(as_of[k]["jev_p_yes"]) - float(plain[k]["jev_p_yes"]) for k in shared]
        flips = sum(
            1 for k in shared
            if (float(plain[k]["jev_p_yes"]) > 0.5) != (float(as_of[k]["jev_p_yes"]) > 0.5)
        )
        big = sum(1 for d in diffs if abs(d) > 0.1)
        mae = statistics.mean(abs(d) for d in diffs)
        print(
            f"plain vs as_of 配对 {len(shared)} 条：MAE {mae:.4f}，"
            f"最大 |Δ| {max(abs(d) for d in diffs):.4f}，|Δ|>0.1 的 {big} 条，"
            f"跨 0.5 翻转 {flips} 条"
        )
        # 结论必须由数据决定：提示无效 + 判别力高才是污染；提示无效 + 判别力低只说明提示没用
        plain_auc = _auc([(float(r["jev_p_yes"]), int(r["outcome"])) for r in by_variant.get("plain", [])])
        if mae < 0.05 and (plain_auc or 0) >= 0.85:
            print("  → as_of 提示几乎不改变答案，且判别力异常高：污染嫌疑高（模型在回忆结果，不是在推理当时信息）。")
        elif mae < 0.05:
            print(
                f"  → as_of 提示几乎不改变答案（MAE {mae:.3f}），但判别力并不高（AUC {_fmt(plain_auc, 3)}）："
                "看不出记忆结果的迹象，更像是提示对这个模型无效。"
            )
        else:
            print("  → as_of 提示确实改变了答案，可用来分离「有无时点约束」两种用法。")
    else:
        print("没有 plain/as_of 配对行（--variant both 才有）")

    table = []
    for variant, grp in sorted(by_variant.items()):
        extreme = [r for r in grp if float(r["jev_p_yes"]) > 0.9 or float(r["jev_p_yes"]) < 0.1]
        extreme_right = sum(
            1 for r in extreme
            if (float(r["jev_p_yes"]) > 0.9 and r["outcome"] == 1) or (float(r["jev_p_yes"]) < 0.1 and r["outcome"] == 0)
        )
        auc = _auc([(float(r["jev_p_yes"]), int(r["outcome"])) for r in grp])
        mkt_auc = _auc([
            (float(r["market_mid"]), int(r["outcome"])) for r in grp if r.get("market_mid") is not None
        ])
        table.append([
            variant,
            len(grp),
            len(extreme),
            _fmt(extreme_right / len(extreme), 3) if extreme else "n/a",
            _fmt(auc, 3),
            _fmt(mkt_auc, 3),
        ])
    _table(
        "极端预测与判别力（AUC 接近 1 且远超盘口 = 记忆而非预测的信号）",
        ["variant", "n", "极端预测数", "极端命中率", "AUC_jev", "AUC_mkt"],
        table,
    )


def _auc(pairs: list[tuple[float, int]]) -> Optional[float]:
    pos = [p for p, y in pairs if y == 1]
    neg = [p for p, y in pairs if y == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else (0.5 if a == b else 0.0)
    return wins / (len(pos) * len(neg))


def section_confidence(rows: list[dict]) -> None:
    table = []
    for variant, grp in sorted(_group(rows, lambda r: str(r.get("variant"))).items()):
        for label, sel in (
            ("conf<0.1", [r for r in grp if (r.get("jev_confidence") or 0) < 0.1]),
            ("0.1–0.5", [r for r in grp if 0.1 <= (r.get("jev_confidence") or 0) < 0.5]),
            ("conf≥0.5", [r for r in grp if (r.get("jev_confidence") or 0) >= 0.5]),
        ):
            if not sel:
                table.append([variant, label, 0, "n/a", "n/a", "n/a"])
                continue
            pairs = [(float(r["jev_p_yes"]), int(r["outcome"])) for r in sel]
            mkt = [
                (float(r["market_mid"]), int(r["outcome"])) for r in sel if r.get("market_mid") is not None
            ]
            table.append([
                variant,
                label,
                len(sel),
                _fmt(brier(pairs)),
                _fmt(brier(mkt)),
                _fmt(statistics.mean(abs(float(r["jev_p_yes"]) - 0.5) for r in sel), 3),
            ])
    _table(
        "按 Jev confidence 分层",
        ["variant", "confidence", "n", "Brier_jev", "Brier_mkt", "平均|p−0.5|"],
        table,
    )


def section_ambiguity(rows: list[dict]) -> None:
    vals = [float(r["jev_ambiguous"]) for r in rows if r.get("jev_ambiguous") is not None]
    if not vals:
        return
    print("\n### 结算措辞歧义（jev_ambiguous）")
    print(
        f"n={len(vals)} 均值 {statistics.mean(vals):.3f} 中位 {statistics.median(vals):.3f} "
        f"最大 {max(vals):.3f}；>0.5 的 {sum(1 for v in vals if v > 0.5)} 条"
    )
    top = sorted(rows, key=lambda r: -(r.get("jev_ambiguous") or 0))[:5]
    for row in top:
        print(f"  {row.get('jev_ambiguous'):.2f}  {str(row['question'])[:78]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate the TypeSafe Jev replay backtest")
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--min-conf", type=float, default=0.0, help="丢掉 confidence 低于该值的行（0=不过滤）")
    ap.add_argument("--mid-sources", default="two_sided,one_sided_ask,crossed_mid",
                    help="保留哪些 mid_source 的行，逗号分隔；建议对比时只留 two_sided")
    ap.add_argument("--calibration-buckets", type=int, default=5)
    ap.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE)
    ap.add_argument("--top-events", type=int, default=8)
    args = ap.parse_args()

    path = Path(args.input)
    raw_rows = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        raw_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    mid_sources = {s.strip() for s in args.mid_sources.split(",") if s.strip()}
    rows, drops = load_rows(path, min_conf=args.min_conf, mid_sources=mid_sources)

    print("=" * 78)
    print("TypeSafe Jev 历史回放评估")
    print("=" * 78)
    markets = len({r["condition_id"] for r in rows})
    events = len({r.get("event_id") for r in rows})
    print("\n### GUARDRAILS（先读这段再看任何数字）")
    print(f"- 样本：{len(rows)} 行 / {markets} 个市场 / {events} 个 event_id —— 远低于 §4 的「单类别 ≥50 已结算样本」")
    print("- 同一 event 下的互斥候选强相关，独立信息量远小于行数；跨 event 的类别集中在少数几场选举/赛事")
    print("- Jev 训练截止官方未公布，本批事件 2026-01 ~ 2026-06 已结算，且 state 里无新闻 → 模型只能靠参数化知识作答，")
    print("  「预测」与「记忆」不可分。本报告用途：plumbing 体检 + prompt 措辞体检 + 污染探针，**不是 go/no-go 判据**。")

    section_coverage(raw_rows, rows, drops)
    if not rows:
        print("\n没有可用行，先跑 scripts/shadow_typesafe_baselines.py replay")
        return 1
    section_brier(rows)
    section_calibration(rows, buckets=args.calibration_buckets)
    section_event_consistency(rows, top=args.top_events)
    section_edge(rows, fee_rate=args.fee_rate)
    section_contamination(rows)
    section_confidence(rows)
    section_ambiguity(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
