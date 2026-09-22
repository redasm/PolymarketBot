"""
构建 wallet_alpha backtest 专用数据集：
每行 = 某钱包在某市场买入 + 该市场当时的 BBO（用同一市场其他成交近似）
"""
import json, sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

OUT_DIR = Path("research/backtest/data/wallet_alpha_hf")
OUT_DIR.mkdir(parents=True, exist_ok=True)

WALLETS_FILE = Path("analysis/tracked_wallets.json")
TRADES_FILE = Path("analysis/wallet_trades_apr_jun2026.parquet")

# 这两个输入不入库（一个是 7.7MB 他人链上成交，一个是钱包地址清单）。
# 重建步骤见 analysis/README.md。
if not WALLETS_FILE.exists():
    raise SystemExit(
        f"缺少 {WALLETS_FILE}。生成：\n"
        "  python analysis/extract_wallets.py --telemetry-dir data/telemetry"
    )
if not TRADES_FILE.exists():
    raise SystemExit(
        f"缺少 {TRADES_FILE}。它不入库，需从 HuggingFace 数据集重建，"
        "见 analysis/README.md。"
    )

wallets = set(json.loads(WALLETS_FILE.read_text()))

df = pd.read_parquet(TRADES_FILE)
df["taker"] = df["taker"].str.lower()
df["price"] = df["price"].astype(float)
df["usdc_amount"] = df["usdc_amount"].astype(float)
df["block_timestamp"] = df["block_timestamp"].astype(int)
print(f"加载 {len(df):,} 行")

# 只留 tracked wallets 的 BUY 且已结算
tracked = df[df["taker"].isin(wallets)].copy()
print(f"tracked wallet 行: {len(tracked):,}")

# 对每个 condition_id，计算每个时刻附近的 yes/no BBO
# 用同一 condition_id 所有成交构建滚动 BBO（最近一笔 BUY=ask, SELL=bid）
# 先按市场+时间排序，然后 forward-fill BBO

all_rows = df.sort_values("block_timestamp")

# 构建 BBO 状态
bbo_records = []
bbo_state: dict[str, dict] = {}  # cid -> {yes_bid, yes_ask, no_bid, no_ask}

for _, row in all_rows.iterrows():
    cid = row["condition_id"]
    outcome = (row.get("outcome_label") or "").lower()
    direction = (row.get("taker_direction") or "").upper()
    price = float(row["price"])

    s = bbo_state.setdefault(cid, {})
    if outcome == "yes":
        if direction == "BUY":
            s["yes_ask"] = price
            s["yes_ask_size"] = float(row["usdc_amount"]) / price if price > 0 else 1.0
        elif direction == "SELL":
            s["yes_bid"] = price
    elif outcome == "no":
        if direction == "BUY":
            s["no_ask"] = price
            s["no_ask_size"] = float(row["usdc_amount"]) / price if price > 0 else 1.0
        elif direction == "SELL":
            s["no_bid"] = price

    # 只在 tracked wallet 买入时 emit 一条 backtest row
    taker = (row.get("taker") or "").lower()
    if taker not in wallets or direction != "BUY":
        continue
    if not all(k in s for k in ("yes_ask", "no_ask")):
        continue

    action = "BUY_YES" if outcome == "yes" else "BUY_NO"
    bbo_records.append({
        "ts_ms": int(row["block_timestamp"]) * 1000,
        "condition_id": cid,
        "event_id": cid,
        "question": str(row.get("market_slug") or cid),
        "wallet_address": taker,
        "action": action,
        "category": str(row.get("category") or ""),
        "winning_outcome": str(row.get("winning_outcome_label") or ""),
        "yes_best_bid": s.get("yes_bid", s.get("yes_ask", 0.5) - 0.05),
        "yes_best_ask": s.get("yes_ask", 0.5),
        "yes_ask_size": s.get("yes_ask_size", 10.0),
        "no_best_bid": s.get("no_bid", s.get("no_ask", 0.5) - 0.05),
        "no_best_ask": s.get("no_ask", 0.5),
        "no_ask_size": s.get("no_ask_size", 10.0),
    })

print(f"生成 backtest 行: {len(bbo_records):,}")

out = OUT_DIR / "market_snapshots.jsonl"
with out.open("w") as f:
    for r in bbo_records:
        f.write(json.dumps(r) + "\n")
print(f"已保存: {out}")
