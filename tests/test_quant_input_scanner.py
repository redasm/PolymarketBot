from __future__ import annotations

import json

from polymarket_arb.models import EventInfo, MarketInfo, TokenInfo
from polymarket_arb.quant_input_scanner import (
    DataApiWalletTradeClient,
    build_wallet_markouts_from_trade_rows,
    build_wallet_observations_from_trades,
    build_wallet_markouts_from_shadow_rows,
    build_wallet_profiles_from_markout_rows,
    discover_wallets_from_trades,
    generate_logical_constraint_candidates,
    promote_wallet_profiles_from_markout_rows,
    select_logical_constraints_with_llm,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return _FakeResponse({"data": [{"proxyWallet": "0xabc", "conditionId": "m1"}]})


class _FakeLLM:
    async def chat(self, messages, *, temperature=0.1, json_mode=False, tools=None):
        content = json.dumps(
            {
                "rules": [
                    {
                        "subject_market_id": "candidate",
                        "bound_market_id": "party",
                        "relation_type": "subject_lte_bound",
                        "min_violation_bps": 250,
                        "reason": "candidate win implies party win",
                    }
                ]
            }
        )
        return type("Resp", (), {"content": content})()


def _market(condition_id: str, question: str, *, event_id: str = "event") -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question=question,
        slug=condition_id,
        tokens=[
            TokenInfo(token_id=f"{condition_id}-yes", outcome="Yes", price=0.5),
            TokenInfo(token_id=f"{condition_id}-no", outcome="No", price=0.5),
        ],
        active=True,
        closed=False,
        liquidity=1000,
        volume_24h=500,
        event_id=event_id,
    )


def test_generate_logical_constraint_candidates_groups_binary_markets_by_event() -> None:
    event = EventInfo(
        event_id="event",
        slug="e",
        title="Election event",
        markets=[
            _market("candidate", "Will Alice win?"),
            _market("party", "Will Alice party win?"),
        ],
    )

    candidates = generate_logical_constraint_candidates([event], min_liquidity=100)

    assert len(candidates) == 2
    assert candidates[0]["event_id"] == "event"
    assert {candidates[0]["subject_market_id"], candidates[0]["bound_market_id"]} == {"candidate", "party"}


def test_generate_logical_constraint_candidates_caps_and_ranks_large_events() -> None:
    markets = [
        _market(f"m{i}", f"Will outcome {i} happen?")
        for i in range(5)
    ]
    for idx, market in enumerate(markets):
        market.volume_24h = float(idx)
        market.liquidity = float(idx * 10)
    event = EventInfo(event_id="event", slug="e", title="Large event", markets=markets)

    candidates = generate_logical_constraint_candidates(
        [event],
        max_markets_per_event=2,
        max_pairs_per_event=10,
    )

    assert len(candidates) == 2
    assert {
        candidates[0]["subject_market_id"],
        candidates[0]["bound_market_id"],
    } == {"m3", "m4"}


def test_select_logical_constraints_with_llm_returns_parseable_rules() -> None:
    rules = select_logical_constraints_with_llm(
        _FakeLLM(),
        [
            {
                "subject_market_id": "candidate",
                "subject_question": "Will Alice win?",
                "bound_market_id": "party",
                "bound_question": "Will Alice party win?",
            }
        ],
    )

    assert rules == [
        {
            "subject_market_id": "candidate",
            "bound_market_id": "party",
            "relation_type": "subject_lte_bound",
            "min_violation_bps": 250.0,
            "tags": ["llm_selected"],
        }
    ]


def test_data_api_wallet_trade_client_fetches_user_trades() -> None:
    session = _FakeSession()
    client = DataApiWalletTradeClient("https://data-api.polymarket.com", session=session)

    rows = client.fetch_trades("0xabc", limit=25)

    assert rows == [{"proxyWallet": "0xabc", "conditionId": "m1"}]
    assert session.calls[0][0] == "https://data-api.polymarket.com/trades"
    assert session.calls[0][1]["user"] == "0xabc"
    assert session.calls[0][1]["limit"] == 25


def test_data_api_wallet_trade_client_fetches_recent_trades_without_user_filter() -> None:
    session = _FakeSession()
    client = DataApiWalletTradeClient("https://data-api.polymarket.com", session=session)

    rows = client.fetch_recent_trades(limit=50)

    assert rows == [{"proxyWallet": "0xabc", "conditionId": "m1"}]
    assert session.calls[0][0] == "https://data-api.polymarket.com/trades"
    assert "user" not in session.calls[0][1]
    assert session.calls[0][1]["limit"] == 50


