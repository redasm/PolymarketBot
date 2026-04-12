"""Backtest reporting helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from polymarket_arb.models import BacktestReport


def save_report(report: BacktestReport, output_dir: str | Path) -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.strategy_name}_{report.dataset_name}_report.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_recommended_params(params: dict, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_trade_log(rows: list[dict[str, Any]], output_dir: str | Path, report: BacktestReport) -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.strategy_name}_{report.dataset_name}_trades.jsonl"
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def save_scan_results(rows: list[dict[str, Any]], output_dir: str | Path, scan_name: str = "scan_results") -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{scan_name}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_best_scan_result(row: dict[str, Any], output_dir: str | Path, filename: str = "best_scan_result.json") -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
