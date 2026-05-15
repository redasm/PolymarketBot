"""Tests for `polymarket_arb.main_helpers.dashboard_serializers`.

These functions used to live inline in `main_loop.py` and were untested
in isolation. Moving them out unlocks (this) regression suite that pins
their wire format — the dashboard FastAPI layer + AI feedback loop both
consume these dicts, so silent shape changes break downstream consumers.
"""

from __future__ import annotations

from types import SimpleNamespace

from polymarket_arb.main_helpers.dashboard_serializers import (
    build_dashboard_trade_rows,
    estimate_ai_trade_outcome,
    has_simulated_trades,
    lookup_market_snapshot,
    reported_execution_success,
    resolve_market_yes_price,
    serialize_opportunity_event,
    serialize_recent_trade,
    serialize_strategy_signal,
    serialize_trade_execution,
    summarize_market_catalog,
)
from polymarket_arb.models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    MarketInfo,
    OrderSide,
    TokenInfo,
    TradeRecord,
    TradeStatus,
)
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier


def _make_opp(
    *,
    arb_type: ArbType = ArbType.BINARY,
    net_edge: float = 0.05,
    total_cost: float = 0.95,
) -> ArbOpportunity:
    legs = [
        ArbLeg(
            token_id="tk-yes",
            condition_id="c1",
            outcome="Yes",
            side=OrderSide.BUY,
            price=0.45,
            size=10.0,
            available_size=20.0,
        ),
        ArbLeg(
            token_id="tk-no",
            condition_id="c1",
            outcome="No",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            available_size=20.0,
        ),
    ]
    return ArbOpportunity(
        arb_type=arb_type,
        event_id="ev-1",
        event_title="Test event",
        markets=[],
        total_cost=total_cost,
        guaranteed_payout=1.0,
        gross_edge=0.05,
        net_edge=net_edge,
        edge_pct=5.0,
        legs=legs,
        max_executable_size=10.0,
        confidence=0.9,
    )


def _make_trade(
    *,
    status: TradeStatus = TradeStatus.FILLED,
    simulated: bool = False,
    fill_size: float | None = 10.0,
    economic_cost: float | None = 0.45,
    side: OrderSide = OrderSide.BUY,
    size: float = 10.0,
    price: float = 0.45,
) -> TradeRecord:
    return TradeRecord(
        trade_id="t-1",
        arb_id="a-1",
        token_id="tk-yes",
        condition_id="c1",
        side=side,
        price=price,
        size=size,
        status=status,
        fill_price=price,
        fill_size=fill_size,
        economic_cost=economic_cost,
        simulated=simulated,
    )


def _make_market(
    *,
    cid: str = "c1",
    yes_price: float = 0.45,
    outcome_prices: list[float] | None = None,
) -> MarketInfo:
    return MarketInfo(
        condition_id=cid,
        question="Will Yes happen?",
        slug="will-yes",
        tokens=[
            TokenInfo(token_id="tk-yes", outcome="Yes", price=yes_price),
            TokenInfo(token_id="tk-no", outcome="No", price=1 - yes_price),
        ],
        volume_24h=1000.0,
        liquidity=500.0,
        outcome_prices=outcome_prices or [],
    )


def test_has_simulated_trades_detects_any_simulated():
    assert has_simulated_trades([_make_trade(simulated=True)])
    assert not has_simulated_trades([_make_trade(simulated=False)])
    assert not has_simulated_trades([])


def test_reported_execution_success_promotes_simulated_full_fill():
    sim_filled = [_make_trade(simulated=True), _make_trade(simulated=True)]
    assert reported_execution_success(live_execution_success=False, trades=sim_filled)


def test_reported_execution_success_simulated_partial_is_failure():
    sim_partial = [
        _make_trade(simulated=True, status=TradeStatus.FILLED),
        _make_trade(simulated=True, status=TradeStatus.PENDING),
    ]
    assert not reported_execution_success(live_execution_success=False, trades=sim_partial)


def test_reported_execution_success_live_passes_through():
    live = [_make_trade(simulated=False)]
    assert reported_execution_success(live_execution_success=True, trades=live)
    assert not reported_execution_success(live_execution_success=False, trades=live)


