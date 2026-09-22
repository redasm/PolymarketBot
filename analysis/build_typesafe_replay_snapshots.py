"""为 TypeSafe Jev 历史回放建快照（纯离线，不调 Jev，不花钱）.

对应 doc/zh/research-findings.md 的 TypeSafe 历史回放。三段式，避免把整条 tick 序列读进内存：

1. 枚举：流式扫 tick NDJSON，抽出所有 condition_id + 市场文本，过滤掉数值类市场
2. 打标：按 condition_id 查 Gamma 拿 description / endDate / 结算结果，只留已结算的
3. 取样：第二遍扫 tick，每个已结算市场按 T-30d / T-7d / T-1d 各取最近的一条 YES 侧盘口

用法::

    python analysis/build_typesafe_replay_snapshots.py \
        --ticks-dir "E:/PolymarketData/*/data/ticks" \
        --hf-snapshots "E:/PolymarketData/6.6-6.12/hf_crypto_resolved/market_snapshots.jsonl"

输出 ``data/typesafe_replay/replay_snapshots.jsonl``（刻意不放 data/telemetry：那里有
DataJanitor 的 14 天淘汰 + 2GB 上限，回测数据要长期保留）。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.shadow_typesafe_baselines import (  # noqa: E402
    REPLAY_STRATA_DAYS,
    fetch_market_raw,
    guess_category,
    is_numeric_market,
    parse_iso_ts,
    pick_strata,
    resolve_outcome,
)

CID_RE = re.compile(r'"condition_id":"(0x[0-9a-f]+)"')
TS_RE = re.compile(r'"ts_ms":(\d+)')
ROLE_RE = re.compile(r'"outcome_role":"([^"]*)"')

DEFAULT_OUTPUT = "data/typesafe_replay/replay_snapshots.jsonl"
GAMMA_HOST = "https://gamma-api.polymarket.com"


def _chunk_order(path: str) -> tuple[str, int]:
    """滚动 tick 文件的时间顺序（照抄 analysis/t0_tick_scan.py:51 的理由）.

    recorder 把 ``2026-06-20.ndjson`` 滚成 ``2026-06-20.1.ndjson``，普通字符串排序会
    把第二块排到基础文件前面，倒放几个小时。
    """
    name = os.path.basename(path)
    stem = name[: -len(".ndjson")] if name.endswith(".ndjson") else name
    day, _, chunk = stem.partition(".")
    return day, int(chunk) if chunk.isdigit() else 0


def expand_tick_files(patterns: Iterable[str]) -> list[str]:
    files: list[str] = []
    for pattern in patterns:
        if os.path.isdir(pattern):
            files.extend(glob.glob(os.path.join(pattern, "*.ndjson")))
        else:
            hits = glob.glob(pattern)
            for hit in hits:
                if os.path.isdir(hit):
                    files.extend(glob.glob(os.path.join(hit, "*.ndjson")))
                elif hit.endswith(".ndjson"):
                    files.append(hit)
    return sorted(set(files), key=_chunk_order)


# ---------------------------------------------------------------------------
# 1. 枚举
# ---------------------------------------------------------------------------


def enumerate_tick_markets(files: list[str]) -> dict[str, dict[str, Any]]:
    """扫所有 tick 文件，返回 ``{condition_id: {question, slug, event_id, event_title}}``.

    只对首次出现的 condition_id 做 ``json.loads``（正则抽 cid 判重），全量 9.6GB 约 40 秒。
    """
    markets: dict[str, dict[str, Any]] = {}
    lines = 0
    t0 = time.time()
    for path in files:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                lines += 1
                m = CID_RE.search(line)
                if m is None:
                    continue
                cid = m.group(1)
                if cid in markets:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                markets[cid] = {
                    "question": str(row.get("question") or ""),
                    "slug": str(row.get("slug") or ""),
                    "event_id": row.get("event_id"),
                    "event_title": str(row.get("event_title") or ""),
                }
    print(f"[1/3] 扫 {len(files)} 个文件 / {lines:,} 行 / {len(markets)} 个市场，{time.time()-t0:.0f}s", flush=True)
    return markets


# ---------------------------------------------------------------------------
# 2. 打标
# ---------------------------------------------------------------------------


def label_markets(
    candidates: dict[str, dict[str, Any]],
    *,
    gamma_host: str,
    workers: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """按 condition_id 查 Gamma，返回已结算市场的元数据 + skip 计数."""
    skips: dict[str, int] = {}

    def _bump(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    def _work(cid: str) -> tuple[str, Optional[dict[str, Any]], str]:
        raw = fetch_market_raw(gamma_host, cid)
        if raw is None:
            return cid, None, "gamma_not_found"
        outcome, status = resolve_outcome(raw)
        if outcome not in (0, 1):
            return cid, None, status
        end_date = str(raw.get("endDate") or raw.get("end_date_iso") or "")
        end_ts = parse_iso_ts(end_date)
        if end_ts is None:
            return cid, None, "no_end_date"
        return cid, {
            "description": str(raw.get("description") or "").strip()[:4000],
            "end_date": end_date,
            "end_ts_ms": int(end_ts * 1000),
            "outcome": outcome,
        }, status

    labeled: dict[str, dict[str, Any]] = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for cid, meta, status in ex.map(_work, list(candidates)):
            if meta is None:
                _bump(status)
                continue
            labeled[cid] = {**candidates[cid], **meta}
    print(
        f"[2/3] Gamma 打标 {len(candidates)} 个候选 → 已结算 {len(labeled)}，"
        f"skip={skips}，{time.time()-t0:.0f}s",
        flush=True,
    )
    return labeled, skips


# ---------------------------------------------------------------------------
# 3. 取样
# ---------------------------------------------------------------------------


def collect_tick_strata(
    files: list[str],
    labeled: dict[str, dict[str, Any]],
    *,
    targets_days: tuple[float, ...],
) -> dict[str, dict[int, dict[str, Any]]]:
    """第二遍扫 tick：每个已结算市场按目标时点各留最近的一条 YES 侧盘口.

    内存 O(市场数 × 目标数)：对每个 (市场, 目标时点) 只保留当前最优的一条。
    """
    targets: dict[str, list[tuple[str, int]]] = {}
    for cid, meta in labeled.items():
        end_ts = int(meta["end_ts_ms"])
        targets[cid] = [(f"T-{d:g}", end_ts - int(d * 86_400_000)) for d in sorted(targets_days)]

    # {cid: {target_ts: (abs_delta, tick_row)}}；双边有盘口的优先，单边/空簿只作兜底
    best_two_sided: dict[str, dict[int, tuple[int, dict[str, Any]]]] = {cid: {} for cid in labeled}
    best_any: dict[str, dict[int, tuple[int, dict[str, Any]]]] = {cid: {} for cid in labeled}
    kept = 0
    t0 = time.time()
    for path in files:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = CID_RE.search(line)
                if m is None:
                    continue
                cid = m.group(1)
                if cid not in best_any:
                    continue
                role = ROLE_RE.search(line)
                if role is None or role.group(1).strip().lower() != "yes":
                    continue
                ts_m = TS_RE.search(line)
                if ts_m is None:
                    continue
                ts = int(ts_m.group(1))
                end_ts = int(labeled[cid]["end_ts_ms"])
                if ts >= end_ts:
                    continue  # 结算后的盘口不是预测输入
                # 便宜的预判：两边都有报价的行才进优先簿（tick 行空簿侧写 null）
                two_sided = '"best_bid":null' not in line and '"best_ask":null' not in line
                row: Optional[dict[str, Any]] = None
                for store in ((best_two_sided, best_any) if two_sided else (best_any,)):
                    for _label, target_ts in targets[cid]:
                        delta = abs(ts - target_ts)
                        current = store[cid].get(target_ts)
                        if current is not None and current[0] <= delta:
                            continue
                        if row is None:
                            try:
                                row = json.loads(line)
                            except json.JSONDecodeError:
                                break
                        store[cid][target_ts] = (delta, row)
                        kept += 1
    one_sided_fallback = sum(1 for cid in labeled if not best_two_sided[cid] and best_any[cid])
    print(
        f"[3/3] 取样第二遍扫完，命中更新 {kept} 次，"
        f"只有单边盘口的市场 {one_sided_fallback} 个，{time.time()-t0:.0f}s",
        flush=True,
    )
    picked: dict[str, dict[int, dict[str, Any]]] = {}
    for cid in labeled:
        per = best_two_sided[cid] or best_any[cid]
        picked[cid] = {ts: payload[1] for ts, payload in per.items()}
    return picked


def _stratum_fields(label: str, days_to_resolution: float) -> dict[str, Any]:
    """分层标签 + 它与实际提前量的差.

    tick 档案只覆盖市场进入热池的那段时间，T-30 的目标时点常常落在覆盖范围外，
    ``pick_strata`` 会退到最近的可用快照——此时标签不能当真，评估一律按
    ``days_to_resolution`` 实际值分桶，``stratum_gap_days`` 用来识别这种退化。
    """
    try:
        target = float(str(label).lstrip("T-"))
    except ValueError:
        target = float("nan")
    return {
        "stratum": label,
        "stratum_target_days": target,
        "stratum_gap_days": round(abs(days_to_resolution - target), 3),
    }


def _mid(best_bid: Any, best_ask: Any) -> tuple[Optional[float], str]:
    """盘口中点 + 它的来源标记.

    返回 ``(mid, mid_source)``，``mid_source`` ∈
    ``two_sided`` / ``crossed_mid``（bid>ask，已知的脏数据形态，评估时应可排除）/
    ``one_sided_ask`` / ``one_sided_bid`` / ``no_book``。
    单边时**不**编造中点：mid 置 None，因为空簿一侧的真实概率区间是开的。
    """

    def _f(value: Any) -> Optional[float]:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if out > 0 else None

    bid = _f(best_bid)
    ask = _f(best_ask)
    if bid is not None and ask is not None:
        mid = round((bid + ask) / 2, 6)
        return mid, ("two_sided" if ask >= bid else "crossed_mid")
    if ask is not None:
        return None, "one_sided_ask"
    if bid is not None:
        return None, "one_sided_bid"
    return None, "no_book"


def build_rows_from_ticks(
    labeled: dict[str, dict[str, Any]],
    picked: dict[str, dict[int, dict[str, Any]]],
    *,
    targets_days: tuple[float, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cid, meta in labeled.items():
        per_target = picked.get(cid) or {}
        if not per_target:
            continue
        ts_list = [int(row.get("ts_ms")) for row in per_target.values() if row.get("ts_ms") is not None]
        strata = pick_strata(ts_list, int(meta["end_ts_ms"]), targets_days)
        by_ts = {int(row["ts_ms"]): row for row in per_target.values() if row.get("ts_ms") is not None}
        for label, ts in strata:
            tick = by_ts.get(ts)
            if tick is None:
                continue
            mid, mid_source = _mid(tick.get("best_bid"), tick.get("best_ask"))
            days = round((int(meta["end_ts_ms"]) - ts) / 86_400_000, 3)
            rows.append(
                {
                    **_stratum_fields(label, days),
                    "condition_id": cid,
                    "slug": meta.get("slug") or "",
                    "event_id": meta.get("event_id"),
                    "event_title": meta.get("event_title") or "",
                    "question": meta.get("question") or "",
                    "description": meta.get("description") or "",
                    "category": guess_category(meta.get("question") or "", meta.get("event_title") or ""),
                    "source": "tick",
                    "snapshot_ts_ms": ts,
                    "days_to_resolution": days,
                    "best_bid": tick.get("best_bid"),
                    "best_ask": tick.get("best_ask"),
                    "market_mid": mid,
                    "mid_source": mid_source,
                    "market_yes_price": mid if mid is not None else tick.get("best_ask"),
                    "end_date": meta["end_date"],
                    "outcome": meta["outcome"],
                }
            )
    return rows


# ---------------------------------------------------------------------------
# HF 已结算数据集（2026-01 crypto），schema 见 §6.3
# ---------------------------------------------------------------------------


def build_rows_from_hf(
    path: Path,
    *,
    gamma_host: str,
    workers: int,
    targets_days: tuple[float, ...],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    per_market: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = str(row.get("condition_id") or "")
        if not cid:
            continue
        entry = per_market.setdefault(
            cid,
            {
                "question": str(row.get("question") or "").replace("-", " "),
                "slug": str(row.get("question") or ""),
                "event_id": row.get("event_id"),
                "event_title": "",
                "ticks": {},
            },
        )
        ts = row.get("ts_ms")
        if ts is None:
            continue
        entry["ticks"][int(ts)] = row

    skips: dict[str, int] = {}
    candidates: dict[str, dict[str, Any]] = {}
    for cid, entry in per_market.items():
        if not entry["question"]:
            skips["no_question"] = skips.get("no_question", 0) + 1
            continue
        if is_numeric_market(entry["question"]):
            skips["numeric"] = skips.get("numeric", 0) + 1
            continue
        candidates[cid] = entry
    print(f"[hf] {len(per_market)} 个市场 → 非数值候选 {len(candidates)}，skip={skips}", flush=True)

    labeled, gamma_skips = label_markets(
        {cid: {k: v for k, v in entry.items() if k != "ticks"} for cid, entry in candidates.items()},
        gamma_host=gamma_host,
        workers=workers,
    )
    for key, value in gamma_skips.items():
        skips[key] = skips.get(key, 0) + value

    rows: list[dict[str, Any]] = []
    for cid, meta in labeled.items():
        ticks = candidates[cid]["ticks"]
        strata = pick_strata(list(ticks), int(meta["end_ts_ms"]), targets_days)
        for label, ts in strata:
            tick = ticks[ts]
            mid, mid_source = _mid(tick.get("yes_best_bid"), tick.get("yes_best_ask"))
            days = round((int(meta["end_ts_ms"]) - ts) / 86_400_000, 3)
            rows.append(
                {
                    **_stratum_fields(label, days),
                    "condition_id": cid,
                    "slug": meta.get("slug") or "",
                    "event_id": meta.get("event_id"),
                    "event_title": "",
                    "question": meta.get("question") or "",
                    "description": meta.get("description") or "",
                    "category": guess_category(meta.get("question") or ""),
                    "source": "hf",
                    "snapshot_ts_ms": ts,
                    "days_to_resolution": days,
                    "best_bid": tick.get("yes_best_bid"),
                    "best_ask": tick.get("yes_best_ask"),
                    "market_mid": mid,
                    "mid_source": mid_source,
                    "market_yes_price": mid if mid is not None else tick.get("yes_best_ask"),
                    "end_date": meta["end_date"],
                    "outcome": meta["outcome"],
                }
            )
    return rows, skips


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Build historical snapshots for the TypeSafe Jev replay backtest")
    ap.add_argument("--ticks-dir", action="append", default=[],
                    help="tick 目录或 glob（可多次传），例：E:/PolymarketData/*/data/ticks")
    ap.add_argument("--hf-snapshots", default=None,
                    help="hf_crypto_resolved/market_snapshots.jsonl，可选")
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--gamma-host", default=GAMMA_HOST)
    ap.add_argument("--gamma-workers", type=int, default=8)
    ap.add_argument("--strata-days", default=",".join(f"{d:g}" for d in REPLAY_STRATA_DAYS),
                    help="目标提前量（天），逗号分隔")
    args = ap.parse_args()

    targets = tuple(float(x) for x in str(args.strata_days).split(",") if x.strip())
    rows: list[dict[str, Any]] = []
    all_skips: dict[str, int] = {}

    if args.ticks_dir:
        files = expand_tick_files(args.ticks_dir)
        if not files:
            print(f"没有匹配到 tick 文件：{args.ticks_dir}")
            return 2
        markets = enumerate_tick_markets(files)
        candidates: dict[str, dict[str, Any]] = {}
        for cid, meta in markets.items():
            question = meta["question"].strip()
            if not question:
                all_skips["no_question"] = all_skips.get("no_question", 0) + 1
                continue
            if is_numeric_market(question):
                all_skips["numeric"] = all_skips.get("numeric", 0) + 1
                continue
            candidates[cid] = meta
        print(f"      非数值候选 {len(candidates)}", flush=True)
        labeled, gamma_skips = label_markets(candidates, gamma_host=args.gamma_host, workers=args.gamma_workers)
        for key, value in gamma_skips.items():
            all_skips[key] = all_skips.get(key, 0) + value
        picked = collect_tick_strata(files, labeled, targets_days=targets)
        rows.extend(build_rows_from_ticks(labeled, picked, targets_days=targets))

    if args.hf_snapshots:
        hf_rows, hf_skips = build_rows_from_hf(
            Path(args.hf_snapshots),
            gamma_host=args.gamma_host,
            workers=args.gamma_workers,
            targets_days=targets,
        )
        rows.extend(hf_rows)
        for key, value in hf_skips.items():
            all_skips[f"hf_{key}"] = all_skips.get(f"hf_{key}", 0) + value

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    by_source: dict[str, int] = {}
    by_stratum: dict[str, int] = {}
    by_mid_source: dict[str, int] = {}
    for row in rows:
        by_source[str(row["source"])] = by_source.get(str(row["source"]), 0) + 1
        by_stratum[str(row["stratum"])] = by_stratum.get(str(row["stratum"]), 0) + 1
        by_mid_source[str(row["mid_source"])] = by_mid_source.get(str(row["mid_source"]), 0) + 1
    print(
        json.dumps(
            {
                "rows": len(rows),
                "markets": len({r["condition_id"] for r in rows}),
                "events": len({r.get("event_id") for r in rows}),
                "by_source": by_source,
                "by_stratum": by_stratum,
                "by_mid_source": by_mid_source,
                "skips": all_skips,
                "output": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
