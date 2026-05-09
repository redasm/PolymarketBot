"""监控仪表盘 API：FastAPI 后端，暴露机器人运行状态的 REST 端点.

所有端点只读，不提供任何写操作（不能通过 dashboard 下单/改配置）。
绑定 127.0.0.1，仅本机可访问。
"""

from __future__ import annotations

import logging
import threading
import time
import uvicorn
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

LOG = logging.getLogger(__name__)

app = FastAPI(title="Polymarket Arb Dashboard", docs_url=None, redoc_url=None)

_STATE: Optional[DashboardState] = None


class DashboardState:
    """仪表盘共享状态：由主循环写入，由 API 端点读取."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Public read-only field grouped from `_lock`. API endpoints can grab
        # it without reaching into the private attribute, while writers in
        # this module keep a single lock identity.
        self.lock = self._lock
        self.bot_start_ts: float = time.time()
        self.cycle_count: int = 0
        self.is_running: bool = True
        self.is_dry_run: bool = True
        self.scan_interval: float = 5.0

        self.arbs_found: int = 0
        self.arbs_executed: int = 0
        self.markets_scanned: int = 0

        self.risk_state: dict = {}
        self.strategy_status: dict = {}
        self.execution_summary: dict = {}

        self.recent_opportunities: list[dict] = []
        self.recent_trades: list[dict] = []
        self.recent_errors: list[dict] = []

        self.pnl_history: list[dict] = []
        self.current_positions: list[dict] = []

        self.orderbook_stats: dict[str, dict] = {}

        self.volatility: dict = {}
        self.edge_decision: Optional[dict] = None
        self.book_summary: dict = {}
        self.ws_status: dict = {}
        self.ai_status: dict = {}
        self.ai_decisions: list[dict] = []
        self.market_catalog: dict[str, dict] = {}
        self.universe_status: dict = {}
        self.research_signal_status: dict = {}
        self.backtest_last_report: dict = {}

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def append_opportunity(self, opp: dict) -> None:
        with self._lock:
            self.recent_opportunities.append(opp)
            if len(self.recent_opportunities) > 100:
                self.recent_opportunities = self.recent_opportunities[-100:]

    def append_trade(self, trade: dict) -> None:
        with self._lock:
            self.recent_trades.append(trade)
            if len(self.recent_trades) > 200:
                self.recent_trades = self.recent_trades[-200:]

    def append_error(self, error: dict) -> None:
        with self._lock:
            self.recent_errors.append(error)
            if len(self.recent_errors) > 50:
                self.recent_errors = self.recent_errors[-50:]

    def append_pnl_point(self, point: dict) -> None:
        with self._lock:
            self.pnl_history.append(point)
            if len(self.pnl_history) > 2880:
                self.pnl_history = self.pnl_history[-2880:]

    def append_ai_decision(self, decision: dict) -> None:
        with self._lock:
            self.ai_decisions.append(decision)
            if len(self.ai_decisions) > 100:
                self.ai_decisions = self.ai_decisions[-100:]

    def snapshot(self) -> dict:
        with self._lock:
            uptime = time.time() - self.bot_start_ts
            return {
                "uptime_sec": uptime,
                "uptime_human": _format_duration(uptime),
                "cycle_count": self.cycle_count,
                "is_running": self.is_running,
                "is_dry_run": self.is_dry_run,
                "scan_interval": self.scan_interval,
                "arbs_found": self.arbs_found,
                "arbs_executed": self.arbs_executed,
                "markets_scanned": self.markets_scanned,
                "risk_state": dict(self.risk_state),
                "strategy_status": dict(self.strategy_status),
                "execution_summary": dict(self.execution_summary),
                "recent_opportunities": list(self.recent_opportunities[-20:]),
                "recent_trades": list(self.recent_trades[-20:]),
                "recent_errors": list(self.recent_errors[-10:]),
                "pnl_history": list(self.pnl_history[-200:]),
                "current_positions": list(self.current_positions),
                "volatility": dict(self.volatility) if self.volatility else None,
                "edge_decision": dict(self.edge_decision) if self.edge_decision else None,
                "book_summary": dict(self.book_summary) if self.book_summary else None,
                "ws_status": dict(self.ws_status) if self.ws_status else None,
                "ai_status": dict(self.ai_status) if self.ai_status else None,
                "universe_status": dict(self.universe_status) if self.universe_status else None,
                "research_signal_status": dict(self.research_signal_status) if self.research_signal_status else None,
                "backtest_last_report": dict(self.backtest_last_report) if self.backtest_last_report else None,
                "ts": time.time(),
            }


def set_state(state: DashboardState) -> None:
    global _STATE
    _STATE = state


def _get_state() -> DashboardState:
    if _STATE is None:
        return DashboardState()
    return _STATE


# --- API 端点 ---

@app.get("/")
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard HTML not found</h1>", status_code=404)


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(_get_state().snapshot())


@app.get("/api/opportunities")
async def api_opportunities() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = list(state.recent_opportunities)
    return JSONResponse({"opportunities": data, "total": len(data)})


@app.get("/api/trades")
async def api_trades() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = list(state.recent_trades)
    return JSONResponse({"trades": data, "total": len(data)})


@app.get("/api/pnl")
async def api_pnl() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = list(state.pnl_history)
    return JSONResponse({"pnl_history": data, "total": len(data)})


@app.get("/api/risk")
async def api_risk() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.risk_state)
    return JSONResponse(data)


@app.get("/api/strategies")
async def api_strategies() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.strategy_status)
    return JSONResponse(data)


@app.get("/api/positions")
async def api_positions() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = list(state.current_positions)
    return JSONResponse({"positions": data})


@app.get("/api/volatility")
async def api_volatility() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.volatility) if state.volatility else {}
    return JSONResponse(data)


@app.get("/api/edge")
async def api_edge() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.edge_decision) if state.edge_decision else {}
    return JSONResponse(data)


@app.get("/api/book")
async def api_book() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.book_summary) if state.book_summary else {}
    return JSONResponse(data)


@app.get("/api/ws")
async def api_ws() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.ws_status) if state.ws_status else {}
    return JSONResponse(data)


@app.get("/api/ai")
async def api_ai() -> JSONResponse:
    state = _get_state()
    with state.lock:
        market_catalog = dict(state.market_catalog)
        return JSONResponse({
            "status": dict(state.ai_status) if state.ai_status else None,
            "decisions": [
                _enrich_ai_decision(decision, market_catalog)
                for decision in state.ai_decisions[-30:]
            ],
        })


@app.get("/api/research-signals")
async def api_research_signals() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.research_signal_status) if state.research_signal_status else {}
    return JSONResponse(data)


@app.get("/api/backtest-report")
async def api_backtest_report() -> JSONResponse:
    state = _get_state()
    with state.lock:
        data = dict(state.backtest_last_report) if state.backtest_last_report else {}
    return JSONResponse(data)


def _format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def start_dashboard_server(
    state: DashboardState,
    host: str = "127.0.0.1",
    port: int = 8077,
) -> threading.Thread:
    """在后台线程启动 dashboard HTTP 服务器."""
    set_state(state)

    def _run() -> None:
        LOG.info("Dashboard 启动: http://%s:%d", host, port)
        uvicorn.run(app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_run, daemon=True, name="dashboard")
    thread.start()
    return thread


_AI_VALIDATION_MIN_AGE_SEC = 300.0
_AI_VALIDATION_MOVE_THRESHOLD = 0.01


def _enrich_ai_decision(decision: dict, market_catalog: dict[str, dict]) -> dict:
    now = time.time()
    enriched = dict(decision)
    market_info = _lookup_market_info(str(decision.get("market_id", "")), market_catalog)
    if market_info:
        enriched.setdefault("market_question", market_info.get("question", ""))
        enriched["current_price"] = market_info.get("yes_price")
        enriched.setdefault("volume_24h", market_info.get("volume_24h"))
        enriched.setdefault("liquidity", market_info.get("liquidity"))

    entry_price = _safe_float(enriched.get("decision_price"))
    current_price = _safe_float(enriched.get("current_price"))
    timestamp = _safe_float(enriched.get("timestamp")) or now
    age_sec = max(0.0, now - timestamp)
    enriched["age_sec"] = round(age_sec, 1)

    if entry_price is not None and current_price is not None:
        price_delta = current_price - entry_price
        enriched["price_delta"] = round(price_delta, 4)
        enriched["price_delta_pct_pts"] = round(price_delta * 100.0, 2)
    else:
        price_delta = None

    enriched["validation"] = _build_ai_validation(
        action=str(enriched.get("action", "")),
        age_sec=age_sec,
        entry_price=entry_price,
        current_price=current_price,
        price_delta=price_delta,
    )
    return enriched


def _build_ai_validation(
    *,
    action: str,
    age_sec: float,
    entry_price: float | None,
    current_price: float | None,
    price_delta: float | None,
) -> dict[str, Any]:
    if action not in {"BUY_YES", "BUY_NO", "SELL_YES", "SELL_NO"}:
        return {"status": "not_applicable", "label": "不评估", "detail": "仅评估方向性决策"}
    if entry_price is None or current_price is None or price_delta is None:
        return {"status": "pending", "label": "待验证", "detail": "缺少价格数据"}
    if age_sec < _AI_VALIDATION_MIN_AGE_SEC:
        remaining = int(_AI_VALIDATION_MIN_AGE_SEC - age_sec)
        return {"status": "pending", "label": "待验证", "detail": f"等待 {remaining}s"}
    if abs(price_delta) < _AI_VALIDATION_MOVE_THRESHOLD:
        return {"status": "neutral", "label": "未显著验证", "detail": f"变化 {price_delta * 100.0:+.2f}pp"}

    expected_up = action in {"BUY_YES", "SELL_NO"}
    is_correct = price_delta > 0 if expected_up else price_delta < 0
    return {
        "status": "correct" if is_correct else "wrong",
        "label": "正确" if is_correct else "错误",
        "detail": f"变化 {price_delta * 100.0:+.2f}pp",
    }


def _lookup_market_info(market_id: str, market_catalog: dict[str, dict]) -> dict[str, Any]:
    if not market_id:
        return {}
    if market_id in market_catalog:
        return dict(market_catalog[market_id])
    for key, value in market_catalog.items():
        if key.startswith(market_id) or market_id.startswith(key):
            return dict(value)
    return {}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
