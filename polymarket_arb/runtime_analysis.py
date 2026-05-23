"""Runtime artifact summarization for logs, telemetry, and ticks."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def _load_ndjson_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(paths):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _telemetry_category_paths(telemetry_dir: Path, category: str) -> list[Path]:
    paths = {
        *telemetry_dir.glob(f"*.{category}.ndjson"),
        *telemetry_dir.glob(f"*.{category}.*.ndjson"),
    }
    return sorted(paths)


def _detect_run_mode(log_text: str) -> str:
    if "DRY RUN" in log_text:
        return "dry_run"
    if "LIVE" in log_text:
        return "live"
    return "unknown"


def _summarize_opportunities(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_stage = Counter(str(row.get("stage") or "unknown") for row in rows)
    top_events: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("stage")) != "verified":
            continue
        top_events.append(
            {
                "event_id": str(row.get("event_id") or ""),
                "event_title": str(row.get("event_title") or ""),
                "arb_type": str(row.get("arb_type") or ""),
                "net_edge": float(row.get("net_edge") or 0.0),
                "max_executable_size": float(row.get("max_executable_size") or 0.0),
            }
        )
    top_events.sort(
        key=lambda item: item["net_edge"] * item["max_executable_size"],
        reverse=True,
    )
    return {
        "detected": by_stage.get("detected", 0),
        "verified": by_stage.get("verified", 0),
        "by_stage": dict(sorted(by_stage.items())),
        "top_verified": top_events[:5],
    }


def _summarize_trades(rows: list[dict[str, Any]]) -> dict[str, Any]:
    simulated_rows = [row for row in rows if bool(row.get("simulated", False))]
    reported_successes = [row for row in rows if bool(row.get("arb_success", False))]
    live_successes = [row for row in rows if bool(row.get("live_execution_success", False))]
    expected_profit_total = round(
        sum(float(row.get("trade_outcome_estimate") or 0.0) for row in reported_successes),
        6,
    )
    return {
        "attempts": len(rows),
        "reported_successes": len(reported_successes),
        "live_successes": len(live_successes),
        "simulated_successes": sum(1 for row in simulated_rows if bool(row.get("arb_success", False))),
        "simulated_attempts": len(simulated_rows),
        "expected_profit_total": expected_profit_total,
    }


def _summarize_signals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_tier = Counter(str(row.get("tier") or "UNKNOWN") for row in rows)
    by_type = Counter(str(row.get("signal_type") or "unknown") for row in rows)
    return {
        "count": len(rows),
        "by_tier": dict(sorted(by_tier.items())),
        "top_signal_types": [
            {"signal_type": signal_type, "count": count}
            for signal_type, count in by_type.most_common(10)
        ],
        "quant_strategies": _summarize_quant_strategy_signals(rows),
    }


def _summarize_pnl_attribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [
        row for row in rows
        if str(row.get("event") or "") in {"position_closed", "position_partially_closed"}
    ]
    by_source: dict[str, dict[str, Any]] = {}
    by_component: dict[str, dict[str, Any]] = {}
    for row in closed:
        pnl = _to_float(row.get("realized_pnl"))
        fees = _to_float(row.get("fees"))
        context = row.get("decision_context") if isinstance(row.get("decision_context"), dict) else {}
        source = str(context.get("signal_source") or row.get("signal_source") or "unknown")
        components = _component_list(context.get("signal_components") or row.get("signal_components"))
        if not components:
            components = [source]
        _add_pnl_bucket(by_source, source, pnl=pnl, fees=fees)
        for component in components:
            _add_pnl_bucket(by_component, component, pnl=pnl, fees=fees)
    return {
        "closed_positions": len(closed),
        "realized_pnl": round(sum(_to_float(row.get("realized_pnl")) for row in closed), 6),
        "fees": round(sum(_to_float(row.get("fees")) for row in closed), 6),
        "by_source": _finalize_pnl_buckets(by_source),
        "by_component": _finalize_pnl_buckets(by_component),
    }


def _summarize_strategy_performance(
    *,
    signal_rows: list[dict[str, Any]],
    execution_rows: list[dict[str, Any]],
    lifecycle_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    by_tier: dict[str, dict[str, Any]] = {}
    by_category: dict[str, dict[str, Any]] = {}
    by_signal_type: dict[str, dict[str, Any]] = {}

    for row in signal_rows:
        tier = _row_tier(row)
        category = _row_category(row)
        signal_type = _row_signal_type(row)
        for bucket_map, key in (
            (by_tier, tier),
            (by_category, category),
            (by_signal_type, signal_type),
        ):
            _add_perf_bucket(bucket_map, key, signals=1)

    for row in execution_rows:
        tier = _row_tier(row)
        category = _row_category(row)
        signal_type = _row_signal_type(row)
        status = str(row.get("status") or "").lower()
        notional = _execution_notional(row)
        expected_edge = _execution_expected_edge_usdc(row, notional)
        executions = 1 if status in {"executed", "simulated", "submitted"} else 0
        skipped = 1 if status == "skipped" or str(row.get("reason") or "") else 0
        for bucket_map, key in (
            (by_tier, tier),
            (by_category, category),
            (by_signal_type, signal_type),
        ):
            _add_perf_bucket(
                bucket_map,
                key,
                attempts=1,
                executions=executions,
                skipped=skipped,
                notional_usdc=notional,
                expected_edge_usdc=expected_edge,
            )

    closed = [
        row for row in lifecycle_rows
        if str(row.get("event") or "") in {"position_closed", "position_partially_closed"}
    ]
    for row in closed:
        tier = _row_tier(row)
        category = _row_category(row)
        signal_type = _row_signal_type(row)
        pnl = _to_float(row.get("realized_pnl"))
        fees = _to_float(row.get("fees"))
        for bucket_map, key in (
            (by_tier, tier),
            (by_category, category),
            (by_signal_type, signal_type),
        ):
            _add_perf_bucket(bucket_map, key, realized_pnl=pnl, fees=fees)

    return {
        "by_tier": _finalize_perf_buckets(by_tier),
        "by_category": _finalize_perf_buckets(by_category),
        "by_signal_type": _finalize_perf_buckets(by_signal_type),
    }


def _summarize_quant_strategy_signals(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups = {
        "logical_constraint": "logical_constraint_",
        "event_calendar": "event_calendar_",
        "wallet_alpha": "wallet_alpha_",
    }
    out: dict[str, dict[str, Any]] = {}
    for name, prefix in groups.items():
        subset = [
            row for row in rows
            if str(row.get("signal_type") or "").startswith(prefix)
        ]
        if not subset:
            continue
        edges = [float(row.get("expected_edge") or 0.0) for row in subset]
        confidences = [float(row.get("confidence") or 0.0) for row in subset]
        submitted = sum(1 for row in subset if bool(row.get("submitted", False)))
        out[name] = {
            "count": len(subset),
            "submitted": submitted,
            "rejected": len(subset) - submitted,
            "avg_expected_edge_bps": round(sum(edges) / len(edges), 6) if edges else 0.0,
            "avg_confidence": round(sum(confidences) / len(confidences), 6) if confidences else 0.0,
        }
    return out


def _summarize_ticks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    event_ids = {str(row.get("event_id") or "") for row in rows if row.get("event_id")}
    condition_ids = {str(row.get("condition_id") or "") for row in rows if row.get("condition_id")}
    token_ids = {str(row.get("token_id") or "") for row in rows if row.get("token_id")}
    return {
        "records": len(rows),
        "unique_events": len(event_ids),
        "unique_conditions": len(condition_ids),
        "unique_tokens": len(token_ids),
    }


def _add_pnl_bucket(bucket_map: dict[str, dict[str, Any]], key: str, *, pnl: float, fees: float) -> None:
    bucket = bucket_map.setdefault(
        key or "unknown",
        {"closed_positions": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0, "fees": 0.0},
    )
    bucket["closed_positions"] += 1
    bucket["wins"] += 1 if pnl > 0 else 0
    bucket["losses"] += 1 if pnl < 0 else 0
    bucket["realized_pnl"] += pnl
    bucket["fees"] += fees


def _add_perf_bucket(
    bucket_map: dict[str, dict[str, Any]],
    key: str,
    *,
    signals: int = 0,
    attempts: int = 0,
    executions: int = 0,
    skipped: int = 0,
    notional_usdc: float = 0.0,
    expected_edge_usdc: float = 0.0,
    realized_pnl: float = 0.0,
    fees: float = 0.0,
) -> None:
    bucket = bucket_map.setdefault(
        key or "unknown",
        {
            "signals": 0,
            "attempts": 0,
            "executions": 0,
            "skipped": 0,
            "notional_usdc": 0.0,
            "expected_edge_usdc": 0.0,
            "realized_pnl": 0.0,
            "fees": 0.0,
        },
    )
    bucket["signals"] += int(signals)
    bucket["attempts"] += int(attempts)
    bucket["executions"] += int(executions)
    bucket["skipped"] += int(skipped)
    bucket["notional_usdc"] += float(notional_usdc)
    bucket["expected_edge_usdc"] += float(expected_edge_usdc)
    bucket["realized_pnl"] += float(realized_pnl)
    bucket["fees"] += float(fees)


def _finalize_perf_buckets(bucket_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, bucket in sorted(bucket_map.items()):
        notional = float(bucket["notional_usdc"])
        fees = float(bucket["fees"])
        realized = float(bucket["realized_pnl"])
        out[key] = {
            "signals": int(bucket["signals"]),
            "attempts": int(bucket["attempts"]),
            "executions": int(bucket["executions"]),
            "skipped": int(bucket["skipped"]),
            "notional_usdc": round(notional, 6),
            "expected_edge_usdc": round(float(bucket["expected_edge_usdc"]), 6),
            "realized_pnl": round(realized, 6),
            "fees": round(fees, 6),
            "net_pnl_after_fees": round(realized - fees, 6),
            "fee_drag_bps": round(fees / notional * 10_000.0, 6) if notional > 0 else 0.0,
        }
    return out


def _finalize_pnl_buckets(bucket_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, bucket in sorted(bucket_map.items()):
        count = int(bucket["closed_positions"])
        out[key] = {
            "closed_positions": count,
            "wins": int(bucket["wins"]),
            "losses": int(bucket["losses"]),
            "win_rate": round(float(bucket["wins"]) / count, 6) if count else 0.0,
            "realized_pnl": round(float(bucket["realized_pnl"]), 6),
            "fees": round(float(bucket["fees"]), 6),
        }
    return out


def _component_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _row_tier(row: dict[str, Any]) -> str:
    context = row.get("decision_context") if isinstance(row.get("decision_context"), dict) else {}
    return str(row.get("tier") or context.get("tier") or "UNKNOWN")


def _row_category(row: dict[str, Any]) -> str:
    context = row.get("decision_context") if isinstance(row.get("decision_context"), dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    execution_check = row.get("execution_check") if isinstance(row.get("execution_check"), dict) else {}
    return str(
        row.get("category")
        or context.get("category")
        or payload.get("category")
        or execution_check.get("category")
        or "unknown"
    )


def _row_signal_type(row: dict[str, Any]) -> str:
    context = row.get("decision_context") if isinstance(row.get("decision_context"), dict) else {}
    return str(row.get("signal_type") or context.get("signal_type") or context.get("signal_source") or "unknown")


def _execution_notional(row: dict[str, Any]) -> float:
    for key in ("submitted_notional", "notional_usdc", "requested_notional"):
        value = _to_float(row.get(key))
        if value > 0:
            return value
    trades = row.get("trades")
    if isinstance(trades, list):
        total = 0.0
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            price = _to_float(trade.get("economic_cost")) or _to_float(trade.get("price")) or _to_float(trade.get("fill_price"))
            size = _to_float(trade.get("fill_size")) or _to_float(trade.get("size"))
            total += max(0.0, price * size)
        if total > 0:
            return total
    return 0.0


def _execution_expected_edge_usdc(row: dict[str, Any], notional: float) -> float:
    direct = _to_float(row.get("expected_edge_usdc"))
    if direct:
        return direct
    per_share = _to_float(row.get("expected_edge_per_share"))
    size = _to_float(row.get("filled_size")) or _to_float(row.get("submitted_size")) or _to_float(row.get("size"))
    if per_share and size:
        return per_share * size
    execution_check = row.get("execution_check") if isinstance(row.get("execution_check"), dict) else {}
    net_edge_bps = _to_float(execution_check.get("net_edge_bps"))
    if net_edge_bps and notional:
        return notional * net_edge_bps / 10_000.0
    return 0.0


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _build_issues(*, run_mode: str, trade_rows: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    if run_mode == "dry_run":
        issues.append("Dry run session: profits and fills are simulated, not realized PnL.")

    for row in trade_rows:
        trades = row.get("trades") or []
        if (
            not row.get("arb_success", False)
            and trades
            and all(str(trade.get("status") or "") == "filled" for trade in trades)
            and float(row.get("trade_outcome_estimate") or 0.0) < 0
        ):
            issues.append(
                "Telemetry mismatch detected: all legs filled but trade outcome estimate is negative."
            )
            break

    return issues


def _normalize_trade_row_for_reporting(row: dict[str, Any], *, run_mode: str) -> dict[str, Any]:
    normalized = dict(row)
    trades = normalized.get("trades") or []
    all_filled = bool(trades) and all(str(trade.get("status") or "") == "filled" for trade in trades)
    simulated = bool(normalized.get("simulated", False))
    live_success = bool(normalized.get("live_execution_success", False))
    reported_success = bool(normalized.get("arb_success", False))

    # Legacy dry-run telemetry stored arb_success=false and a negative trade_outcome_estimate
    # even when all simulated legs were filled. Recover the intent for reporting.
    if run_mode == "dry_run" and not simulated and not live_success and all_filled:
        simulated = True
        reported_success = True
        normalized["trade_outcome_estimate"] = round(
            float(normalized.get("expected_net_edge") or 0.0) * float(normalized.get("requested_size") or 0.0),
            6,
        )

    normalized["simulated"] = simulated
    normalized["live_execution_success"] = live_success
    normalized["arb_success"] = reported_success
    return normalized


def summarize_runtime_artifacts(
    *,
    log_path: str | Path,
    telemetry_dir: str | Path,
    ticks_dir: str | Path,
) -> dict[str, Any]:
    log_path = Path(log_path)
    telemetry_dir = Path(telemetry_dir)
    ticks_dir = Path(ticks_dir)

    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    opportunity_rows = _load_ndjson_rows(_telemetry_category_paths(telemetry_dir, "opportunities"))
    trade_rows = _load_ndjson_rows(_telemetry_category_paths(telemetry_dir, "trades"))
    signal_rows = _load_ndjson_rows(_telemetry_category_paths(telemetry_dir, "strategy_signals"))
    execution_rows = _load_ndjson_rows(_telemetry_category_paths(telemetry_dir, "strategy_executions"))
    lifecycle_rows = _load_ndjson_rows(_telemetry_category_paths(telemetry_dir, "positions_lifecycle"))
    tick_rows = _load_ndjson_rows(list(ticks_dir.glob("*.ndjson")))

    run_mode = _detect_run_mode(log_text)
    trade_rows = [
        _normalize_trade_row_for_reporting(row, run_mode=run_mode)
        for row in trade_rows
    ]
    opportunities = _summarize_opportunities(opportunity_rows)
    trades = _summarize_trades(trade_rows)
    signals = _summarize_signals(signal_rows)
    pnl_attribution = _summarize_pnl_attribution(lifecycle_rows)
    strategy_performance = _summarize_strategy_performance(
        signal_rows=signal_rows,
        execution_rows=execution_rows,
        lifecycle_rows=lifecycle_rows,
    )
    ticks = _summarize_ticks(tick_rows)
    issues = _build_issues(run_mode=run_mode, trade_rows=trade_rows)

    return {
        "run_mode": run_mode,
        "arbitrage_available": opportunities["detected"] > 0,
        "opportunities": opportunities,
        "trades": trades,
        "signals": signals,
        "pnl_attribution": pnl_attribution,
        "strategy_performance": strategy_performance,
        "ticks": ticks,
        "issues": issues,
    }