def test_estimate_ai_trade_outcome_success_uses_min_filled_size():
    opp = _make_opp(net_edge=0.10)
    trades = [
        _make_trade(fill_size=8.0, status=TradeStatus.FILLED),
        _make_trade(fill_size=12.0, status=TradeStatus.FILLED),
    ]
    pnl = estimate_ai_trade_outcome(opp, trades, arb_success=True, adj_size=10.0)
    assert pnl == 0.10 * 8.0


def test_estimate_ai_trade_outcome_failure_with_partial_fills_returns_negative_realized():
    opp = _make_opp(net_edge=0.10, total_cost=0.95)
    trades = [_make_trade(fill_size=4.0, economic_cost=0.45, status=TradeStatus.PARTIAL)]
    pnl = estimate_ai_trade_outcome(opp, trades, arb_success=False, adj_size=10.0)
    assert pnl == -0.45 * 4.0


def test_estimate_ai_trade_outcome_failure_no_fill_falls_back_to_total_cost():
    opp = _make_opp(net_edge=0.10, total_cost=0.95)
    trades = [_make_trade(fill_size=0.0, status=TradeStatus.FAILED)]
    pnl = estimate_ai_trade_outcome(opp, trades, arb_success=False, adj_size=10.0)
    assert pnl == -0.95 * 10.0


def test_serialize_opportunity_event_contains_legs_and_metadata():
    opp = _make_opp()
    payload = serialize_opportunity_event(opp, stage="detected")
    assert payload["stage"] == "detected"
    assert payload["arb_type"] == "binary"
    assert payload["event_id"] == "ev-1"
    assert payload["net_edge"] == 0.05
    assert len(payload["legs"]) == 2
    assert payload["legs"][0]["side"] == "BUY"


def test_serialize_trade_execution_combines_simulated_promotion():
    opp = _make_opp()
    trades = [_make_trade(simulated=True)]
    payload = serialize_trade_execution(opp, trades, arb_success=False, adj_size=10.0)
    assert payload["arb_success"] is True  # simulated full-fill promoted
    assert payload["live_execution_success"] is False
    assert payload["simulated"] is True
    assert len(payload["trades"]) == 1
    assert payload["trades"][0]["status"] == "filled"


def test_build_dashboard_trade_rows_uses_min_size_for_expected_profit():
    opp = _make_opp(net_edge=0.10)
    trades = [
        _make_trade(size=4.0, fill_size=4.0),
        _make_trade(size=10.0, fill_size=10.0),
    ]
    rows = build_dashboard_trade_rows(
        opp=opp,
        trades=trades,
        live_execution_success=True,
        dashboard_execution_success=True,
    )
    assert len(rows) == 2
    profits = {row["expected_profit"] for row in rows}
    # min(size=4) * net_edge — same value across all per-leg rows
    assert profits == {0.10 * 4.0}
    assert rows[0]["mode"] == "live"


def test_build_dashboard_trade_rows_marks_simulated_mode():
    opp = _make_opp()
    trades = [_make_trade(simulated=True)]
    rows = build_dashboard_trade_rows(
        opp=opp,
        trades=trades,
        live_execution_success=False,
        dashboard_execution_success=True,
    )
    assert rows[0]["mode"] == "simulated"


def test_serialize_strategy_signal_round_trip():
    signal = StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type="t2_buy",
        market_id="cX",
        description="bid below fair",
        expected_edge=0.04,
        confidence=0.65,
        recommended_size_usdc=50.0,
        urgency=0.3,
        payload={"hint": "ok"},
        signal_id="sig-test",
    )
    payload = serialize_strategy_signal(signal, submitted=True, research_overlay={"r": 1})
    assert payload["signal_id"] == "sig-test"
    assert payload["tier"] == "STATISTICAL_ARB"
    assert payload["submitted"] is True
    assert payload["research_overlay"] == {"r": 1}
    assert payload["payload"] == {"hint": "ok"}


