"""
wallet_alpha backtest: 用链上真实 lagged_follow_roi 跑回测。
依赖: data/quant_inputs/wallet_alpha_profiles.json（build_wallet_profiles.py 生成）
"""
import json, sys
from pathlib import Path

# 加入项目根路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from research.backtest.replay.runner import BacktestRunner, BacktestRunConfig
from research.backtest.adapters.quant_strategy_adapter import WalletAlphaBacktestAdapter
from polymarket_arb.strategies.wallet_alpha import WalletProfile

PROFILES_FILE = Path("data/quant_inputs/wallet_alpha_profiles.json")
DATA_DIR = "research/backtest/data"
DATASET = "default"

if not PROFILES_FILE.exists():
    print(f"ERROR: {PROFILES_FILE} 不存在，先运行 analysis/build_wallet_profiles.py")
    sys.exit(1)

raw = json.loads(PROFILES_FILE.read_text())
profiles = {
    w: WalletProfile(
        wallet_address=w,
        trade_count=int(d["trade_count"]),
        realized_roi=float(d["realized_roi"]),
        lagged_follow_roi=float(d["lagged_follow_roi"]),
        max_drawdown=float(d["max_drawdown"]),
        concentration_score=float(d["concentration_score"]),
        category_edges={k: float(v) for k, v in d.get("category_edges", {}).items()},
    )
    for w, d in raw.items()
}

passing = sum(1 for p in profiles.values() if p.lagged_follow_roi >= 0.04)
print(f"profiles 总数: {len(profiles)}, 通过 lagged_roi≥0.04 门槛: {passing}")

runner = BacktestRunner(DATA_DIR)
cfg = BacktestRunConfig(
    dataset_name=DATASET,
    output_dir="research/backtest/output/wallet_alpha_hf",
    holding_period_ms=300_000,  # 5min markout
    max_open_positions=3,
)

from research.backtest.adapters.quant_strategy_adapter import WalletAlphaBacktestAdapter
adapter = WalletAlphaBacktestAdapter(profiles=profiles, order_size_usdc=10.0)

# 直接调内部 quant markout runner
from research.backtest.execution_model.base import DepthVWAPExecutionModel, ExecutionModelConfig
exec_model = DepthVWAPExecutionModel(ExecutionModelConfig(fee_rate=0.02, latency_ms=25, slippage_bps=20))
report = runner.run(adapter, DATASET, execution_model=exec_model, config=cfg)

print(f"\n=== wallet_alpha backtest 结果 ===")
print(f"信号: {report.total_signals}, 成交: {report.filled_trades}")
print(f"胜率: {report.win_rate:.1%}, PnL: ${report.gross_pnl:.2f}")
print(f"profit_factor: {report.profit_factor}")
