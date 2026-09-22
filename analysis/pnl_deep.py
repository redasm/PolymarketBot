import argparse
import json, glob, os
from collections import defaultdict

parser = argparse.ArgumentParser(description="Detailed position lifecycle PnL analysis")
parser.add_argument("--telemetry-dir", default="data/telemetry")
args = parser.parse_args()
BASE = args.telemetry_dir

# ── positions_lifecycle: 只取 closed ─────────────────────────────────────────
closed = []
opened = []
for f in sorted(glob.glob(f"{BASE}/*.positions_lifecycle.ndjson")):
    date = os.path.basename(f).split(".")[0]
    with open(f) as fp:
        for line in fp:
            try:
                r = json.loads(line); r["_date"] = date
                if r.get("event") == "position_closed":
                    closed.append(r)
                elif r.get("event") == "position_opened":
                    opened.append(r)
            except: pass

print(f"=== positions_lifecycle ===")
print(f"开仓: {len(opened)}, 平仓: {len(closed)}")

# 按 tier / signal_type 分组
by_tier = defaultdict(list)
by_sig  = defaultdict(list)
for p in closed:
    tier = p.get("tier", "unknown")
    sig  = p.get("decision_context", {}).get("signal_type", "unknown")
    by_tier[tier].append(p)
    by_sig[sig].append(p)

print("\n-- 按 tier 汇总 --")
print(f"{'Tier':<20} {'笔数':>6} {'总PnL':>10} {'均PnL':>9} {'胜率':>7} {'均持仓(s)':>10}")
for tier, recs in sorted(by_tier.items()):
    pnls  = [r["realized_pnl"] for r in recs if "realized_pnl" in r]
    holds = [r["hold_sec"]     for r in recs if "hold_sec" in r]
    wins  = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    avg   = total / len(pnls) if pnls else 0
    wr    = wins / len(pnls) if pnls else 0
    avgh  = sum(holds)/len(holds) if holds else 0
    print(f"{tier:<20} {len(pnls):>6} {total:>10.4f} {avg:>9.4f} {wr:>7.1%} {avgh:>10.1f}")

print("\n-- 按 signal_type 汇总 --")
print(f"{'signal_type':<35} {'笔数':>6} {'总PnL':>10} {'均PnL':>9} {'胜率':>7}")
for sig, recs in sorted(by_sig.items(), key=lambda x: -abs(sum(r.get("realized_pnl",0) for r in x[1]))):
    pnls = [r["realized_pnl"] for r in recs if "realized_pnl" in r]
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    avg   = total / len(pnls) if pnls else 0
    wr    = wins / len(pnls) if pnls else 0
    print(f"{sig:<35} {len(pnls):>6} {total:>10.4f} {avg:>9.4f} {wr:>7.1%}")

print("\n-- 按日汇总 --")
print(f"{'日期':<12} {'平仓笔数':>8} {'日PnL':>10} {'开仓笔数':>8}")
open_by_date  = defaultdict(int)
close_by_date = defaultdict(list)
for p in opened: open_by_date[p["_date"]] += 1
for p in closed: close_by_date[p["_date"]].append(p.get("realized_pnl", 0))
for d in sorted(set(list(open_by_date) + list(close_by_date))):
    pnls = close_by_date[d]
    print(f"{d:<12} {len(pnls):>8} {sum(pnls):>10.4f} {open_by_date[d]:>8}")

# 总计
all_pnls = [r["realized_pnl"] for r in closed if "realized_pnl" in r]
all_fees = [r.get("fees", 0) for r in closed]
wins = sum(1 for p in all_pnls if p > 0)
print(f"\n== 合计 ==")
print(f"平仓总笔数: {len(all_pnls)}")
print(f"累计realized_pnl: {sum(all_pnls):.4f} USDC")
print(f"累计费用: {sum(all_fees):.4f} USDC")
print(f"胜率: {wins/len(all_pnls):.1%}" if all_pnls else "无数据")

# 最差10笔
print("\n-- 最差10笔 --")
worst = sorted(closed, key=lambda r: r.get("realized_pnl", 0))[:10]
for r in worst:
    dc = r.get("decision_context", {})
    print(f"  pnl={r.get('realized_pnl'):>8.4f}  hold={r.get('hold_sec',0):>7.1f}s  "
          f"sig={dc.get('signal_type','?'):<30}  title={dc.get('event_title','?')[:50]}")

# 最好10笔
print("\n-- 最好10笔 --")
best = sorted(closed, key=lambda r: -r.get("realized_pnl", 0))[:10]
for r in best:
    dc = r.get("decision_context", {})
    print(f"  pnl={r.get('realized_pnl'):>8.4f}  hold={r.get('hold_sec',0):>7.1f}s  "
          f"sig={dc.get('signal_type','?'):<30}  title={dc.get('event_title','?')[:50]}")

# ── exit_reason 分布 ──────────────────────────────────────────────────────────
exit_reasons = defaultdict(lambda: {"count":0,"pnl":0.0})
for r in closed:
    dc  = r.get("decision_context", {})
    key = dc.get("exit_reason", "unknown")
    exit_reasons[key]["count"] += 1
    exit_reasons[key]["pnl"]   += r.get("realized_pnl", 0)

print("\n-- exit_reason 分布 --")
print(f"{'exit_reason':<25} {'笔数':>6} {'总PnL':>10} {'均PnL':>9}")
for k, v in sorted(exit_reasons.items(), key=lambda x: -abs(x[1]["pnl"])):
    avg = v["pnl"]/v["count"] if v["count"] else 0
    print(f"{k:<25} {v['count']:>6} {v['pnl']:>10.4f} {avg:>9.4f}")