def test_lookup_market_snapshot_exact_then_prefix_then_empty():
    catalog = {"abcdef123": {"q": "Will X?"}}
    assert lookup_market_snapshot("abcdef123", catalog) == {"q": "Will X?"}
    # prefix lookup (caller passed truncated id)
    assert lookup_market_snapshot("abcdef", catalog) == {"q": "Will X?"}
    # both ways
    assert lookup_market_snapshot("abcdef123extra", catalog) == {"q": "Will X?"}
    assert lookup_market_snapshot("zzz", catalog) == {}
    assert lookup_market_snapshot("", catalog) == {}


def test_lookup_market_snapshot_returns_shallow_copy():
    catalog = {"id1": {"nested": [1, 2]}}
    out = lookup_market_snapshot("id1", catalog)
    out["new"] = True
    assert "new" not in catalog["id1"]


def test_resolve_market_yes_price_prefers_yes_token_then_outcome_prices_then_first_valid():
    yes_first = _make_market(yes_price=0.45)
    assert resolve_market_yes_price(yes_first) == 0.45

    invalid_token = MarketInfo(
        condition_id="c1",
        question="?",
        slug="s",
        tokens=[
            TokenInfo(token_id="tk-yes", outcome="Yes", price=0.0),
            TokenInfo(token_id="tk-no", outcome="No", price=0.0),
        ],
        outcome_prices=[0.55, 0.45],
    )
    # YES token's price is 0 -> falls through to outcome_prices[0]
    assert resolve_market_yes_price(invalid_token) == 0.55

    only_no = MarketInfo(
        condition_id="c1",
        question="?",
        slug="s",
        tokens=[TokenInfo(token_id="tk-no", outcome="No", price=0.30)],
    )
    # No YES token, no outcome_prices -> fall back to any 0<p<1 token price.
    assert resolve_market_yes_price(only_no) == 0.30


def test_resolve_market_yes_price_returns_none_when_nothing_valid():
    market = MarketInfo(
        condition_id="c1",
        question="?",
        slug="s",
        tokens=[TokenInfo(token_id="tk", outcome="X", price=0.0)],
    )
    assert resolve_market_yes_price(market) is None


def test_summarize_market_catalog_respects_limit_and_includes_yes_price():
    markets = [_make_market(cid=f"c{i}", yes_price=0.4 + i * 0.01) for i in range(5)]
    cat = summarize_market_catalog(markets, limit=3)
    assert list(cat.keys()) == ["c0", "c1", "c2"]
    assert cat["c0"]["yes_price"] == 0.4
    assert "updated_at" in cat["c0"]


def test_serialize_recent_trade_supports_dataclass_and_dict():
    trade = _make_trade()
    payload = serialize_recent_trade(trade)
    assert payload["trade_id"] == "t-1"
    assert payload["status"] == "filled"

    raw_dict = {"trade_id": "x", "status": "y"}
    assert serialize_recent_trade(raw_dict) == raw_dict


def test_build_ws_status_phase_logic():
    """`build_ws_status` derives `phase` from store + ws_targets so a stale
    `phase_hint` can never override a live connection signal."""
    from polymarket_arb.main_helpers.dashboard_serializers import build_ws_status

    class _StubStore:
        def __init__(self, **fields):
            self._fields = fields

        def snapshot(self):
            return self._fields

    config_on = SimpleNamespace(ws_enabled=True)
    config_off = SimpleNamespace(ws_enabled=False)

    # connected
    s_conn = _StubStore(connected=True, market_id="m", ts_ms=1)
    out = build_ws_status(
        config=config_on, enhanced_store=s_conn, ws_target_ids=["t"], phase_hint="idle"
    )
    assert out["phase"] == "connected"
    assert out["connected"] is True

    # disabled by config
    s_disc = _StubStore(connected=False, market_id=None, ts_ms=0)
    out = build_ws_status(
        config=config_off, enhanced_store=s_disc, ws_target_ids=[], phase_hint="idle"
    )
    assert out["phase"] == "disabled"

    # subscribed but not yet connected
    out = build_ws_status(
        config=config_on, enhanced_store=s_disc, ws_target_ids=["t"], phase_hint="idle"
    )
    assert out["phase"] == "initializing"

    # falls back to phase_hint when nothing else known
    out = build_ws_status(
        config=config_on, enhanced_store=s_disc, ws_target_ids=[], phase_hint="warmup"
    )
    assert out["phase"] == "warmup"
