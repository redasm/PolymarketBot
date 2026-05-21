"""Scan public/read-only sources into opt-in quant strategy JSON inputs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.ai_provider import create_provider
from polymarket_arb.config import ArbConfig
from polymarket_arb.market_scanner import MarketScanner
from polymarket_arb.market_scanner import _parse_event
from polymarket_arb.quant_input_scanner import (
    DataApiWalletTradeClient,
    build_wallet_markouts_from_trade_rows,
    build_wallet_markouts_from_shadow_rows,
    build_wallet_observations_from_trades,
    build_wallet_profiles_from_markout_rows,
    discover_wallets_from_trades,
    generate_logical_constraint_candidates,
    promote_wallet_profiles_from_markout_rows,
    select_logical_constraints_with_llm,
)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("rows") or payload.get("data") or payload.get("items") or []
        return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _load_events(path: Path):
    return [event for row in _load_rows(path) if (event := _parse_event(row)) is not None]


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan read-only data into quant strategy inputs")
    sub = parser.add_subparsers(dest="kind", required=True)

    p_candidates = sub.add_parser("logical-candidates", help="Generate same-event relation candidates")
    p_candidates.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_candidates.add_argument("--events-json", default=None, help="Gamma /events JSON export")
    p_candidates.add_argument("--fetch-gamma", action="store_true", help="Fetch active events from Gamma directly")
    p_candidates.add_argument("--dotenv-path", default=None)
    p_candidates.add_argument("--event-limit", type=int, default=50)
    p_candidates.add_argument("--min-liquidity", type=float, default=0.0)
    p_candidates.add_argument("--min-volume-24h", type=float, default=0.0)
    p_candidates.add_argument("--max-markets-per-event", type=int, default=40)
    p_candidates.add_argument("--max-pairs-per-event", type=int, default=80)

    p_rules = sub.add_parser("logical-rules-llm", help="Use configured LLM to select deterministic rules")
    p_rules.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_rules.add_argument("--candidates", required=True, help="Candidate JSON from logical-candidates")
    p_rules.add_argument("--dotenv-path", default=None)
    p_rules.add_argument("--min-violation-bps", type=float, default=250.0)
    p_rules.add_argument("--max-candidates", type=int, default=40)
    p_rules.add_argument("--rules-expires-sec", type=float, default=30 * 60.0)

    p_obs = sub.add_parser("wallet-observations", help="Fetch wallet trades and emit observation JSON")
    p_obs.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_obs.add_argument("--wallet", action="append", required=True, help="Wallet address; repeatable")
    p_obs.add_argument("--data-api-host", default="https://data-api.polymarket.com")
    p_obs.add_argument("--limit", type=int, default=200)

    p_auto_obs = sub.add_parser("auto-wallet-observations", help="Discover active wallets then emit observations")
    p_auto_obs.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_auto_obs.add_argument("--data-api-host", default="https://data-api.polymarket.com")
    p_auto_obs.add_argument("--recent-limit", type=int, default=500)
    p_auto_obs.add_argument("--wallet-trade-limit", type=int, default=100)
    p_auto_obs.add_argument("--min-trades", type=int, default=3)
    p_auto_obs.add_argument("--min-notional", type=float, default=100.0)
    p_auto_obs.add_argument("--max-wallets", type=int, default=25)
    p_auto_obs.add_argument("--repeat-interval-sec", type=float, default=0.0)
    p_auto_obs.add_argument("--repeat-count", type=int, default=1, help="Use 0 to repeat forever")

    p_profiles = sub.add_parser("wallet-profiles", help="Build wallet profiles from offline markout rows")
    p_profiles.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_profiles.add_argument("--input", required=True, help="CSV/JSON with wallet markout rows")
    p_profiles.add_argument("--min-trades", type=int, default=30)

    p_promote = sub.add_parser("promote-wallet-profiles", help="Promote only validated wallet profiles")
    p_promote.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_promote.add_argument("--input", required=True, help="CSV/JSON with wallet markout rows")
    p_promote.add_argument("--min-trades", type=int, default=30)
    p_promote.add_argument("--min-lagged-roi", type=float, default=0.04)
    p_promote.add_argument("--max-concentration", type=float, default=0.35)
    p_promote.add_argument("--max-drawdown", type=float, default=0.35)
    p_promote.add_argument("--holdout-sec", type=float, default=24 * 3600.0)
    p_promote.add_argument("--min-t-stat", type=float, default=2.0)
    p_promote.add_argument("--profile-expires-sec", type=float, default=30 * 60.0)
    p_promote.add_argument("--repeat-interval-sec", type=float, default=0.0)
    p_promote.add_argument("--repeat-count", type=int, default=1, help="Use 0 to repeat forever")

    p_markouts = sub.add_parser("wallet-markouts-from-telemetry", help="Build wallet markouts from shadow telemetry")
    p_markouts.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_markouts.add_argument("--telemetry-dir", default="data/telemetry")
    p_markouts.add_argument("--date", action="append", default=None, help="UTC date YYYY-MM-DD; repeatable")
    p_markouts.add_argument("--lookback-days", type=int, default=1, help="Used when --date is omitted")
    p_markouts.add_argument("--repeat-interval-sec", type=float, default=0.0)
    p_markouts.add_argument("--repeat-count", type=int, default=1, help="Use 0 to repeat forever")

    p_trade_markouts = sub.add_parser(
        "wallet-markouts-from-recent-trades",
        help="Build independent wallet markouts from public recent trades",
    )
    p_trade_markouts.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_trade_markouts.add_argument("--data-api-host", default="https://data-api.polymarket.com")
    p_trade_markouts.add_argument("--recent-limit", type=int, default=1000)
    p_trade_markouts.add_argument("--wallet-trade-limit", type=int, default=200)
    p_trade_markouts.add_argument("--min-trades", type=int, default=3)
    p_trade_markouts.add_argument("--min-notional", type=float, default=100.0)
    p_trade_markouts.add_argument("--max-wallets", type=int, default=25)
    p_trade_markouts.add_argument("--lag-sec", type=float, default=300.0)
    p_trade_markouts.add_argument("--repeat-interval-sec", type=float, default=0.0)
    p_trade_markouts.add_argument("--repeat-count", type=int, default=1, help="Use 0 to repeat forever")

    p_auto_promote = sub.add_parser(
        "auto-promote-wallet-profiles",
        help="Build markouts from shadow telemetry then promote validated profiles",
    )
    p_auto_promote.add_argument("--output", default=None, help="Optional JSON output file; written atomically")
    p_auto_promote.add_argument("--telemetry-dir", default="data/telemetry")
    p_auto_promote.add_argument("--date", action="append", default=None, help="UTC date YYYY-MM-DD; repeatable")
    p_auto_promote.add_argument("--lookback-days", type=int, default=7, help="Used when --date is omitted")
    p_auto_promote.add_argument("--min-trades", type=int, default=30)
    p_auto_promote.add_argument("--min-lagged-roi", type=float, default=0.04)
    p_auto_promote.add_argument("--max-concentration", type=float, default=0.35)
    p_auto_promote.add_argument("--max-drawdown", type=float, default=0.35)
    p_auto_promote.add_argument("--holdout-sec", type=float, default=24 * 3600.0)
    p_auto_promote.add_argument("--min-t-stat", type=float, default=2.0)
    p_auto_promote.add_argument("--profile-expires-sec", type=float, default=30 * 60.0)
    p_auto_promote.add_argument("--repeat-interval-sec", type=float, default=0.0)
    p_auto_promote.add_argument("--repeat-count", type=int, default=1, help="Use 0 to repeat forever")

    args = parser.parse_args()

    repeat_interval = float(getattr(args, "repeat_interval_sec", 0.0) or 0.0)
    repeat_count = int(getattr(args, "repeat_count", 1) or 0)
    iteration = 0
    last_payload: Any = None
    while True:
        try:
            payload = _build_payload(args)
            last_payload = payload
        except Exception as exc:
            if repeat_interval <= 0:
                raise
            print(f"scan_quant_strategy_inputs iteration failed: {exc}", file=sys.stderr)
            payload = last_payload
        if payload is None:
            iteration += 1
            if repeat_interval <= 0 or (repeat_count > 0 and iteration >= repeat_count):
                break
            time.sleep(repeat_interval)
            continue
        output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if args.output:
            _write_json_atomically(Path(args.output), output)
        else:
            print(output)

        iteration += 1
        if repeat_interval <= 0 or (repeat_count > 0 and iteration >= repeat_count):
            break
        time.sleep(repeat_interval)
    return 0


def _build_payload(args) -> Any:
    if args.kind == "logical-candidates":
        if args.fetch_gamma:
            config = ArbConfig.from_env(args.dotenv_path, require_wallet=False)
            events = MarketScanner(config).fetch_active_events(limit=args.event_limit)
        elif args.events_json:
            events = _load_events(Path(args.events_json))
        else:
            raise SystemExit("logical-candidates requires --events-json or --fetch-gamma")
        return generate_logical_constraint_candidates(
            events,
            min_liquidity=args.min_liquidity,
            min_volume_24h=args.min_volume_24h,
            max_markets_per_event=args.max_markets_per_event,
            max_pairs_per_event=args.max_pairs_per_event,
        )
    if args.kind == "logical-rules-llm":
        config = ArbConfig.from_env(args.dotenv_path, require_wallet=False)
        provider = create_provider(config)
        rules = select_logical_constraints_with_llm(
            provider,
            _load_rows(Path(args.candidates)),
            min_violation_bps=args.min_violation_bps,
            max_candidates=args.max_candidates,
        )
        generated_at = time.time()
        return {
            "schema_version": 1,
            "generated_at": generated_at,
            "expires_at": generated_at + max(0.0, float(args.rules_expires_sec)),
            "rules": rules,
        }
    if args.kind == "wallet-observations":
        client = DataApiWalletTradeClient(args.data_api_host)
        trades: list[dict[str, Any]] = []
        for wallet in args.wallet:
            trades.extend(client.fetch_trades(wallet, limit=args.limit))
        return build_wallet_observations_from_trades(trades)
    if args.kind == "auto-wallet-observations":
        client = DataApiWalletTradeClient(args.data_api_host)
        recent_trades = client.fetch_recent_trades(limit=args.recent_limit)
        wallets = discover_wallets_from_trades(
            recent_trades,
            min_trades=args.min_trades,
            min_notional_usdc=args.min_notional,
            max_wallets=args.max_wallets,
        )
        trades = []
        for wallet in wallets:
            trades.extend(client.fetch_trades(wallet, limit=args.wallet_trade_limit))
        return build_wallet_observations_from_trades(trades)
    if args.kind == "wallet-profiles":
        return build_wallet_profiles_from_markout_rows(
            _load_rows(Path(args.input)),
            min_trades=args.min_trades,
        )
    if args.kind == "wallet-markouts-from-telemetry":
        return _build_wallet_markouts_from_telemetry(args)
    if args.kind == "wallet-markouts-from-recent-trades":
        client = DataApiWalletTradeClient(args.data_api_host)
        recent_trades = client.fetch_recent_trades(limit=args.recent_limit)
        wallets = discover_wallets_from_trades(
            recent_trades,
            min_trades=args.min_trades,
            min_notional_usdc=args.min_notional,
            max_wallets=args.max_wallets,
        )
        wallet_trades: list[dict[str, Any]] = []
        for wallet in wallets:
            wallet_trades.extend(client.fetch_trades(wallet, limit=args.wallet_trade_limit))
        return build_wallet_markouts_from_trade_rows(
            wallet_trades,
            recent_trades,
            lag_sec=args.lag_sec,
        )
    if args.kind == "auto-promote-wallet-profiles":
        return promote_wallet_profiles_from_markout_rows(
            _build_wallet_markouts_from_telemetry(args),
            min_trades=args.min_trades,
            min_lagged_roi=args.min_lagged_roi,
            max_concentration=args.max_concentration,
            max_drawdown=args.max_drawdown,
            holdout_sec=args.holdout_sec,
            min_t_stat=args.min_t_stat,
            profile_expires_sec=args.profile_expires_sec,
        )
    return promote_wallet_profiles_from_markout_rows(
        _load_rows(Path(args.input)),
        min_trades=args.min_trades,
        min_lagged_roi=args.min_lagged_roi,
        max_concentration=args.max_concentration,
        max_drawdown=args.max_drawdown,
        holdout_sec=args.holdout_sec,
        min_t_stat=args.min_t_stat,
        profile_expires_sec=args.profile_expires_sec,
    )


def _build_wallet_markouts_from_telemetry(args) -> list[dict[str, Any]]:
    telemetry_dir = Path(args.telemetry_dir)
    virtual_rows: list[dict[str, Any]] = []
    lifecycle_rows: list[dict[str, Any]] = []
    for date in _resolve_dates(args.date, lookback_days=args.lookback_days):
        virtual_rows.extend(_load_ndjson(telemetry_dir / f"{date}.virtual_fills.ndjson"))
        lifecycle_rows.extend(_load_ndjson(telemetry_dir / f"{date}.positions_lifecycle.ndjson"))
    return build_wallet_markouts_from_shadow_rows(
        virtual_fill_rows=virtual_rows,
        lifecycle_rows=lifecycle_rows,
    )


def _write_json_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _resolve_dates(raw_dates: list[str] | None, *, lookback_days: int) -> list[str]:
    if raw_dates:
        return raw_dates
    days = max(1, int(lookback_days))
    end = datetime.strptime(_today_utc(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = end - timedelta(days=days - 1)
    return [
        (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days)
    ]


def _load_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
