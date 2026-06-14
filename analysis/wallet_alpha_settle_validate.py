"""Wallet-alpha 跟单到结算的真实 PnL 验证（无 look-ahead）。

数据每行 = 一个被跟单钱包在某市场的动作 + 客观 winning_outcome。
模拟：按 best_ask 跟单进场，持有到结算，赢家拿 $1，输家归零，扣 V2 taker fee。
这是判断 wallet_alpha 是否有真实预测力的终极判据——结算结果是客观事实，不存在泄漏。
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "research/backtest/data/wallet_alpha_hf/market_snapshots.jsonl"
ORDER_USDC = 10.0
FEE_RATE = 0.02  # V2 taker fee rate（保守，与项目 polymarket_taker_fee_rate 同量级）


def v2_fee(price: float, size: float) -> float:
    p = max(0.0, min(1.0, price))
    return size * FEE_RATE * p * (1.0 - p)


def simulate(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        action = (r.get("action") or "").upper()
        win = r.get("winning_outcome")
        if action not in ("BUY_YES", "BUY_NO") or win not in ("Yes", "No"):
            continue
        if action == "BUY_YES":
            entry = r.get("yes_best_ask")
            won = win == "Yes"
        else:
            entry = r.get("no_best_ask")
            won = win == "No"
        if entry is None or entry <= 0 or entry >= 1:
            continue  # 脏价（负/0/>=1）跳过，无法跟单
        size = ORDER_USDC / entry          # 份额
        cost = size * entry                 # = ORDER_USDC
        fee = v2_fee(entry, size)
        payout = size * (1.0 if won else 0.0)
        pnl = payout - cost - fee
        out.append({
            "wallet": r.get("wallet_address"),
            "action": action,
            "category": r.get("category") or "",
            "entry": entry,
            "won": won,
            "pnl": pnl,
            "fee": fee,
            "notional": cost,
        })
    return out


def agg(name, recs):
    n = len(recs)
    if not n:
        print(f"{name}: 无样本")
        return
    pnl = sum(x["pnl"] for x in recs)
    fee = sum(x["fee"] for x in recs)
    notl = sum(x["notional"] for x in recs)
    wins = sum(1 for x in recs if x["won"])
    roi = pnl / notl * 100 if notl else 0
    print(f"{name:34s} n={n:5d} 命中率={wins/n*100:4.0f}% 净PnL={pnl:+9.2f} fee={fee:7.2f} ROI={roi:+6.2f}%")


def main() -> None:
    rows = [json.loads(l) for l in open(DATA, encoding="utf-8-sig") if l.strip()]
    recs = simulate(rows)
    print(f"=== Wallet-alpha 跟单到结算 (订单${ORDER_USDC}, fee_rate={FEE_RATE}) ===")
    print(f"原始行={len(rows)}  可模拟={len(recs)}\n")

    agg("总体", recs)
    print()
    print("--- 按 action ---")
    by = collections.defaultdict(list)
    for x in recs: by[x["action"]].append(x)
    for k in sorted(by): agg(k, by[k])
    print()
    print("--- 按钱包 (n>=20) ---")
    by = collections.defaultdict(list)
    for x in recs: by[x["wallet"]].append(x)
    pos = 0
    for w, rs in sorted(by.items(), key=lambda kv: -sum(x["pnl"] for x in kv[1])):
        if len(rs) >= 20:
            if sum(x["pnl"] for x in rs) > 0: pos += 1
            agg(w[:16], rs)
    n20 = [rs for rs in by.values() if len(rs) >= 20]
    print(f"\nn>=20 钱包: {len(n20)} 个, 净正 {pos} 个")
    # 敏感性：不同 fee 下总体
    print("\n--- fee 敏感性 (总体净PnL) ---")
    for fr in (0.0, 0.01, 0.02):
        tot = 0.0
        for x in recs:
            f = abs(x["entry"]); f = (ORDER_USDC/x["entry"]) * fr * max(0,min(1,x["entry"]))*(1-max(0,min(1,x["entry"])))
            gross = x["pnl"] + x["fee"]
            tot += gross - f
        print(f"  fee_rate={fr}: 净PnL={tot:+.2f}")


if __name__ == "__main__":
    main()
