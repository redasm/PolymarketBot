"""Telemetry enrichers for strategy analysis and post-run joins."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import OrderBookSnapshot


_SENSITIVE_TOKENS = (
    "FEISHU_APP_ID",
    "FEISHU_OPEN_ID",
    "KEY",
    "SECRET",
    "PASSPHRASE",
    "PASS_PHRASE",
    "PRIVATE",
    "TOKEN",
    "APP_SECRET",
)


def new_execution_id() -> str:
    return f"exe-{uuid.uuid4().hex[:12]}"


def ensure_signal_id(signal: Any) -> str:
    signal_id = str(getattr(signal, "signal_id", "") or "")
    if not signal_id:
        signal_id = f"sig-{uuid.uuid4().hex[:12]}"
        try:
            signal.signal_id = signal_id
        except Exception:
            pass
    return signal_id


def compact_book_snapshot(snap: OrderBookSnapshot | Any | None) -> dict[str, Any]:
    if snap is None:
        return {
            "available": False,
        }
    now = time.time()
    ts = float(getattr(snap, "timestamp", 0.0) or 0.0)
    best_bid = getattr(snap, "best_bid", None)
    best_ask = getattr(snap, "best_ask", None)
    mid = getattr(snap, "mid", None)
    spread = getattr(snap, "spread", None)
    if spread is None and best_bid is not None and best_ask is not None:
        spread = float(best_ask) - float(best_bid)
    spread_bps = None
    if mid is not None and spread is not None and float(mid) > 0:
        spread_bps = (float(spread) / float(mid)) * 10_000.0
    bids = list(getattr(snap, "bids", []) or [])
    asks = list(getattr(snap, "asks", []) or [])
    return {
        "available": True,
        "token_id": str(getattr(snap, "token_id", "") or ""),
        "best_bid": float(best_bid) if best_bid is not None else None,
        "best_ask": float(best_ask) if best_ask is not None else None,
        "mid": float(mid) if mid is not None else None,
        "spread": float(spread) if spread is not None else None,
        "spread_bps": round(float(spread_bps), 4) if spread_bps is not None else None,
        "bid_depth_5": round(sum(float(getattr(level, "size", 0.0) or 0.0) for level in bids[:5]), 6),
        "ask_depth_5": round(sum(float(getattr(level, "size", 0.0) or 0.0) for level in asks[:5]), 6),
        "bids_top3": [[float(level.price), float(level.size)] for level in bids[:3]],
        "asks_top3": [[float(level.price), float(level.size)] for level in asks[:3]],
        "tick_size": float(getattr(snap, "tick_size", 0.01) or 0.01),
        "age_sec": round(max(0.0, now - ts), 4) if ts > 0 else None,
    }


def build_run_config_event(config: ArbConfig, *, run_id: str) -> dict[str, Any]:
    safe_config = {
        key: _json_safe(value)
        for key, value in _config_items(config).items()
        if not _is_sensitive_key(key)
    }
    serialized = json.dumps(safe_config, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "event": "run_config",
        "run_id": run_id,
        "pid": _safe_pid(),
        "mode": "dry_run" if config.dry_run else "live",
        "git_commit": _git_commit(),
        "config_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16],
        "config": safe_config,
    }


def summarize_gate_skips(items: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_market: dict[str, int] = {}
    for item in items:
        reason = str(item.get("reason") or "unknown")
        tier = str(item.get("tier") or "")
        market_id = str(item.get("market_id") or "")
        reasons[reason] = reasons.get(reason, 0) + 1
        if tier:
            by_tier[tier] = by_tier.get(tier, 0) + 1
        if market_id:
            by_market[market_id] = by_market.get(market_id, 0) + 1
    return {
        "total": len(items),
        "reasons": reasons,
        "by_tier": by_tier,
        "top_markets": sorted(by_market.items(), key=lambda kv: kv[1], reverse=True)[:10],
    }


def structured_skip_reason(reason: str, *, default_code: str = "unknown") -> dict[str, Any]:
    """Convert human-readable skip text into stable analysis buckets.

    Existing callers and dashboards still receive the original `reason`
    string. Telemetry rows additionally include this `reason_code` and
    `reason_context` so offline analysis does not need fragile Chinese regexes.
    """
    text = str(reason or "").strip()
    if not text:
        return {"reason_code": default_code, "reason_context": {}}

    event_match = re.search(r"事件\s+([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)秒内已执行过套利", text)
    if event_match:
        return {
            "reason_code": "event_cooldown",
            "reason_context": {
                "event_id": event_match.group(1),
                "cooldown_sec": _number(event_match.group(2)),
            },
        }
    market_match = re.search(r"市场\s+([^\s]+)\s+敞口已达上限", text)
    if market_match:
        return {
            "reason_code": "market_exposure_cap",
            "reason_context": {"market_id_prefix": market_match.group(1)},
        }
    if text == "全局敞口已满":
        return {"reason_code": "total_exposure_cap", "reason_context": {}}
    if text == "风控后可执行数量为 0":
        return {"reason_code": "zero_size_after_risk", "reason_context": {}}
    if text.startswith("insufficient_balance"):
        return {"reason_code": "insufficient_balance", "reason_context": {"detail": text}}
    if text.startswith("交易已暂停"):
        return {"reason_code": "risk_halted", "reason_context": {"detail": text}}
    if text.startswith("持仓数 "):
        return {"reason_code": "open_position_cap", "reason_context": {"detail": text}}
    if text.startswith("总敞口 "):
        return {"reason_code": "total_exposure_cap", "reason_context": {"detail": text}}
    if text.startswith("日亏损 "):
        return {"reason_code": "daily_loss_stop", "reason_context": {"detail": text}}
    if text.startswith("连续失败 "):
        return {"reason_code": "consecutive_failure_halt", "reason_context": {"detail": text}}
    if text.startswith("账户同步连续失败"):
        return {"reason_code": "portfolio_sync_gate", "reason_context": {"detail": text}}
    slug = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    return {"reason_code": slug[:80] or default_code, "reason_context": {"detail": text}}


def normalize_skip_reason_counts(*summaries: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for summary in summaries:
        if not summary:
            continue
        if "reasons" in summary and isinstance(summary["reasons"], dict):
            items = summary["reasons"].items()
        elif "skip_reasons" in summary and isinstance(summary["skip_reasons"], dict):
            items = summary["skip_reasons"].items()
        else:
            items = summary.items()
        for reason, count in items:
            if reason in {"total", "by_tier", "top_markets"}:
                continue
            try:
                numeric = int(count)
            except Exception:
                continue
            if numeric <= 0:
                continue
            code = structured_skip_reason(str(reason)).get("reason_code", "unknown")
            counts[str(code)] = counts.get(str(code), 0) + numeric
    return counts


def _config_items(config: ArbConfig) -> dict[str, Any]:
    if is_dataclass(config):
        return asdict(config)
    return dict(getattr(config, "__dict__", {}) or {})


def _is_sensitive_key(key: str) -> bool:
    upper = key.upper()
    return any(token in upper for token in _SENSITIVE_TOKENS)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _number(value: str) -> int | float:
    numeric = float(value)
    if abs(numeric - round(numeric)) < 1e-9:
        return int(round(numeric))
    return numeric


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _safe_pid() -> int:
    try:
        import os

        return os.getpid()
    except Exception:
        return 0
