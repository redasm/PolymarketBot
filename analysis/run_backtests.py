"""wallet_alpha + T0 backtest，直接用 load_jsonl 绕过 runner 的 BBO 重建。"""
import json, sys
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).parent.parent))

from research.backtest.data.reader import BacktestDatasetReader
from research.backtest.adapters.quant_strategy_adapter import WalletAlphaBacktestAdapter
from research.backtest.adapters.t0_adapter import T0BacktestAdapter
from research.backtest.execution_model.base import DepthVWAPExecutionModel, ExecutionModelConfig, estimate_binary_clob_fee
from polymarket_arb.strategies.wallet_alpha import WalletProfile
from polymarket_arb.config import ArbConfig

FEE_RATE = 0.02
HOLDING_MS = 300_000  # 5min markout

reader = BacktestDatasetReader("research/backtest/data")
exec_model = DepthVWAPExecutionModel(ExecutionModelConfig(fee_rate=FEE_RATE, latency_ms=25, slippage_bps=20))

def run_markout(adapter, rows, label):
    """通用 markout backtest：信号 → 成交 → N分钟后 mid 退出。"""
    rows = sorted(rows, key=lambda r: int(r.get("ts_ms", 0)))
    rows_by_market = defaultdict(list)
    for r in rows:
        rows_by_market[r["condition_id"]].append(r)

    wins = losses = filled = signals = 0
    total_pnl = 0.0

    for cid, mrows in rows_by_market.items():
        for i, row in enumerate(mrows):
            sig = adapter.detect(row)
            if sig is None:
                continue
            signals += 1
            order = adapter.to_order_request(sig, row)
            exe = exec_model.simulate(order, {**row, **order})
            if not exe.filled or exe.average_price is None:
                continue
            filled += 1

            # 找 holding_ms 后的 exit row
            ts = int(row["ts_ms"])
            exit_row = next((r for r in mrows[i+1:] if int(r["ts_ms"]) >= ts + HOLDING_MS), mrows[-1])

            action = str(sig.payload.get("action", "BUY_YES")).upper()
            prefix = "no" if action == "BUY_NO" else "yes"
            eb = exit_row.get(f"{prefix}_best_bid")
            ea = exit_row.get(f"{prefix}_best_ask")
            if eb is None or ea is None:
                continue
            exit_mid = (float(eb) + float(ea)) / 2
            entry_fee = exe.fees_paid
            exit_fee = estimate_binary_clob_fee(exit_mid, exe.filled_size, FEE_RATE)
            pnl = (exit_mid - exe.average_price) * exe.filled_size - entry_fee - exit_fee
            total_pnl += pnl
            if pnl > 0: wins += 1
            else: losses += 1

    wr = wins / filled if filled else 0
    print(f"\n=== {label} ===")
    print(f"信号={signals} 成交={filled} 胜率={wr:.1%} PnL=${total_pnl:.2f}")
    print(f"均PnL=${total_pnl/filled:.3f}" if filled else "")
    return total_pnl, filled, wr


# ── 1. wallet_alpha backtest ─────────────────────────────────────────────
profiles_raw = json.loads(Path("data/quant_inputs/wallet_alpha_profiles.json").read_text())
wp = {w: WalletProfile(
        wallet_address=w, trade_count=int(d["trade_count"]),
        realized_roi=float(d["realized_roi"]), lagged_follow_roi=float(d["lagged_follow_roi"]),
        max_drawdown=float(d["max_drawdown"]), concentration_score=float(d["concentration_score"]),
        category_edges={k: float(v) for k, v in d.get("category_edges", {}).items()},
      ) for w, d in profiles_raw.items()}

wa_rows = reader.load_jsonl("research/backtest/data/wallet_alpha_hf/market_snapshots.jsonl")
wa_adapter = WalletAlphaBacktestAdapter(profiles=wp, order_size_usdc=10.0)
run_markout(wa_adapter, wa_rows, "wallet_alpha (lagged_follow_roi 验证)")

# ── 2. T0 backtest（如果数据集存在）────────────────────────────────────
t0_path = Path("research/backtest/data/hf_crypto_resolved/market_snapshots.jsonl")
if t0_path.exists():
    t0_rows = reader.load_jsonl(str(t0_path))
    arb_config = ArbConfig.from_env(require_wallet=False)
    t0_adapter = T0BacktestAdapter(arb_config)
    # T0 detect 接口不同，直接用 from_rows
    t0_signals = t0_filled = 0
    t0_pnl = 0.0
    for row in t0_rows:
        adapter_inst, market = T0BacktestAdapter.from_rows(arb_config, row)
        opp = adapter_inst.detect(market)
        if opp is None:
            continue
        t0_signals += 1
        order = adapter_inst.to_order_request(opp)
        exe = exec_model.simulate(order, {**row, **order})
        if exe.filled:
            t0_filled += 1
            pnl = opp.gross_edge * exe.filled_size - exe.fees_paid
            t0_pnl += pnl
    print(f"\n=== T0 (hf_crypto_resolved) ===")
    print(f"信号={t0_signals} 成交={t0_filled} PnL=${t0_pnl:.2f}")
else:
    print(f"\nT0 数据集尚未构建，跳过")
