from __future__ import annotations

import json
import sys


def test_scan_quant_strategy_inputs_builds_wallet_profiles_from_markout_file(tmp_path, capsys, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    input_path = tmp_path / "markouts.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "wallet_address": "0xabc",
                    "category": "macro",
                    "notional_usdc": 100,
                    "realized_pnl_usdc": 10,
                    "lagged_follow_pnl_usdc": 6,
                },
                {
                    "wallet_address": "0xabc",
                    "category": "macro",
                    "notional_usdc": 100,
                    "realized_pnl_usdc": -2,
                    "lagged_follow_pnl_usdc": 4,
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["scan_quant_strategy_inputs.py", "wallet-profiles", "--input", str(input_path), "--min-trades", "2"],
    )

    assert scan_quant_strategy_inputs.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["0xabc"]["lagged_follow_roi"] == 0.05


def test_scan_quant_strategy_inputs_builds_logical_candidates_from_events_file(tmp_path, capsys, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    input_path = tmp_path / "events.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "id": "event",
                    "slug": "event",
                    "title": "Election",
                    "markets": [
                        {
                            "condition_id": "candidate",
                            "question": "Will Alice win?",
                            "slug": "candidate",
                            "tokens": [{"token_id": "y1", "outcome": "Yes"}, {"token_id": "n1", "outcome": "No"}],
                            "liquidity": 1000,
                            "volume24hr": 500,
                        },
                        {
                            "condition_id": "party",
                            "question": "Will Alice party win?",
                            "slug": "party",
                            "tokens": [{"token_id": "y2", "outcome": "Yes"}, {"token_id": "n2", "outcome": "No"}],
                            "liquidity": 1000,
                            "volume24hr": 500,
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["scan_quant_strategy_inputs.py", "logical-candidates", "--events-json", str(input_path)],
    )

    assert scan_quant_strategy_inputs.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert payload[0]["selector"] == "same_event_binary_pair"


def test_scan_quant_strategy_inputs_can_write_output_file_atomically(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    input_path = tmp_path / "markouts.json"
    output_path = tmp_path / "wallet_profiles.json"
    input_path.write_text(
        json.dumps(
            [
                {"wallet_address": "0xabc", "notional_usdc": 100, "lagged_follow_pnl_usdc": 5},
                {"wallet_address": "0xabc", "notional_usdc": 100, "lagged_follow_pnl_usdc": 5},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "wallet-profiles",
            "--input",
            str(input_path),
            "--min-trades",
            "2",
            "--output",
            str(output_path),
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    assert json.loads(output_path.read_text(encoding="utf-8"))["0xabc"]["lagged_follow_roi"] == 0.05


def test_scan_quant_strategy_inputs_auto_wallet_observations_discovers_wallets(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    output_path = tmp_path / "wallet_observations.json"

    class _FakeClient:
        def __init__(self, host):
            self.host = host

        def fetch_recent_trades(self, *, limit=500, offset=0):
            return [
                {"proxyWallet": "0xaaa", "price": 0.5, "size": 100},
                {"proxyWallet": "0xaaa", "price": 0.5, "size": 100},
            ]

        def fetch_trades(self, wallet, *, limit=200, offset=0):
            return [
                {
                    "proxyWallet": wallet,
                    "conditionId": "m1",
                    "outcome": "Yes",
                    "side": "BUY",
                    "price": 0.4,
                    "size": 25,
                }
            ]

    monkeypatch.setattr(scan_quant_strategy_inputs, "DataApiWalletTradeClient", _FakeClient)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "auto-wallet-observations",
            "--min-trades",
            "2",
            "--min-notional",
            "50",
            "--output",
            str(output_path),
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload[0]["wallet_address"] == "0xaaa"
    assert payload[0]["market_id"] == "m1"


def test_scan_quant_strategy_inputs_repeat_mode_refreshes_output(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    output_path = tmp_path / "wallet_observations.json"
    calls = {"recent": 0}

    class _FakeClient:
        def __init__(self, host):
            self.host = host

        def fetch_recent_trades(self, *, limit=500, offset=0):
            calls["recent"] += 1
            return [
                {"proxyWallet": "0xaaa", "price": 0.5, "size": 100},
                {"proxyWallet": "0xaaa", "price": 0.5, "size": 100},
            ]

        def fetch_trades(self, wallet, *, limit=200, offset=0):
            return [
                {
                    "proxyWallet": wallet,
                    "conditionId": f"m{calls['recent']}",
                    "outcome": "Yes",
                    "side": "BUY",
                    "price": 0.4,
                    "size": 25,
                }
            ]

    monkeypatch.setattr(scan_quant_strategy_inputs, "DataApiWalletTradeClient", _FakeClient)
    monkeypatch.setattr(scan_quant_strategy_inputs.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "auto-wallet-observations",
            "--min-trades",
            "2",
            "--min-notional",
            "50",
            "--output",
            str(output_path),
            "--repeat-interval-sec",
            "1",
            "--repeat-count",
            "2",
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    assert calls["recent"] == 2
    assert json.loads(output_path.read_text(encoding="utf-8"))[0]["market_id"] == "m2"


def test_scan_quant_strategy_inputs_promotes_only_validated_wallet_profiles(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    markouts_path = tmp_path / "wallet_markouts.json"
    output_path = tmp_path / "wallet_profiles.json"
    rows = []
    for _ in range(3):
        rows.append({"wallet_address": "0xgood", "notional_usdc": 100, "lagged_follow_pnl_usdc": 6})
        rows.append({"wallet_address": "0xbad", "notional_usdc": 100, "lagged_follow_pnl_usdc": -1})
    markouts_path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "promote-wallet-profiles",
            "--input",
            str(markouts_path),
            "--min-trades",
            "3",
            "--min-lagged-roi",
            "0.04",
            "--output",
            str(output_path),
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert list(payload) == ["0xgood"]


def test_scan_quant_strategy_inputs_builds_wallet_markouts_from_shadow_telemetry(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    output_path = tmp_path / "wallet_markouts.json"
    (telemetry_dir / "2026-05-21.virtual_fills.ndjson").write_text(
        json.dumps(
            {
                "trade_id": "entry-1",
                "market_id": "m1",
                "price": 0.4,
                "decision_context": {
                    "wallet_address": "0xgood",
                    "category": "macro",
                    "signal_type": "wallet_alpha_candidate_buy_yes",
                },
                "result": {"filled_size": 10, "avg_fill_price": 0.4},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (telemetry_dir / "2026-05-21.positions_lifecycle.ndjson").write_text(
        json.dumps(
            {
                "event": "position_closed",
                "open_trade_id": "entry-1",
                "market_id": "m1",
                "open_price": 0.4,
                "close_size": 10,
                "realized_pnl": 1.4,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "wallet-markouts-from-telemetry",
            "--telemetry-dir",
            str(telemetry_dir),
            "--date",
            "2026-05-21",
            "--output",
            str(output_path),
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload[0]["wallet_address"] == "0xgood"
    assert payload[0]["lagged_follow_pnl_usdc"] == 1.4


def test_scan_quant_strategy_inputs_markouts_defaults_to_recent_dates(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    output_path = tmp_path / "wallet_markouts.json"
    (telemetry_dir / "2026-05-21.virtual_fills.ndjson").write_text(
        json.dumps(
            {
                "trade_id": "entry-1",
                "market_id": "m1",
                "price": 0.4,
                "decision_context": {"wallet_address": "0xgood", "signal_type": "wallet_alpha_candidate_buy_yes"},
                "result": {"filled_size": 10, "avg_fill_price": 0.4},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (telemetry_dir / "2026-05-21.positions_lifecycle.ndjson").write_text(
        json.dumps({"event": "position_closed", "open_trade_id": "entry-1", "open_price": 0.4, "close_size": 10, "realized_pnl": 1.0})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scan_quant_strategy_inputs, "_today_utc", lambda: "2026-05-21")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "wallet-markouts-from-telemetry",
            "--telemetry-dir",
            str(telemetry_dir),
            "--lookback-days",
            "1",
            "--output",
            str(output_path),
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    assert json.loads(output_path.read_text(encoding="utf-8"))[0]["wallet_address"] == "0xgood"


def test_scan_quant_strategy_inputs_auto_promotes_from_shadow_telemetry(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    output_path = tmp_path / "wallet_profiles.json"
    (telemetry_dir / "2026-05-21.virtual_fills.ndjson").write_text(
        "\n".join(
            json.dumps(
                {
                    "trade_id": f"entry-{idx}",
                    "market_id": f"m{idx}",
                    "price": 0.4,
                    "decision_context": {
                        "wallet_address": "0xgood",
                        "signal_type": "wallet_alpha_candidate_buy_yes",
                    },
                    "result": {"filled_size": 10, "avg_fill_price": 0.4},
                }
            )
            for idx in range(2)
        )
        + "\n",
        encoding="utf-8",
    )
    (telemetry_dir / "2026-05-21.positions_lifecycle.ndjson").write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "position_closed",
                    "open_trade_id": f"entry-{idx}",
                    "open_price": 0.4,
                    "close_size": 10,
                    "realized_pnl": 1.0,
                }
            )
            for idx in range(2)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scan_quant_strategy_inputs, "_today_utc", lambda: "2026-05-21")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "auto-promote-wallet-profiles",
            "--telemetry-dir",
            str(telemetry_dir),
            "--lookback-days",
            "1",
            "--min-trades",
            "2",
            "--min-lagged-roi",
            "0.01",
            "--max-concentration",
            "1.0",
            "--output",
            str(output_path),
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["0xgood"]["trade_count"] == 2


def test_scan_quant_strategy_inputs_repeat_keeps_running_after_transient_error(tmp_path, monkeypatch) -> None:
    from scripts import scan_quant_strategy_inputs

    output_path = tmp_path / "wallet_observations.json"
    calls = {"count": 0}

    def _fake_payload(_args):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary data-api failure")
        return [{"wallet_address": "0xok"}]

    monkeypatch.setattr(scan_quant_strategy_inputs, "_build_payload", _fake_payload)
    monkeypatch.setattr(scan_quant_strategy_inputs.time, "sleep", lambda _sec: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_quant_strategy_inputs.py",
            "auto-wallet-observations",
            "--repeat-interval-sec",
            "1",
            "--repeat-count",
            "2",
            "--output",
            str(output_path),
        ],
    )

    assert scan_quant_strategy_inputs.main() == 0

    assert json.loads(output_path.read_text(encoding="utf-8")) == [{"wallet_address": "0xok"}]


def test_scan_quant_strategy_inputs_logical_candidates_can_fetch_gamma(monkeypatch, capsys) -> None:
    from scripts import scan_quant_strategy_inputs

    class _FakeScanner:
        def __init__(self, config):
            self.config = config

        def fetch_active_events(self, *, limit=50):
            return [
                _event_with_markets("event", "Election"),
            ]

    monkeypatch.setattr(scan_quant_strategy_inputs, "MarketScanner", _FakeScanner)
    monkeypatch.setattr(
        scan_quant_strategy_inputs.ArbConfig,
        "from_env",
        lambda dotenv_path=None, require_wallet=False: type("Cfg", (), {"gamma_host": "https://gamma-api.polymarket.com"})(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["scan_quant_strategy_inputs.py", "logical-candidates", "--fetch-gamma", "--event-limit", "5"],
    )

    assert scan_quant_strategy_inputs.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["event_id"] == "event"


def _event_with_markets(event_id: str, title: str):
    from polymarket_arb.models import EventInfo, MarketInfo, TokenInfo

    return EventInfo(
        event_id=event_id,
        slug=event_id,
        title=title,
        markets=[
            MarketInfo(
                condition_id="candidate",
                question="Will Alice win?",
                slug="candidate",
                tokens=[TokenInfo(token_id="y1", outcome="Yes"), TokenInfo(token_id="n1", outcome="No")],
                liquidity=1000,
                volume_24h=500,
            ),
            MarketInfo(
                condition_id="party",
                question="Will Alice party win?",
                slug="party",
                tokens=[TokenInfo(token_id="y2", outcome="Yes"), TokenInfo(token_id="n2", outcome="No")],
                liquidity=1000,
                volume_24h=500,
            ),
        ],
    )