def test_discover_wallets_from_trades_ranks_by_trade_count_and_notional() -> None:
    wallets = discover_wallets_from_trades(
        [
            {"proxyWallet": "0xaaa", "price": 0.5, "size": 100},
            {"proxyWallet": "0xaaa", "price": 0.5, "size": 100},
            {"proxyWallet": "0xbbb", "price": 0.9, "size": 10},
            {"proxyWallet": "0xdirty", "price": 1_000_000, "size": 100},
            {"proxyWallet": "0xdirty", "price": 1_000_000, "size": 100},
        ],
        min_trades=2,
        min_notional_usdc=50,
        max_wallets=3,
    )

    assert wallets == ["0xaaa"]


def test_build_wallet_observations_from_trades_maps_side_and_notional() -> None:
    observations = build_wallet_observations_from_trades(
        [
            {
                "proxyWallet": "0xabc",
                "conditionId": "m1",
                "outcome": "Yes",
                "side": "BUY",
                "price": 0.40,
                "size": 50,
                "marketSlug": "macro-event",
            },
            {
                "proxyWallet": "0xabc",
                "conditionId": "m1",
                "outcome": "No",
                "side": "SELL",
                "price": 0.40,
                "size": 50,
                "marketSlug": "macro-event",
            },
            {
                "proxyWallet": "0xabc",
                "conditionId": "m1",
                "outcome": "Yes",
                "side": "BUY",
                "price": 1_000_000,
                "size": 50,
                "marketSlug": "macro-event",
            }
        ]
    )

    assert observations == [
        {
            "wallet_address": "0xabc",
            "market_id": "m1",
            "category": "macro-event",
            "action": "BUY_YES",
            "observed_size_usdc": 20.0,
        }
    ]


def test_build_wallet_markouts_from_trade_rows_uses_lagged_public_price_tape() -> None:
    wallet_rows = [
        {
            "proxyWallet": "0xabc",
            "conditionId": "m1",
            "outcome": "Yes",
            "side": "BUY",
            "price": 0.40,
            "size": 50,
            "timestamp": 1_700_000_000,
            "marketSlug": "macro-event",
        },
        {
            "proxyWallet": "0xabc",
            "conditionId": "m1",
            "outcome": "Yes",
            "side": "SELL",
            "price": 0.41,
            "size": 50,
            "timestamp": 1_700_000_010,
        },
    ]
    tape_rows = [
        {"conditionId": "m1", "outcome": "Yes", "price": 0.42, "size": 5, "timestamp": 1_700_000_100},
        {"conditionId": "m1", "outcome": "Yes", "price": 0.46, "size": 5, "timestamp": 1_700_000_300},
        {"conditionId": "m1", "outcome": "No", "price": 0.55, "size": 5, "timestamp": 1_700_000_300},
    ]

    markouts = build_wallet_markouts_from_trade_rows(wallet_rows, tape_rows, lag_sec=300)

    assert markouts == [
        {
            "wallet_address": "0xabc",
            "market_id": "m1",
            "category": "macro-event",
            "outcome": "yes",
            "entry_ts": 1_700_000_000,
            "markout_lag_sec": 300.0,
            "entry_price": 0.4,
            "markout_price": 0.46,
            "notional_usdc": 20.0,
            "lagged_follow_pnl_usdc": 3.0,
        }
    ]


def test_build_wallet_profiles_from_markout_rows_scores_lagged_follow_performance() -> None:
    profiles = build_wallet_profiles_from_markout_rows(
        [
            {
                "wallet_address": "0xabc",
                "category": "macro",
                "notional_usdc": 100,
                "realized_pnl_usdc": 12,
                "lagged_follow_pnl_usdc": 7,
            },
            {
                "wallet_address": "0xabc",
                "category": "macro",
                "notional_usdc": 100,
                "realized_pnl_usdc": -4,
                "lagged_follow_pnl_usdc": 3,
            },
        ],
        min_trades=2,
    )

    assert profiles["0xabc"]["trade_count"] == 2
    assert profiles["0xabc"]["realized_roi"] == 0.04
    assert profiles["0xabc"]["lagged_follow_roi"] == 0.05
    assert profiles["0xabc"]["category_edges"]["macro"] == 0.05


