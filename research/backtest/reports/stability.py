"""Aggregate backtest scan outputs into stability summaries."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


def build_stability_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "dataset_count": 0,
            "positive_dataset_count": 0,
            "positive_rate": 0.0,
            "avg_net_pnl": 0.0,
            "avg_profit_factor": None,
            "finite_profit_factor_count": 0,
            "infinite_profit_factor_count": 0,
            "avg_win_rate": 0.0,
            "avg_fill_rate": 0.0,
            "avg_signal_edge_bps": 0.0,
            "execution_models": {},
            "datasets": [],
        }

    positive_count = sum(1 for row in rows if float(row.get("net_pnl", 0.0)) > 0)
    execution_models = Counter(str(row.get("execution_model") or "unknown") for row in rows)
    finite_profit_factors = [
        float(row["profit_factor"])
        for row in rows
        if row.get("profit_factor") is not None and not row.get("profit_factor_infinite")
    ]
    infinite_profit_factor_count = sum(1 for row in rows if row.get("profit_factor_infinite"))
    datasets = [
        {
            "dataset_name": row.get("dataset_name"),
            "net_pnl": row.get("net_pnl"),
            "profit_factor": row.get("profit_factor"),
            "profit_factor_infinite": bool(row.get("profit_factor_infinite")),
            "win_rate": row.get("win_rate"),
            "fill_rate": row.get("fill_rate"),
            "signal_count": row.get("total_signals"),
            "execution_model": row.get("execution_model"),
        }
        for row in sorted(rows, key=lambda item: str(item.get("dataset_name") or ""))
    ]
    return {
        "dataset_count": len(rows),
        "positive_dataset_count": positive_count,
        "positive_rate": round(positive_count / len(rows), 4),
        "avg_net_pnl": round(mean(float(row.get("net_pnl", 0.0)) for row in rows), 4),
        "avg_profit_factor": round(mean(finite_profit_factors), 4) if finite_profit_factors else None,
        "finite_profit_factor_count": len(finite_profit_factors),
        "infinite_profit_factor_count": infinite_profit_factor_count,
        "avg_win_rate": round(mean(float(row.get("win_rate", 0.0)) for row in rows), 4),
        "avg_fill_rate": round(mean(float(row.get("fill_rate", 0.0)) for row in rows), 4),
        "avg_signal_edge_bps": round(mean(float(row.get("avg_signal_edge_bps", 0.0)) for row in rows), 2),
        "execution_models": dict(execution_models),
        "datasets": datasets,
    }


def collect_best_scan_rows(output_dir: str | Path, *, strategy_name: str | None = None) -> list[dict[str, Any]]:
    out_dir = Path(output_dir)
    if not out_dir.exists():
        return []

    rows: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*_best_scan_result.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if strategy_name and payload.get("strategy_name") != strategy_name:
            continue
        rows.append(payload)
    return rows


def collect_parameter_scan_rows(output_dir: str | Path, *, strategy_name: str | None = None) -> list[dict[str, Any]]:
    out_dir = Path(output_dir)
    if not out_dir.exists():
        return []

    rows: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*_parameter_scan.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            if strategy_name and row.get("strategy_name") != strategy_name:
                continue
            rows.append(row)
    return rows


def build_parameter_stability_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        signature = (
            row.get("strategy_name"),
            row.get("execution_model"),
            row.get("scan_slippage_bps"),
            row.get("scan_latency_ms"),
            row.get("scan_fee_rate"),
            row.get("scan_queue_ahead_ratio"),
            row.get("scan_adverse_selection_bps"),
            row.get("scan_partial_fill_ratio"),
            row.get("scan_min_deviation"),
            row.get("scan_min_confidence"),
            row.get("scan_holding_period_ms"),
        )
        groups.setdefault(signature, []).append(row)

    ranked: list[dict[str, Any]] = []
    for signature, items in groups.items():
        positive_count = sum(1 for item in items if float(item.get("net_pnl", 0.0)) > 0)
        finite_profit_factors = [
            float(item["profit_factor"])
            for item in items
            if item.get("profit_factor") is not None and not item.get("profit_factor_infinite")
        ]
        ranked.append(
            {
                "signature": {
                    "strategy_name": signature[0],
                    "execution_model": signature[1],
                    "scan_slippage_bps": signature[2],
                    "scan_latency_ms": signature[3],
                    "scan_fee_rate": signature[4],
                    "scan_queue_ahead_ratio": signature[5],
                    "scan_adverse_selection_bps": signature[6],
                    "scan_partial_fill_ratio": signature[7],
                    "scan_min_deviation": signature[8],
                    "scan_min_confidence": signature[9],
                    "scan_holding_period_ms": signature[10],
                },
                "dataset_count": len(items),
                "positive_dataset_count": positive_count,
                "positive_rate": round(positive_count / len(items), 4),
                "avg_net_pnl": round(mean(float(item.get("net_pnl", 0.0)) for item in items), 4),
                "avg_finite_profit_factor": round(mean(finite_profit_factors), 4) if finite_profit_factors else None,
                "avg_win_rate": round(mean(float(item.get("win_rate", 0.0)) for item in items), 4),
                "avg_fill_rate": round(mean(float(item.get("fill_rate", 0.0)) for item in items), 4),
                "avg_signal_edge_bps": round(mean(float(item.get("avg_signal_edge_bps", 0.0)) for item in items), 2),
            }
        )

    ranked.sort(
        key=lambda row: (
            row["positive_rate"],
            row["avg_net_pnl"],
            row["avg_finite_profit_factor"] if row["avg_finite_profit_factor"] is not None else float("inf"),
            row["avg_fill_rate"],
        ),
        reverse=True,
    )
    return {
        "parameter_set_count": len(ranked),
        "ranked_parameter_sets": ranked,
    }


def save_stability_summary(summary: dict[str, Any], output_dir: str | Path, filename: str = "stability_summary.json") -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
