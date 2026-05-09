"""Diagnose Polymarket wallet/API accessibility without placing orders."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.client_factory import build_readonly_client, build_trading_client
from polymarket_arb.config import ArbConfig
from polymarket_arb.execution_engine import ExecutionEngine
from polymarket_arb.portfolio_sync import PortfolioSync


def _mask(value: str, *, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return "<empty>"
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return f"{value[:prefix]}***{value[-suffix:]}"


def _ok_result(name: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"check": name, "ok": True}
    if details:
        payload["details"] = details
    return payload


def _err_result(name: str, error: Exception | str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"check": name, "ok": False, "error": str(error)}
    if details:
        payload["details"] = details
    return payload


def _build_config(dotenv_path: str | None, *, signature_type_override: int | None = None) -> ArbConfig:
    cfg = ArbConfig.from_env(dotenv_path=dotenv_path, require_wallet=True)
    if signature_type_override is None:
        return cfg
    return ArbConfig(
        **{
            **cfg.__dict__,
            "signature_type": int(signature_type_override),
        }
    )


def _check_signer(config: ArbConfig) -> dict[str, Any]:
    owner = _owner_address_from_private_key(config.private_key, config.chain_id)
    return _ok_result(
        "signer",
        {
            "owner_address": owner,
            "owner_masked": _mask(owner),
            "funder_address": config.funder_address,
            "funder_masked": _mask(config.funder_address),
            "owner_equals_funder": owner.lower() == config.funder_address.lower(),
            "signature_type": config.signature_type,
        },
    )


def _check_readonly_clob(config: ArbConfig) -> dict[str, Any]:
    client = build_readonly_client(config)
    started = time.perf_counter()
    server_time = client.get_server_time()
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    return _ok_result(
        "readonly_clob",
        {
            "host": config.clob_host,
            "server_time": server_time,
            "latency_ms": elapsed_ms,
        },
    )


def _check_trading_auth(config: ArbConfig) -> tuple[dict[str, Any], Any | None]:
    started = time.perf_counter()
    client = build_trading_client(config)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    details = {
        "host": config.clob_host,
        "funder_masked": _mask(config.funder_address),
        "signature_type": config.signature_type,
        "latency_ms": elapsed_ms,
    }
    return _ok_result("trading_auth", details), client


def _check_available_balance(config: ArbConfig, trading_client: Any) -> dict[str, Any]:
    engine = ExecutionEngine(config, trading_client)
    available = engine.get_available_collateral_balance()
    raw_response = None
    try:
        balance_params = _build_balance_allowance_params(config)
        if balance_params is None:
            raw_response = trading_client.get_balance_allowance()
        else:
            raw_response = trading_client.get_balance_allowance(balance_params)
    except TypeError:
        try:
            if config.signature_type == 3:
                raise
            raw_response = trading_client.get_balance_allowance(_build_legacy_balance_allowance_params(-1))
        except Exception as exc:
            raw_response = {"error": str(exc)}
    except Exception as exc:
        raw_response = {"error": str(exc)}

    details = {
        "available_collateral": available,
        "raw_balance_allowance": raw_response,
    }
    if available is None:
        return _err_result("available_balance", "balance_check_unavailable", details)
    return _ok_result("available_balance", details)


def _owner_address_from_private_key(private_key: str, chain_id: int) -> str:
    try:
        from py_clob_client.signer import Signer

        return Signer(private_key, chain_id).address()
    except ImportError:
        from eth_account import Account

        return Account.from_key(private_key).address


def _build_balance_allowance_params(config: ArbConfig) -> Any | None:
    if config.signature_type == 3:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams, SignatureTypeV2

        return BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=getattr(SignatureTypeV2, "POLY_1271", 3),
        )

    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        return None
    return BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)


def _build_legacy_balance_allowance_params(signature_type: int) -> Any:
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    return BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)


def _check_portfolio_sync(config: ArbConfig) -> dict[str, Any]:
    sync = PortfolioSync(config)
    snapshot = sync.refresh()
    return _ok_result(
        "portfolio_sync",
        {
            "source_address": snapshot.source_address,
            "positions": len(snapshot.positions),
            "realized_daily_pnl": snapshot.realized_daily_pnl,
            "synced_at": snapshot.synced_at,
        },
    )


def _run_once(config: ArbConfig, *, skip_portfolio_sync: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "checks": [],
        "config": {
            "clob_host": config.clob_host,
            "gamma_host": config.gamma_host,
            "data_api_host": config.data_api_host,
            "signature_type": config.signature_type,
            "chain_id": config.chain_id,
            "funder_masked": _mask(config.funder_address),
            "portfolio_sync_enabled": config.portfolio_sync_enabled,
        },
    }

    try:
        report["checks"].append(_check_signer(config))
    except Exception as exc:
        report["checks"].append(_err_result("signer", exc))

    try:
        report["checks"].append(_check_readonly_clob(config))
    except Exception as exc:
        report["checks"].append(_err_result("readonly_clob", exc, {"host": config.clob_host}))

    trading_client = None
    try:
        auth_result, trading_client = _check_trading_auth(config)
        report["checks"].append(auth_result)
    except Exception as exc:
        report["checks"].append(
            _err_result(
                "trading_auth",
                exc,
                {
                    "funder_masked": _mask(config.funder_address),
                    "signature_type": config.signature_type,
                },
            )
        )

    if trading_client is not None:
        try:
            report["checks"].append(_check_available_balance(config, trading_client))
        except Exception as exc:
            report["checks"].append(_err_result("available_balance", exc))

    if not skip_portfolio_sync:
        try:
            report["checks"].append(_check_portfolio_sync(config))
        except Exception as exc:
            report["checks"].append(_err_result("portfolio_sync", exc, {"host": config.data_api_host}))

    report["ok"] = all(bool(item.get("ok")) for item in report["checks"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Polymarket wallet/API accessibility without placing orders")
    parser.add_argument("--dotenv-path", default=".env", help="Path to .env file")
    parser.add_argument(
        "--signature-type",
        type=int,
        default=None,
        help="Override POLYMARKET_SIGNATURE_TYPE for this check only",
    )
    parser.add_argument(
        "--skip-portfolio-sync",
        action="store_true",
        help="Skip Data API positions/closed-positions check",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Run the diagnostic multiple times and print an aggregate summary",
    )
    args = parser.parse_args()

    report: dict[str, Any] = {
        "dotenv_path": str(args.dotenv_path),
        "attempts": [],
    }

    try:
        config = _build_config(args.dotenv_path, signature_type_override=args.signature_type)
    except Exception as exc:
        report["attempts"].append({"attempt": 1, "checks": [_err_result("config", exc)], "ok": False})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    report["config"] = {
        "clob_host": config.clob_host,
        "gamma_host": config.gamma_host,
        "data_api_host": config.data_api_host,
        "signature_type": config.signature_type,
        "chain_id": config.chain_id,
        "funder_masked": _mask(config.funder_address),
        "portfolio_sync_enabled": config.portfolio_sync_enabled,
        "retries": max(1, int(args.retries)),
    }

    retries = max(1, int(args.retries))
    aggregate: dict[str, dict[str, int]] = {}
    overall_ok = False

    for attempt in range(1, retries + 1):
        attempt_report = _run_once(config, skip_portfolio_sync=args.skip_portfolio_sync)
        attempt_report["attempt"] = attempt
        report["attempts"].append(attempt_report)
        overall_ok = overall_ok or bool(attempt_report["ok"])

        for check in attempt_report["checks"]:
            name = str(check.get("check"))
            bucket = aggregate.setdefault(name, {"ok": 0, "fail": 0})
            if check.get("ok"):
                bucket["ok"] += 1
            else:
                bucket["fail"] += 1

    report["summary"] = aggregate
    report["ok"] = overall_ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
