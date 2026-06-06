"""
两个任务并行：
1. wallet_alpha: 从 daily_aligned 按跟单钱包地址过滤，计算 lagged_follow_roi，生成 wallet_profiles.json
2. T0: 从 daily_aligned 过滤 crypto resolved 市场，重建 BBO 快照，生成 backtest dataset
"""
import json, sys
from collections import defaultdict
from pathlib import Path

# ── 第一步：从 telemetry 提取被跟单的钱包地址 ──────────────────────────
telemetry_dir = Path("E:/PolymarketData/6.2-6.5/data/telemetry")
wallet_addrs = set()
for f in sorted(telemetry_dir.glob("*.positions_lifecycle.ndjson")):
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            dc = r.get("decision_context") or {}
            w = dc.get("wallet_address") or dc.get("source_wallet") or dc.get("alpha_wallet")
            if w and w != "unknown":
                wallet_addrs.add(w.lower())
        except Exception:
            pass

print(f"跟单钱包数: {len(wallet_addrs)}")
for w in sorted(wallet_addrs)[:5]:
    print(" ", w)

out = Path("E:/AppProject/PolymarketBot/analysis/tracked_wallets.json")
out.write_text(json.dumps(sorted(wallet_addrs), indent=2))
print(f"\n已保存到: {out}")
