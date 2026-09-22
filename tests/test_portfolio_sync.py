from __future__ import annotations

from polymarket_arb.portfolio_sync import PortfolioSync
from tests.conftest import make_test_config


def test_portfolio_sync_refresh_parses_positions_and_daily_realized_pnl(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params or {}), timeout))

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                if url.endswith("/positions"):
                    return [
                        {
                            "asset": "token-yes",
                            "conditionId": "cond-1",
                            "outcome": "Yes",
                            "size": 3,
                            "avgPrice": 0.42,
                            "currentValue": 1.41,
                            "cashPnl": 0.15,
                        }
                    ]
                if url.endswith("/closed-positions"):
                    if params.get("offset", 0) == 0:
                        return [
                            {"timestamp": 1713446500, "realizedPnl": 2.5},
                            {"timestamp": 1713450000, "realizedPnl": -0.5},
                        ]
                    return []
                raise AssertionError(f"unexpected url {url}")

        return _Resp()

    monkeypatch.setattr("polymarket_arb.portfolio_sync.requests.get", fake_get)

    sync = PortfolioSync(
        make_test_config(
            portfolio_sync_enabled=True,
            portfolio_sync_timeout_sec=7.0,
            data_api_host="https://data-api.polymarket.com",
            portfolio_sync_user_address="0xabc",
        )
    )

    snapshot = sync.refresh(now_ts=1713446400)  # 2024-04-18 00:00:00 UTC

    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].token_id == "token-yes"
    assert snapshot.realized_daily_pnl == 2.0
    assert calls[0][2] == 7.0


def test_portfolio_sync_ignores_closed_positions_before_utc_day_start(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                if url.endswith("/positions"):
                    return []
                if url.endswith("/closed-positions"):
                    return [
                        {"timestamp": 1713398399, "realizedPnl": 9.9},  # previous UTC day
                        {"timestamp": 1713446500, "realizedPnl": 1.2},
                    ]
                raise AssertionError(f"unexpected url {url}")

        return _Resp()

    monkeypatch.setattr("polymarket_arb.portfolio_sync.requests.get", fake_get)

    sync = PortfolioSync(make_test_config(portfolio_sync_enabled=True, portfolio_sync_user_address="0xabc"))
    snapshot = sync.refresh(now_ts=1713446400)

    assert snapshot.realized_daily_pnl == 1.2
