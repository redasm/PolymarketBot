"""
两个任务并行：
1. wallet_alpha: 从 daily_aligned 按跟单钱包地址过滤，计算 lagged_follow_roi，生成 wallet_profiles.json
2. T0: 从 daily_aligned 过滤 crypto resolved 市场，重建 BBO 快照，生成 backtest dataset
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract followed wallets from position telemetry")
    parser.add_argument("--telemetry-dir", type=Path, default=Path("data/telemetry"))
    parser.add_argument("--output", type=Path, default=Path("analysis/tracked_wallets.json"))
    args = parser.parse_args()

    wallet_addrs = set()
    for path in sorted(args.telemetry_dir.glob("*.positions_lifecycle.ndjson")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            context = row.get("decision_context") or {}
            wallet = context.get("wallet_address") or context.get("source_wallet") or context.get("alpha_wallet")
            if wallet and wallet != "unknown":
                wallet_addrs.add(str(wallet).lower())

    print(f"跟单钱包数: {len(wallet_addrs)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sorted(wallet_addrs), indent=2), encoding="utf-8")
    print(f"已保存到: {args.output}")


if __name__ == "__main__":
    main()
