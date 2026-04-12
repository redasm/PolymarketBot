"""Data janitor tests."""

from __future__ import annotations

import time
from pathlib import Path

from polymarket_arb.data_janitor import DataJanitor


def test_data_janitor_deletes_old_tick_files(tmp_path: Path):
    ticks_dir = tmp_path / "ticks"
    ticks_dir.mkdir()
    old_file = ticks_dir / "old.ndjson"
    old_file.write_text("old", encoding="utf-8")
    old_ts = time.time() - (10 * 86400)
    old_file.touch()
    import os
    os.utime(old_file, (old_ts, old_ts))

    janitor = DataJanitor(
        enabled=True,
        interval_sec=1,
        tick_dir=str(ticks_dir),
        tick_retention_days=7,
        tick_max_gb=1.0,
        telemetry_dir=str(tmp_path / "telemetry"),
        telemetry_retention_days=14,
        telemetry_max_gb=1.0,
        research_cache_dir=str(tmp_path / "research"),
        research_cache_retention_days=14,
        research_cache_max_gb=1.0,
        research_knowledge_dir=str(tmp_path / "research" / "knowledge"),
        backtest_data_dir=str(tmp_path / "backtest"),
        backtest_retention_days=30,
        backtest_max_gb=1.0,
    )

    results = janitor.run_once()

    assert old_file.exists() is False
    assert any(item.scope == "ticks" and item.deleted_files == 1 for item in results)


def test_data_janitor_keeps_research_knowledge_files(tmp_path: Path):
    research_dir = tmp_path / "research"
    knowledge_dir = research_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    knowledge_file = knowledge_dir / "example.jsonl"
    knowledge_file.write_text("{}", encoding="utf-8")
    cache_file = research_dir / "cache.json"
    cache_file.write_text("{}", encoding="utf-8")
    old_ts = time.time() - (20 * 86400)
    import os
    os.utime(knowledge_file, (old_ts, old_ts))
    os.utime(cache_file, (old_ts, old_ts))

    janitor = DataJanitor(
        enabled=True,
        interval_sec=1,
        tick_dir=str(tmp_path / "ticks"),
        tick_retention_days=7,
        tick_max_gb=1.0,
        telemetry_dir=str(tmp_path / "telemetry"),
        telemetry_retention_days=14,
        telemetry_max_gb=1.0,
        research_cache_dir=str(research_dir),
        research_cache_retention_days=7,
        research_cache_max_gb=1.0,
        research_knowledge_dir=str(knowledge_dir),
        backtest_data_dir=str(tmp_path / "backtest"),
        backtest_retention_days=30,
        backtest_max_gb=1.0,
    )

    janitor.run_once()

    assert knowledge_file.exists() is True
    assert cache_file.exists() is False
