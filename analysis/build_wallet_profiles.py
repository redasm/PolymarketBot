"""从已下载的 parquet 计算 wallet profiles 并运行 wallet_alpha backtest."""
import argparse
import json, sys
from collections import defaultdict
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from research.backtest.replay.runner import BacktestRunner, BacktestRunConfig
from research.backtest.adapters.quant_strategy_adapter import WalletAlphaBacktestAdapter
from polymarket_arb.strategies.wallet_alpha import WalletProfile
from research.backtest.execution_model.base import DepthVWAPExecutionModel, ExecutionModelConfig

from polymarket_arb.config import ArbConfig
FEE_RATE = ArbConfig.from_env(require_wallet=False).polymarket_taker_fee_rate
MIN_TRADES = 10
parser = argparse.ArgumentParser(description="Build wallet alpha profiles")
parser.add_argument("--trades", type=Path, default=Path("analysis/wallet_trades_apr_jun2026.parquet"))
parser.add_argument("--profiles-out", type=Path, default=Path("data/quant_inputs/wallet_alpha_profiles.json"))
parser.add_argument("--fee-rate", type=float, default=FEE_RATE)
args = parser.parse_args()
FEE_RATE = args.fee_rate
PROFILES_OUT = args.profiles_out

# ── 1. 计算 profiles ────────────────────────────────────────────────────
# --trades 默认指向一份不入库的 parquet（7.7MB 他人链上成交）。
# 重建步骤见 analysis/README.md。
if not args.trades.exists():
    raise SystemExit(
        f"缺少 {args.trades}。该数据集不入库，需从 HuggingFace 数据集重建，"
        "见 analysis/README.md；也可用 --trades 指定自己的文件。"
    )

df = pd.read_parquet(args.trades)
df["taker"] = df["taker"].str.lower()
print(f"加载 {len(df):,} 笔交易，钱包数: {df['taker'].nunique()}")

profiles = {}
for wallet, grp in df.groupby("taker"):
    if len(grp) < MIN_TRADES:
        continue
    grp = grp.sort_values("block_timestamp")
    p = grp["price"].astype(float)
    won = grp["outcome_label"] == grp["winning_outcome_label"]
    payout = won.astype(float)
    fee = p * FEE_RATE * (1 - p)
    size = grp["usdc_amount"].astype(float) / p.clip(lower=1e-9)
    roi = (payout - p - fee) / p.clip(lower=1e-9)

    realized_roi = ((payout - p - fee) * size).sum() / grp["usdc_amount"].astype(float).sum()

    lp = (p * 1.01).clip(upper=0.98)
    lfee = lp * FEE_RATE * (1 - lp)
    lagged_follow_roi = float(((payout - lp - lfee) / lp.clip(lower=1e-9)).mean())

    # max drawdown
    running = ((1 - p) * won + (-p) * ~won).cumsum()
    peak = running.cummax()
    max_dd = (peak - running).max()
    max_drawdown = float(min(max_dd / (abs(peak.max()) + 1e-9), 1.0))

    concentration = float(min(
        grp.groupby("condition_id")["usdc_amount"].sum().max() / grp["usdc_amount"].sum(),
        1.0
    ))
    cat_edges = {
        c: round(float(g.mean()), 6)
        for c, g in roi.groupby(grp["category"])
        if len(g) >= 3
    }

    profiles[wallet] = {
        "wallet_address": wallet,
        "trade_count": int(len(grp)),
        "realized_roi": round(float(realized_roi), 6),
        "lagged_follow_roi": round(lagged_follow_roi, 6),
        "max_drawdown": round(max_drawdown, 6),
        "concentration_score": round(concentration, 6),
        "category_edges": cat_edges,
    }

passing = sum(1 for p in profiles.values() if p["lagged_follow_roi"] >= 0.04)
print(f"\n生成 {len(profiles)} profiles，通过门槛(lagged≥0.04): {passing}")
for w, p in sorted(profiles.items(), key=lambda x: -x[1]["lagged_follow_roi"])[:10]:
    flag = "✓" if p["lagged_follow_roi"] >= 0.04 else "✗"
    print(f"  {flag} {w[:16]}... n={p['trade_count']:3d} realized={p['realized_roi']:+.3f} lagged={p['lagged_follow_roi']:+.3f}")

PROFILES_OUT.parent.mkdir(exist_ok=True)
PROFILES_OUT.write_text(json.dumps(profiles, indent=2))
print(f"\n已保存: {PROFILES_OUT}")

# ── 2. 运行 wallet_alpha backtest ───────────────────────────────────────
wp = {
    w: WalletProfile(
        wallet_address=w,
        trade_count=int(d["trade_count"]),
        realized_roi=float(d["realized_roi"]),
        lagged_follow_roi=float(d["lagged_follow_roi"]),
        max_drawdown=float(d["max_drawdown"]),
        concentration_score=float(d["concentration_score"]),
        category_edges={k: float(v) for k, v in d.get("category_edges", {}).items()},
    )
    for w, d in profiles.items()
}

adapter = WalletAlphaBacktestAdapter(profiles=wp, order_size_usdc=10.0)
runner = BacktestRunner("research/backtest/data")
cfg = BacktestRunConfig(
    dataset_name="wallet_alpha_hf",
    output_dir="research/backtest/output/wallet_alpha_hf",
    holding_period_ms=300_000,
    max_open_positions=3,
)
exec_model = DepthVWAPExecutionModel(ExecutionModelConfig(fee_rate=0.02, latency_ms=25, slippage_bps=20))
report = runner.run(adapter, "default", execution_model=exec_model, config=cfg)

print(f"\n=== wallet_alpha backtest ===")
print(f"信号={report.total_signals} 成交={report.filled_trades} 胜率={report.win_rate:.1%}")
print(f"PnL=${report.gross_pnl:.2f} profit_factor={report.profit_factor}")
