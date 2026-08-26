"""Reproducible wallet-alpha + T0 backtests using executable prices."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket_arb.config import ArbConfig
from polymarket_arb.strategies.wallet_alpha import WalletProfile
from research.backtest.adapters.quant_strategy_adapter import WalletAlphaBacktestAdapter
from research.backtest.adapters.t0_adapter import T0BacktestAdapter
from research.backtest.data.reader import BacktestDatasetReader
from research.backtest.execution_model.base import DepthVWAPExecutionModel, ExecutionModelConfig, estimate_binary_clob_fee


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_markout(adapter, rows, label: str, *, holding_ms: int, fee_rate: float, exec_model):
    rows = sorted(rows, key=lambda row: int(row.get("ts_ms", 0)))
    rows_by_market = defaultdict(list)
    for row in rows:
        rows_by_market[row["condition_id"]].append(row)
    wins = fills = signals = 0
    total_pnl = 0.0
    for market_rows in rows_by_market.values():
        for index, row in enumerate(market_rows):
            signal = adapter.detect(row)
            if signal is None:
                continue
            signals += 1
            order = adapter.to_order_request(signal, row)
            execution = exec_model.simulate(order, {**row, **order})
            if not execution.filled or execution.average_price is None:
                continue
            timestamp = int(row["ts_ms"])
            exit_row = next((candidate for candidate in market_rows[index + 1:] if int(candidate["ts_ms"]) >= timestamp + holding_ms), market_rows[-1])
            action = str(signal.payload.get("action", "BUY_YES")).upper()
            prefix = "no" if action == "BUY_NO" else "yes"
            best_bid = exit_row.get(f"{prefix}_best_bid")
            if best_bid is None:
                continue
            exit_price = float(best_bid)
            exit_fee = estimate_binary_clob_fee(exit_price, execution.filled_size, fee_rate)
            pnl = (exit_price - execution.average_price) * execution.filled_size - execution.fees_paid - exit_fee
            total_pnl += pnl
            fills += 1
            wins += pnl > 0
    result = {"strategy": label, "signals": signals, "fills": fills, "wins": wins, "losses": fills - wins, "win_rate": wins / fills if fills else 0.0, "net_pnl": total_pnl}
    print(f"\n=== {label} ===\n信号={signals} 成交={fills} 胜率={result['win_rate']:.1%} PnL=${total_pnl:.2f}")
    return result


def _wallet_profiles(path: Path) -> dict[str, WalletProfile]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {wallet: WalletProfile(wallet_address=wallet, trade_count=int(row["trade_count"]), realized_roi=float(row["realized_roi"]), lagged_follow_roi=float(row["lagged_follow_roi"]), max_drawdown=float(row["max_drawdown"]), concentration_score=float(row["concentration_score"]), category_edges={key: float(value) for key, value in row.get("category_edges", {}).items()}) for wallet, row in raw.items()}


def main() -> None:
    config = ArbConfig.from_env(require_wallet=False)
    parser = argparse.ArgumentParser(description="Run wallet-alpha and T0 research backtests")
    parser.add_argument("--profiles", type=Path, default=Path("data/quant_inputs/wallet_alpha_profiles.json"))
    parser.add_argument("--wallet-snapshots", type=Path, default=Path("research/backtest/data/wallet_alpha_hf/market_snapshots.jsonl"))
    parser.add_argument("--t0-snapshots", type=Path, default=Path("research/backtest/data/hf_crypto_resolved/market_snapshots.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("research/backtest/output/analysis_backtests.json"))
    parser.add_argument("--fee-rate", type=float, default=config.polymarket_taker_fee_rate)
    parser.add_argument("--holding-ms", type=int, default=300_000)
    parser.add_argument("--order-size-usdc", type=float, default=10.0)
    args = parser.parse_args()
    if not 0 <= args.fee_rate < 1:
        parser.error("--fee-rate must be in [0, 1)")
    missing = [path for path in (args.profiles, args.wallet_snapshots) if not path.is_file()]
    if missing:
        parser.error("missing required input(s): " + ", ".join(str(path) for path in missing))

    reader = BacktestDatasetReader(args.wallet_snapshots.parent)
    execution_model = DepthVWAPExecutionModel(ExecutionModelConfig(fee_rate=args.fee_rate, latency_ms=25, slippage_bps=20))
    results = [run_markout(WalletAlphaBacktestAdapter(profiles=_wallet_profiles(args.profiles), order_size_usdc=args.order_size_usdc), reader.load_jsonl(str(args.wallet_snapshots)), "wallet_alpha", holding_ms=args.holding_ms, fee_rate=args.fee_rate, exec_model=execution_model)]
    if args.t0_snapshots.is_file():
        t0_signals = t0_fills = 0
        t0_pnl = 0.0
        for row in reader.load_jsonl(str(args.t0_snapshots)):
            adapter, market = T0BacktestAdapter.from_rows(config, row)
            opportunity = adapter.detect(market)
            if opportunity is None:
                continue
            t0_signals += 1
            order = adapter.to_order_request(opportunity)
            execution = execution_model.simulate(order, {**row, **order})
            if execution.filled:
                t0_fills += 1
                t0_pnl += opportunity.gross_edge * execution.filled_size - execution.fees_paid
        results.append({"strategy": "t0", "signals": t0_signals, "fills": t0_fills, "net_pnl": t0_pnl})
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "fee_rate": args.fee_rate, "fee_source": "--fee-rate" if "--fee-rate" in sys.argv else "ArbConfig/POLYMARKET_TAKER_FEE_RATE", "holding_ms": args.holding_ms, "order_size_usdc": args.order_size_usdc, "inputs": {name: {"path": str(path.resolve()), "sha256": _sha256(path)} for name, path in {"profiles": args.profiles, "wallet_snapshots": args.wallet_snapshots, "t0_snapshots": args.t0_snapshots}.items()}, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"结果与输入 manifest: {args.output}")


if __name__ == "__main__":
    main()
