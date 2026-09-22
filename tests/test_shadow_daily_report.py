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


def test_partial_fills_count_toward_fee_notional_and_slippage(tmp_path: Path) -> None:
    """P2 regression: a partial fill is real money — its filled_size,
    fee, and slippage must show up in the tier aggregates, not be
    silently dropped because ``status`` is "partial" instead of
    "filled".
    """
    date = "2026-05-11"
    fills = [
        {
            "tier": "T3_MAKER",
            "market_id": "cid-A",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.001,
            "intended_price": 0.30,
            # Maker quote crossed but only 6 of 10 units traded.
            "result": {"status": "partial", "filled_size": 6.0, "avg_fill_price": 0.30},
        },
        {
            "tier": "T3_MAKER",
            "market_id": "cid-A",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.0,
            "intended_price": 0.30,
            "result": {"status": "filled", "filled_size": 10.0, "avg_fill_price": 0.30},
        },
        {
            "tier": "T3_MAKER",
            "market_id": "cid-A",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.0,
            "intended_price": 0.30,
            # Partial with zero filled_size: edge case where status
            # was tagged partial but no size actually traded; must
            # NOT count toward fee/notional aggregates.
            "result": {"status": "partial", "filled_size": 0.0, "avg_fill_price": None},
        },
    ]
    _write_fills(tmp_path / f"{date}.virtual_fills.ndjson", fills)

    payload = _run(["--telemetry-dir", str(tmp_path), "--date", date])

    t3 = payload["by_tier"]["T3_MAKER"]
    assert t3["filled"] == 1
    # Only the partial with size > 0 counts.
    assert t3["partial"] == 1
    assert t3["any_filled"] == 2
    # 6×0.30 + 10×0.30 = 4.8
    assert t3["total_notional_filled"] == 4.8
    # Slippage list should include the partial (0.001) and the
    # complete fill (0.0) — count = 2.
    assert t3["slippage"]["count"] == 2

    overall = payload["overall"]
    assert overall["rows"] == 3
    assert overall["filled"] == 1
    assert overall["partial"] == 1
    assert overall["any_filled"] == 2


def test_top_markets_excludes_pending_and_failed(tmp_path: Path) -> None:
    """P3 regression: ``top_markets_by_fills`` must rank by actual
    fills, not by any-row count. A market the maker quotes 50 times
    without crossing once should NOT outrank a market that filled
    10 times.
    """
    date = "2026-05-12"
    fills: list[dict] = []
    # Market 'noisy' generates 20 pending maker quotes that never cross.
    for _ in range(20):
        fills.append({
            "tier": "T3_MAKER",
            "market_id": "noisy",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.0,
            "intended_price": 0.50,
            "result": {"status": "pending", "filled_size": 0.0, "avg_fill_price": None},
        })
    # Market 'productive' has 5 actual fills (3 full + 2 partial).
    for _ in range(3):
        fills.append({
            "tier": "T3_MAKER",
            "market_id": "productive",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.0,
            "intended_price": 0.30,
            "result": {"status": "filled", "filled_size": 10.0, "avg_fill_price": 0.30},
        })
    for _ in range(2):
        fills.append({
            "tier": "T3_MAKER",
            "market_id": "productive",
            "is_maker": True,
            "fee": 0.0,
            "slippage": 0.001,
            "intended_price": 0.30,
            "result": {"status": "partial", "filled_size": 5.0, "avg_fill_price": 0.30},
        })
    _write_fills(tmp_path / f"{date}.virtual_fills.ndjson", fills)

    payload = _run(["--telemetry-dir", str(tmp_path), "--date", date])
    top = payload["top_markets_by_fills"]
    # 'productive' has 5 fills; 'noisy' has 0 fills and must be
    # dropped from the ranking entirely.
    assert top[0]["market_id"] == "productive"
    assert top[0]["fills"] == 5
    assert top[0]["notional"] == round(3 * 10.0 * 0.30 + 2 * 5.0 * 0.30, 4)
    assert all(row["market_id"] != "noisy" for row in top)