def test_build_wallet_profiles_prefers_net_follow_pnl_after_fees() -> None:
    profiles = build_wallet_profiles_from_markout_rows(
        [
            {
                "wallet_address": "0xabc",
                "category": "macro",
                "notional_usdc": 100,
                "lagged_follow_pnl_usdc": 12,
                "lagged_follow_pnl_net_usdc": 3,
            },
            {
                "wallet_address": "0xabc",
                "category": "macro",
                "notional_usdc": 100,
                "lagged_follow_pnl_usdc": 12,
                "lagged_follow_pnl_net_usdc": -1,
            },
        ],
        min_trades=2,
    )

    assert profiles["0xabc"]["lagged_follow_roi"] == 0.01
    assert profiles["0xabc"]["category_edges"]["macro"] == 0.01


def test_promote_wallet_profiles_filters_only_validated_wallets() -> None:
    rows = []
    for _ in range(3):
        rows.append(
            {
                "wallet_address": "0xgood",
                "notional_usdc": 100,
                "realized_pnl_usdc": 10,
                "lagged_follow_pnl_usdc": 6,
            }
        )
        rows.append(
            {
                "wallet_address": "0xbad",
                "notional_usdc": 100,
                "realized_pnl_usdc": 10,
                "lagged_follow_pnl_usdc": -2,
            }
        )

    promoted = promote_wallet_profiles_from_markout_rows(
        rows,
        min_trades=3,
        min_lagged_roi=0.04,
        max_concentration=0.5,
        max_drawdown=0.5,
    )

    assert promoted["schema_version"] == 1
    assert list(promoted["wallets"]) == ["0xgood"]
    assert promoted["wallets"]["0xgood"]["lagged_follow_roi"] == 0.06


def test_promote_wallet_profiles_respects_holdout_and_t_stat() -> None:
    promoted = promote_wallet_profiles_from_markout_rows(
        [
            {
                "wallet_address": "0xlucky",
                "notional_usdc": 100,
                "lagged_follow_pnl_usdc": 20,
                "close_ts": 2_000,
            },
            {
                "wallet_address": "0xlucky",
                "notional_usdc": 100,
                "lagged_follow_pnl_usdc": -10,
                "close_ts": 1_000,
            },
            {
                "wallet_address": "0xlucky",
                "notional_usdc": 100,
                "lagged_follow_pnl_usdc": 0,
                "close_ts": 1_000,
            },
        ],
        min_trades=2,
        min_lagged_roi=0.01,
        max_concentration=1.0,
        max_drawdown=1.0,
        holdout_sec=500,
        now_ts=2_000,
        min_t_stat=2.0,
    )

    assert promoted["wallets"] == {}


def test_build_wallet_markouts_from_shadow_rows_joins_entry_context_to_closed_positions() -> None:
    markouts = build_wallet_markouts_from_shadow_rows(
        virtual_fill_rows=[
            {
                "trade_id": "entry-1",
                "market_id": "m1",
                "price": 0.40,
                "decision_context": {
                    "wallet_address": "0xgood",
                    "category": "macro",
                    "signal_type": "wallet_alpha_candidate_buy_yes",
                },
                "result": {"filled_size": 10, "avg_fill_price": 0.40},
            }
        ],
        lifecycle_rows=[
            {
                "event": "position_closed",
                "open_trade_id": "entry-1",
                "market_id": "m1",
                "open_price": 0.40,
                "close_price": 0.55,
                "close_size": 10,
                "close_ts": 1234,
                "realized_pnl": 1.4,
                "lagged_follow_pnl_usdc": 0.9,
                "lagged_follow_pnl_net_usdc": 0.7,
                "markout_pnl_5m_usdc": 0.8,
                "markout_pnl_30m_usdc": 1.1,
                "markout_pnl_4h_usdc": 1.6,
                "settlement_pnl_usdc": 2.0,
            }
        ],
    )

    assert markouts == [
        {
            "wallet_address": "0xgood",
            "market_id": "m1",
            "category": "macro",
            "notional_usdc": 4.0,
            "realized_pnl_usdc": 1.4,
            "lagged_follow_pnl_usdc": 0.9,
            "lagged_follow_pnl_net_usdc": 0.7,
            "markout_pnl_5m_usdc": 0.8,
            "markout_pnl_30m_usdc": 1.1,
            "markout_pnl_4h_usdc": 1.6,
            "settlement_pnl_usdc": 2.0,
            "close_ts": 1234.0,
        }
    ]
