"""
从 daily_aligned 2026年数据构建 T0 backtest 数据集。
筛选 crypto + resolved + 二元市场，重建 BBO 快照序列。
"""
import json
from collections import defaultdict
from pathlib import Path
import pyarrow.parquet as pq
from huggingface_hub import HfFileSystem

REPO = "TimeSeventeen/Polymarket-v1"
OUT_DIR = Path("research/backtest/data/hf_crypto_resolved")
OUT_DIR.mkdir(parents=True, exist_ok=True)

fs = HfFileSystem()
all_files = fs.ls(f"datasets/{REPO}/daily_aligned", detail=False)
# 只取2026年1月，已有约15万快照足够回测
files_2026 = sorted(f for f in all_files if "/2026-01" in f)
print(f"2026年文件数: {len(files_2026)}")

# BBO 状态: condition_id -> {yes: {bid,ask,size}, no: {bid,ask,size}, meta}
bbo: dict[str, dict] = {}
snapshots: list[dict] = []

for i, hf_path in enumerate(files_2026):
    fname = hf_path.split("/")[-1]
    print(f"[{i+1}/{len(files_2026)}] {fname}", end="  ", flush=True)
    try:
        with fs.open(hf_path, "rb") as f:
            table = pq.read_table(f, columns=[
                "condition_id", "outcome_label", "taker_direction",
                "price", "usdc_amount", "block_timestamp",
                "category", "neg_risk", "resolution_status",
                "winning_outcome_label", "market_slug",
            ])
        df = table.to_pydict()
        n = len(df["condition_id"])
        emitted = 0
        for j in range(n):
            cat = (df["category"][j] or "").lower()
            if "crypto" not in cat:
                continue
            if df["resolution_status"][j] != "resolved":
                continue
            neg = str(df["neg_risk"][j] or "f").lower()
            if neg in ("t", "true", "1"):
                continue
            outcome = (df["outcome_label"][j] or "").lower()
            if outcome not in ("yes", "no"):
                continue

            cid = df["condition_id"][j]
            ts_ms = int(df["block_timestamp"][j]) * 1000
            price = float(df["price"][j])
            size = float(df["usdc_amount"][j] or 0) / price if price > 0 else 0.0
            direction = (df["taker_direction"][j] or "").upper()

            state = bbo.setdefault(cid, {
                "yes": {"bid": None, "ask": None, "size": 0.0},
                "no":  {"bid": None, "ask": None, "size": 0.0},
                "question": df["market_slug"][j] or cid,
                "winning": df["winning_outcome_label"][j] or "",
                "ts_ms": ts_ms,
            })
            side = state[outcome]
            if direction == "BUY":
                side["ask"] = price
                side["size"] = size
            elif direction == "SELL":
                side["bid"] = price
            state["ts_ms"] = ts_ms

            yes, no = state["yes"], state["no"]
            if all(yes.get(k) is not None for k in ("bid", "ask")) and \
               all(no.get(k) is not None for k in ("bid", "ask")):
                snapshots.append({
                    "ts_ms": ts_ms,
                    "condition_id": cid,
                    "event_id": cid,
                    "question": state["question"],
                    "winning_outcome": state["winning"],
                    "yes_best_bid": yes["bid"],
                    "yes_best_ask": yes["ask"],
                    "yes_ask_size": yes["size"],
                    "no_best_bid": no["bid"],
                    "no_best_ask": no["ask"],
                    "no_ask_size": no["size"],
                })
                emitted += 1
        print(f"行={n:,} 快照={emitted}")
    except Exception as e:
        print(f"ERROR: {e}")

snapshots.sort(key=lambda r: r["ts_ms"])
out_file = OUT_DIR / "market_snapshots.jsonl"
with out_file.open("w") as f:
    for s in snapshots:
        f.write(json.dumps(s) + "\n")

print(f"\n完成: {len(snapshots):,} 快照，{len(bbo):,} 市场")
print(f"已保存: {out_file}")
