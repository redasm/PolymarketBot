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
    tick_rows = _load_ndjson_rows(list(ticks_dir.glob("*.ndjson")))

    run_mode = _detect_run_mode(log_text)
    trade_rows = [
        _normalize_trade_row_for_reporting(row, run_mode=run_mode)
        for row in trade_rows
    ]
    opportunities = _summarize_opportunities(opportunity_rows)
    trades = _summarize_trades(trade_rows)
    signals = _summarize_signals(signal_rows)
    ticks = _summarize_ticks(tick_rows)
    issues = _build_issues(run_mode=run_mode, trade_rows=trade_rows)

    return {
        "run_mode": run_mode,
        "arbitrage_available": opportunities["detected"] > 0,
        "opportunities": opportunities,
        "trades": trades,
        "signals": signals,
        "ticks": ticks,
        "issues": issues,
    }
