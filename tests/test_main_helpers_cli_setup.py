"""Tests for `polymarket_arb.main_helpers.cli_setup`.

These functions used to live inline in `main_loop.py` and were not
unit-tested. Pinning their behaviour here so the upcoming further
decomposition of `main()` cannot silently drop a parsing rule, log line,
or graceful-degradation branch.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from polymarket_arb.main_helpers.cli_setup import (
    build_run_instance_id,
    create_research_signal_service,
    load_last_backtest_report,
    log_startup_summary,
    parse_extra_rss_feeds,
    parse_http_json_sources,
    round_timing,
)
from tests.conftest import make_test_config


def test_build_run_instance_id_format():
    rid = build_run_instance_id(now_ts=0.0)  # 1970-01-01T00:00:00Z
    assert rid == f"run-{os.getpid()}-19700101T000000Z"


def test_build_run_instance_id_uses_now_when_omitted():
    rid_a = build_run_instance_id()
    time.sleep(0.001)
    rid_b = build_run_instance_id()
    assert rid_a.startswith(f"run-{os.getpid()}-")
    assert rid_b.startswith(f"run-{os.getpid()}-")


def test_round_timing_clamps_negative_and_rounds():
    assert round_timing(-3.0) == 0.0
    assert round_timing(0.123456789) == 0.1235
    assert round_timing(None) == 0.0  # type: ignore[arg-type]
    assert round_timing(0.0) == 0.0


def test_parse_extra_rss_feeds_handles_named_and_unnamed_entries():
    raw = "feed_a=https://a.example/rss, https://b.example/rss ,=https://c.example/rss"
    out = parse_extra_rss_feeds(raw)
    assert out == [
        ("feed_a", "https://a.example/rss"),
        ("rss_feed_2", "https://b.example/rss"),
        ("rss_feed_3", "https://c.example/rss"),  # blank name -> auto-name
    ]


def test_parse_extra_rss_feeds_drops_blank_templates_and_empty_items():
    raw = " , name_only=  , real=https://x.example/rss"
    out = parse_extra_rss_feeds(raw)
    assert out == [("real", "https://x.example/rss")]


def test_parse_http_json_sources_returns_dict_list():
    raw = '[{"url": "https://x"}, {"url": "https://y"}]'
    out = parse_http_json_sources(raw)
    assert out == [{"url": "https://x"}, {"url": "https://y"}]


def test_parse_http_json_sources_drops_non_dict_entries():
    raw = '[{"url": "ok"}, "junk", 42, {"url": "ok2"}]'
    out = parse_http_json_sources(raw)
    assert out == [{"url": "ok"}, {"url": "ok2"}]


def test_parse_http_json_sources_empty_input_returns_empty_list():
    assert parse_http_json_sources("") == []
    assert parse_http_json_sources("  ") == []


def test_parse_http_json_sources_logs_on_invalid_json(caplog):
    with caplog.at_level(logging.ERROR, logger="main_loop"):
        out = parse_http_json_sources("not json")
    assert out == []
    assert any("解析失败" in record.getMessage() for record in caplog.records)


def test_parse_http_json_sources_logs_when_not_a_list(caplog):
    with caplog.at_level(logging.ERROR, logger="main_loop"):
        out = parse_http_json_sources('{"a": 1}')
    assert out == []
    assert any("必须是 JSON list" in record.getMessage() for record in caplog.records)


def test_create_research_signal_service_disabled_returns_none():
    cfg = make_test_config(research_signal_enabled=False)
    assert create_research_signal_service(cfg) is None


def test_create_research_signal_service_handles_missing_module(monkeypatch, caplog):
    cfg = make_test_config(research_signal_enabled=True)

    def _raise(name, package=None):
        raise ImportError(f"no module {name}")

    monkeypatch.setattr(
        "polymarket_arb.main_helpers.cli_setup.importlib.import_module",
        _raise,
    )
    with caplog.at_level(logging.ERROR, logger="main_loop"):
        out = create_research_signal_service(cfg)
    assert out is None
    assert any("模块导入失败" in record.getMessage() for record in caplog.records)


def test_load_last_backtest_report_missing_dir_returns_disabled():
    out = load_last_backtest_report("/no/such/dir/xyz")
    assert out["enabled"] is False
    assert out["reports_dir"] == "/no/such/dir/xyz"


def test_load_last_backtest_report_no_files_returns_disabled(tmp_path: Path):
    out = load_last_backtest_report(str(tmp_path))
    assert out["enabled"] is False


def test_load_last_backtest_report_picks_most_recent_and_attaches_trades(tmp_path: Path):
    older = tmp_path / "alpha_report.json"
    older.write_text(json.dumps({"name": "alpha"}), encoding="utf-8")
    older_trades = tmp_path / "alpha_trades.jsonl"
    older_trades.write_text('{"t": 1}\n{"t": 2}\n', encoding="utf-8")

    # Force file mtime ordering since CI clocks can have low resolution.
    old_ts = time.time() - 60
    os.utime(older, (old_ts, old_ts))

    newer = tmp_path / "beta_report.json"
    newer.write_text(json.dumps({"name": "beta"}), encoding="utf-8")
    newer_trades = tmp_path / "beta_trades.jsonl"
    newer_trades.write_text('{"t": 9}\n', encoding="utf-8")

    out = load_last_backtest_report(str(tmp_path))
    assert out["enabled"] is True
    assert out["name"] == "beta"
    assert out["path"].endswith("beta_report.json")
    assert out["trades_path"].endswith("beta_trades.jsonl")
    assert out["recent_trade_rows"] == [{"t": 9}]


def test_load_last_backtest_report_handles_unparseable_json(tmp_path: Path):
    bad = tmp_path / "x_report.json"
    bad.write_text("not json", encoding="utf-8")
    out = load_last_backtest_report(str(tmp_path))
    assert out == {"enabled": True, "reports_dir": str(tmp_path), "error": "report_parse_failed"}


def test_log_startup_summary_emits_banner(caplog):
    cfg = make_test_config()
    with caplog.at_level(logging.INFO, logger="main_loop"):
        log_startup_summary(cfg, run_id="run-test")
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Polymarket 套利机器人启动" in messages
    assert "run_id=run-test" in messages
    assert "DRY RUN" in messages or "LIVE" in messages
