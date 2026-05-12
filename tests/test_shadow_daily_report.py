"""Smoke tests for scripts/shadow_daily_report.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "shadow_daily_report.py"


def _write_fills(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _run(args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        check=True,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_report_aggregates_per_tier_and_overall(tmp_path: Path) -> None:
    date = "2026-05-10"
    fills = [
        {
            "tier": "T0_STRUCTURAL",
            "market_id": "cid-1",
            "is_maker": False,
            "fee": 0.01,
            "slippage": 0.0,
            "intended_price": 0.50,
            "result": {"status": "filled", "filled_size": 10.0, "avg_fill_price": 0.50},
        },
        {
            "tier": "T0_STRUCTURAL",
            "market_id": "cid-1",
            "is_maker": False,
            "fee": 0.01,
            "slippage": 0.002,
            "intended_price": 0.45,
            "result": {"status": "filled", "filled_size": 10.0, "avg_fill_price": 0.452},
        },
        {
            "tier": "T3_MAKER",
            "market_id": "cid-2",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.0,
            "intended_price": 0.30,
            "result": {"status": "pending", "filled_size": 0.0, "avg_fill_price": None},
        },
        {
            "tier": "T3_MAKER",
            "market_id": "cid-2",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.0,
            "intended_price": 0.30,
            "result": {"status": "filled", "filled_size": 20.0, "avg_fill_price": 0.30},
        },
    ]
    _write_fills(tmp_path / f"{date}.virtual_fills.ndjson", fills)

    payload = _run(["--telemetry-dir", str(tmp_path), "--date", date])
    assert payload["overall"]["rows"] == 4
    assert payload["overall"]["filled"] == 3
    assert payload["overall"]["maker_filled"] == 1
    assert payload["overall"]["taker_filled"] == 2

    t0 = payload["by_tier"]["T0_STRUCTURAL"]
    assert t0["filled"] == 2
    assert t0["maker_ratio"] == 0.0
    assert t0["fill_rate"] == 1.0

    t3 = payload["by_tier"]["T3_MAKER"]
    assert t3["filled"] == 1
    assert t3["pending"] == 1
    assert t3["maker_ratio"] == 1.0

    top = payload["top_markets_by_fills"]
    assert top[0]["market_id"] in {"cid-1", "cid-2"}


def test_report_handles_missing_telemetry(tmp_path: Path) -> None:
    payload = _run(["--telemetry-dir", str(tmp_path), "--date", "2026-05-10"])
    assert payload["overall"]["rows"] == 0
    assert "warning" in payload
