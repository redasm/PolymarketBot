import json, glob, os

BASE = "E:/PolymarketData/6.2-6.5/data/telemetry"

# ── 1. virtual_fills ──────────────────────────────────────────────────────────
fills = []
for f in sorted(glob.glob(f"{BASE}/*.virtual_fills.ndjson")):
    date = os.path.basename(f).split(".")[0]
    with open(f) as fp:
        for line in fp:
            try:
                r = json.loads(line); r["_date"] = date; fills.append(r)
            except: pass

print(f"\n=== 1. virtual_fills ===")
print(f"总记录: {len(fills)}")
if fills:
    print("字段样例:", list(fills[0].keys()))
    print("前3条:", json.dumps(fills[:3], indent=2, default=str))

# ── 2. strategy_executions ───────────────────────────────────────────────────
execs = []
for f in sorted(glob.glob(f"{BASE}/*.strategy_executions.ndjson")):
    date = os.path.basename(f).split(".")[0]
    with open(f) as fp:
        for line in fp:
            try:
                r = json.loads(line); r["_date"] = date; execs.append(r)
            except: pass

print(f"\n=== 2. strategy_executions ===")
print(f"总记录: {len(execs)}")
if execs:
    print("字段样例:", list(execs[0].keys()))
    print("前3条:", json.dumps(execs[:3], indent=2, default=str))

# ── 3. positions_lifecycle ────────────────────────────────────────────────────
pos = []
for f in sorted(glob.glob(f"{BASE}/*.positions_lifecycle.ndjson")):
    date = os.path.basename(f).split(".")[0]
    with open(f) as fp:
        for line in fp:
            try:
                r = json.loads(line); r["_date"] = date; pos.append(r)
            except: pass

print(f"\n=== 3. positions_lifecycle ===")
print(f"总记录: {len(pos)}")
if pos:
    print("字段样例:", list(pos[0].keys()))
    closed = [p for p in pos if p.get("event") == "closed" or p.get("status") == "closed"]
    print(f"已关闭仓位: {len(closed)}")
    print("前5条:", json.dumps(pos[:5], indent=2, default=str))

# ── 4. cycle_metrics ──────────────────────────────────────────────────────────
metrics = []
for f in sorted(glob.glob(f"{BASE}/*.cycle_metrics.ndjson")):
    date = os.path.basename(f).split(".")[0]
    with open(f) as fp:
        for line in fp:
            try:
                r = json.loads(line); r["_date"] = date; metrics.append(r)
            except: pass

print(f"\n=== 4. cycle_metrics ===")
if metrics:
    last = metrics[-1]
    print("最后一条:", json.dumps(last, indent=2, default=str))
    by_date = {}
    for m in metrics:
        by_date[m["_date"]] = m
    print("\n每日最后一条汇总:")
    for d, m in sorted(by_date.items()):
        pnl   = m.get("daily_pnl") or m.get("pnl") or m.get("realized_pnl")
        total = m.get("total_pnl") or m.get("cumulative_pnl")
        print(f"  {d}: daily_pnl={pnl}, total_pnl={total}, "
              f"arbs_found={m.get('arbs_found_total')}, t0_opps={m.get('t0_opportunities_total')}")
