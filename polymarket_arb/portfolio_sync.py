"""低频账户同步：拉取真实持仓与当日已实现盈亏."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from polymarket_arb.config import ArbConfig
from polymarket_arb.models import PositionSnapshot

LOG = logging.getLogger(__name__)

_DEFAULT_MAX_PAGES = 50


@dataclass
class PortfolioSnapshot:
    positions: list[PositionSnapshot]
    realized_daily_pnl: float
    synced_at: float
    source_address: str


class PortfolioSync:
    """从 Polymarket Data API 拉取账户真实状态."""

    def __init__(self, config: ArbConfig):
        self._config = config
        self._host = config.data_api_host.rstrip("/")
        self._user = config.portfolio_sync_user_address or config.funder_address
        self._timeout = float(config.portfolio_sync_timeout_sec)

    @property
    def source_address(self) -> str:
        return self._user

    def refresh(self, *, now_ts: float | None = None) -> PortfolioSnapshot:
        now_ts = float(now_ts) if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
        positions = self._fetch_positions()
        realized_daily_pnl = self._fetch_realized_daily_pnl(now_ts)
        return PortfolioSnapshot(
            positions=positions,
            realized_daily_pnl=realized_daily_pnl,
            synced_at=now_ts,
            source_address=self._user,
        )

    def _fetch_positions(self) -> list[PositionSnapshot]:
        rows = self._paginate(
            endpoint="/positions",
            base_params={
                "user": self._user,
                "sizeThreshold": 0,
            },
            limit=500,
        )
        positions = [_parse_position_row(row) for row in rows]
        return [position for position in positions if position.size > 0]

    def _fetch_realized_daily_pnl(self, now_ts: float) -> float:
        day_start_ts = _utc_day_start(now_ts)
        realized = 0.0
        offset = 0
        limit = 200
        # Bounded pagination: a runaway maker session could otherwise fill
        # `/closed-positions` with thousands of rows and stall the main loop.
        # 50 × 200 = 10 000 closed positions per day is well above any realistic
        # bot cadence; if we hit it we WARN and surface incomplete daily PnL.
        for page_index in range(_DEFAULT_MAX_PAGES):
            rows = self._request_rows(
                "/closed-positions",
                {
                    "user": self._user,
                    "limit": limit,
                    "offset": offset,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                },
            )
            if not rows:
                return realized

            reached_older_rows = False
            for row in rows:
                ts = _coerce_timestamp(row.get("timestamp"))
                if ts is None:
                    continue
                if ts < day_start_ts:
                    reached_older_rows = True
                    continue
                realized += _coerce_float(row.get("realizedPnl"))

            if reached_older_rows or len(rows) < limit:
                return realized
            offset += limit
        else:
            LOG.warning(
                "Portfolio sync 已达分页上限 %d × %d，daily PnL 可能不完整",
                _DEFAULT_MAX_PAGES,
                limit,
            )
        return realized

    def _paginate(self, *, endpoint: str, base_params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        for _ in range(_DEFAULT_MAX_PAGES):
            params = dict(base_params)
            params["limit"] = limit
            params["offset"] = offset
            rows = self._request_rows(endpoint, params)
            if not rows:
                return results
            results.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < limit:
                return results
            offset += limit
        LOG.warning(
            "Portfolio sync %s 已达分页上限 %d × %d，结果可能不完整",
            endpoint,
            _DEFAULT_MAX_PAGES,
            limit,
        )
        return results

    def _request_rows(self, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self._request_json(endpoint, params)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
        return []

    def _request_json(self, endpoint: str, params: dict[str, Any]) -> Any:
        url = f"{self._host}{endpoint}"
        resp = requests.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        payload = resp.json()
        LOG.debug("Portfolio sync fetched %s params=%s", endpoint, params)
        return payload


def _parse_position_row(row: dict[str, Any]) -> PositionSnapshot:
    size = _coerce_float(row.get("size"))
    avg_price = _coerce_float(row.get("avgPrice"))
    current_value = _coerce_float(row.get("currentValue"))
    unrealized_pnl = _coerce_float(row.get("cashPnl"))

    return PositionSnapshot(
        token_id=str(row.get("asset") or row.get("tokenId") or ""),
        condition_id=str(row.get("conditionId") or row.get("condition_id") or ""),
        outcome=str(row.get("outcome") or row.get("title") or ""),
        size=size,
        avg_price=avg_price,
        current_value=current_value,
        unrealized_pnl=unrealized_pnl,
    )


def _coerce_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return 0.0
        return parsed
    except (TypeError, ValueError):
        return 0.0


def _coerce_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        return raw
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _coerce_timestamp(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _utc_day_start(now_ts: float) -> float:
    now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
