"""T0 backtest on HuggingFace crypto resolved dataset."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from research.backtest.replay.runner import BacktestRunner, BacktestRunConfig
from research.backtest.adapters.t0_adapter import T0BacktestAdapter
from research.backtest.execution_model.base import DepthVWAPExecutionModel, ExecutionModelConfig

DATA_DIR = "research/backtest/data"
DATASET = "hf_crypto_resolved"

snapshots = Path(f"{DATA_DIR}/{DATASET}/market_snapshots.jsonl")
if not snapshots.exists():
    print(f"ERROR: {snapshots} 不存在，先运行 analysis/build_t0_dataset.py")
    sys.exit(1)

print(f"快照文件: {snapshots.stat().st_size / 1e6:.1f} MB")

runner = BacktestRunner(DATA_DIR)
cfg = BacktestRunConfig(
    dataset_name=DATASET,
    output_dir="research/backtest/output/t0_hf_crypto",
    holding_period_ms=0,
    market_cooldown_ms=60_000,
)
exec_model = DepthVWAPExecutionModel(ExecutionModelConfig(fee_rate=0.02, latency_ms=25, slippage_bps=20))

strategy = T0BacktestAdapter  # runner 用 strategy_name 区分路径
report = runner.run(strategy, DATASET, execution_model=exec_model, config=cfg)

print(f"\n=== T0 backtest 结果 ===")
print(f"信号: {report.total_signals}, 成交: {report.filled_trades}, 跳过: {report.skipped_signals}")
print(f"填充率: {report.fill_rate:.1%}, 胜率: {report.win_rate:.1%}")
print(f"PnL: ${report.gross_pnl:.2f}, profit_factor: {report.profit_factor}")
print(f"avg edge: {report.avg_signal_edge_bps:.0f} bps")
