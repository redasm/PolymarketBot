"""T0 binary 套利窗口存活时间检验。

从 tick 流重建每个市场的 yes/no 双边盘口，标记 T0 binary 套利窗口
(Σask<1 且非 crossed 且扣 fee 净正)，测每个窗口从出现到消失存活多少毫秒。

判据：若高 edge 窗口存活时间 < 下单延迟(~50-200ms)，则 T0 不可成交，是 stale 假象。
"""
from __future__ import annotations

import collections
import json
import statistics
import sys
from pathlib import Path

FEE = 0.02


def vfee(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return FEE * p * (1.0 - p)


def net_edge(ya: float, na: float) -> float:
    return (1.0 - (ya + na)) - (vfee(ya) + vfee(na))


def process_file(path: Path, windows: list):
    """按 condition 分组，组内按 ts 排序，重建套利窗口存活时间。"""
    by_cond = collections.defaultdict(list)
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("event_type") != "book":
                continue
            role = d.get("outcome_role")
            if role not in ("yes", "no"):
                continue
            cid = d.get("condition_id")
            if not cid:
                continue
            by_cond[cid].append(d)

    for cid, ticks in by_cond.items():
        ticks.sort(key=lambda r: r.get("ts_ms", 0))
        st = {"yes_ask": None, "no_ask": None, "yes_bid": None, "no_bid": None}
        open_ts = None
        open_peak = 0.0
        open_start_edge = 0.0
        for t in ticks:
            ts = t.get("ts_ms", 0)
            role = t["outcome_role"]
            st[f"{role}_ask"] = t.get("best_ask")
            st[f"{role}_bid"] = t.get("best_bid")
            ya, na, yb, nb = st["yes_ask"], st["no_ask"], st["yes_bid"], st["no_bid"]
            arb = False
            e = 0.0
            if ya is not None and na is not None and ya + na < 1.0:
                yc = yb is not None and yb >= ya
                nc = nb is not None and nb >= na
                if not (yc or nc):
                    e = net_edge(ya, na)
                    if e > 0:
                        arb = True
            if arb:
                if open_ts is None:
                    open_ts = ts
                    open_peak = e
                    open_start_edge = e
                else:
                    open_peak = max(open_peak, e)
            else:
                if open_ts is not None:
                    windows.append({
                        "cid": cid,
                        "survive_ms": ts - open_ts,
                        "peak_edge": open_peak,
                        "start_edge": open_start_edge,
                    })
                    open_ts = None
        # 文件末仍开着的窗口：用最后 ts 截断（保守，记为至少存活到此）
        if open_ts is not None:
            windows.append({
                "cid": cid,
                "survive_ms": ticks[-1]["ts_ms"] - open_ts,
                "peak_edge": open_peak,
                "start_edge": open_start_edge,
                "censored": True,
            })


def main():
    tick_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/e/PolymarketData/6.6-6.12/data/ticks")
    files = sorted(tick_dir.glob("*.ndjson"))
    if len(sys.argv) > 2:
        files = files[: int(sys.argv[2])]
    windows = []
    for fp in files:
        print(f"processing {fp.name} ...", file=sys.stderr)
        process_file(fp, windows)

    print(f"\n=== T0 binary 套利窗口存活时间 (fee_rate={FEE}) ===")
    print(f"文件数={len(files)}  总窗口数={len(windows)}")
    if not windows:
        print("无窗口")
        return
    cens = sum(1 for w in windows if w.get("censored"))
    surv = [w["survive_ms"] for w in windows]
    print(f"  (其中文件末截断 censored={cens})")
    print()
    # 存活时间分布
    surv_s = sorted(surv)
    def pct(p): return surv_s[min(len(surv_s) - 1, int(len(surv_s) * p))]
    print("存活时间分布(ms):")
    print(f"  p10={pct(.1)} p25={pct(.25)} median={statistics.median(surv):.0f} p75={pct(.75)} p90={pct(.9)} max={max(surv)}")
    # 关键阈值：低于常见下单延迟的占比
    for thr in (50, 100, 200, 500, 1000):
        c = sum(1 for s in surv if s < thr)
        print(f"  存活 < {thr}ms: {c} ({c/len(surv)*100:.0f}%)")
    print()
    # 按 edge 档分层看存活
    print("按 peak_edge 分层:")
    bands = [(0, 0.005, "微(<0.5%)"), (0.005, 0.02, "薄(0.5-2%)"), (0.02, 0.05, "中(2-5%)"), (0.05, 1.0, "高(>5%)")]
    for lo, hi, name in bands:
        ws = [w for w in windows if lo <= w["peak_edge"] < hi]
        if not ws:
            print(f"  {name:14s} n=0")
            continue
        s = sorted(w["survive_ms"] for w in ws)
        med = statistics.median(s)
        lt100 = sum(1 for x in s if x < 100) / len(s) * 100
        print(f"  {name:14s} n={len(ws):5d} median存活={med:7.0f}ms  <100ms占比={lt100:3.0f}%")


if __name__ == "__main__":
    main()
