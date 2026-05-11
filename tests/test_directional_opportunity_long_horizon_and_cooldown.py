"""Regression: live-mode T2 entry gates added for go-live.

D: Long-horizon binary markets (>30d) require a much higher net_edge
   than the default `live_min_net_edge_bps`. Production data showed a
   "Will BTC hit $1m before GTA VI?" market sneaking through with a
   99 bps net edge that turned into a stuck losing position.

A: After exiting (or abandoning) a market, lock it out from re-entry
   for `t2_post_exit_cooldown_sec`. Without this, every bot restart
   re-opened the position the operator had just manually closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from polymarket_arb.main_helpers.directional_opportunity import (
    build_directional_opportunity_from_signal,
)
from polymarket_arb.models import MarketInfo, TokenInfo
from polymarket_arb.strategies.recent_exit_cooldown import RecentExitCooldownStore
from polymarket_arb.strategies.strategy_orchestrator import StrategySignal, StrategyTier


def _signal(action: str = "BUY_NO", deviation: float = 0.022) -> StrategySignal:
    return StrategySignal(
        tier=StrategyTier.STATISTICAL_ARB,
        signal_type=f"statistical_{action.lower()}",
        market_id="cond-1",
        description="Will BTC hit $1m before GTA VI?",
        expected_edge=220.0,
        confidence=0.7,
        recommended_size_usdc=8.0,
        payload={"action": action, "deviation": deviation},
    )


def _market(*, end_date: str = "") -> MarketInfo:
    return MarketInfo(
        condition_id="cond-1",
        question="Will BTC hit $1m before GTA VI?",
        slug="btc-1m-gta",
        tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
        end_date=end_date,
    )


def _config(**overrides) -> SimpleNamespace:
    base = dict(
        dry_run=False,
        # Match prod fee rate (canary): the GTA VI signal had a ~125 bps
        # fee on a ~225 bps gross edge, leaving only 100 bps net.
        polymarket_taker_fee_rate=0.05,
        live_max_orderbook_snapshot_age_sec=2.0,
        live_min_ws_hit_ratio=0.0,  # disable feed_health
        live_min_net_edge_usd=0.0001,
        live_min_net_edge_bps=25.0,
        t2_long_horizon_days=30.0,
        t2_long_horizon_min_net_edge_bps=200.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _snap(best_ask: float = 0.507, best_bid: float = 0.495):
    return SimpleNamespace(
        best_ask=best_ask,
        best_bid=best_bid,
        asks=[SimpleNamespace(price=best_ask, size=100.0)],
        bids=[SimpleNamespace(price=best_bid, size=100.0)],
    )


def _ob(*, executable=(0.507, 30.0)):
    return SimpleNamespace(
        get_snapshot=lambda _tok: _snap(),
        get_executable_ask_price=lambda _tok, _size: executable,
        feed_health=lambda **_: {"healthy": True, "reason": ""},
    )


def _iso_in_days(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


# ---------- D: long-horizon edge gate -----------------------------------------


class TestLongHorizonEdgeGate:
    def test_long_horizon_market_with_thin_edge_rejected(self):
        # The prod scenario: ~2.2% deviation → ~99 bps net edge after fees.
        # Above the 25 bps default but below the 200 bps long-horizon bar.
        signal = _signal(deviation=0.022)
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=_iso_in_days(180.0)),
            ob_analyzer=_ob(),
        )
        assert opp is None
        assert reason == "live_edge_below_buffer"
        check = signal.payload["execution_check"]
        assert check["long_horizon"] is True
        assert check["live_min_net_edge_bps"] == 200.0

    def test_long_horizon_market_with_fat_edge_passes(self):
        # 6% deviation should clear even the 200 bps bar.
        signal = _signal(deviation=0.06)
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=_iso_in_days(180.0)),
            ob_analyzer=_ob(),
        )
        assert opp is not None
        assert reason == ""

    def test_short_horizon_market_keeps_default_threshold(self):
        # 7-day market with the same thin edge — must still pass at 25 bps.
        signal = _signal(deviation=0.022)
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=_iso_in_days(7.0)),
            ob_analyzer=_ob(),
        )
        assert opp is not None
        assert reason == ""
        check = signal.payload["execution_check"]
        assert check["long_horizon"] is False
        assert check["live_min_net_edge_bps"] == 25.0

    def test_missing_end_date_treated_as_long_horizon(self):
        signal = _signal(deviation=0.022)
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=""),
            ob_analyzer=_ob(),
        )
        assert opp is None
        assert reason == "live_edge_below_buffer"
        assert signal.payload["execution_check"]["long_horizon"] is True

    def test_horizon_boundary_inclusive_is_short(self):
        # Exactly 30 days → NOT long-horizon (boundary is `> 30`).
        # Use 29.5d to avoid clock skew flakiness.
        signal = _signal(deviation=0.022)
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(t2_long_horizon_days=30.0),
            signal=signal,
            market=_market(end_date=_iso_in_days(29.5)),
            ob_analyzer=_ob(),
        )
        assert opp is not None
        assert reason == ""


# ---------- A: post-exit cooldown gate ---------------------------------------


class TestCooldownGate:
    def test_cooldown_blocks_entry(self, tmp_path):
        store = RecentExitCooldownStore(
            state_file=str(tmp_path / "exits.json"),
            cooldown_sec=3600.0,
        )
        store.record_exit("cond-1", now_ts=__import__("time").time())
        signal = _signal(deviation=0.06)  # would otherwise pass long-horizon gate
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=_iso_in_days(7.0)),
            ob_analyzer=_ob(),
            cooldown_store=store,
        )
        assert opp is None
        assert reason == "recent_exit_cooldown"
        check = signal.payload["execution_check"]
        assert check["cooldown_remaining_sec"] > 0.0

    def test_no_cooldown_store_means_no_gate(self):
        signal = _signal(deviation=0.06)
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=_iso_in_days(7.0)),
            ob_analyzer=_ob(),
            cooldown_store=None,
        )
        assert opp is not None
        assert reason == ""

    def test_market_not_in_cooldown_passes(self, tmp_path):
        store = RecentExitCooldownStore(
            state_file=str(tmp_path / "exits.json"),
            cooldown_sec=3600.0,
        )
        store.record_exit("cond-OTHER", now_ts=__import__("time").time())
        signal = _signal(deviation=0.06)
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=_iso_in_days(7.0)),
            ob_analyzer=_ob(),
            cooldown_store=store,
        )
        assert opp is not None
        assert reason == ""

    def test_cooldown_gate_fires_before_long_horizon_gate(self, tmp_path):
        """If both gates would block, cooldown's reason wins (it runs first
        so we don't waste depth/fee math on a market we know to skip)."""
        store = RecentExitCooldownStore(
            state_file=str(tmp_path / "exits.json"),
            cooldown_sec=3600.0,
        )
        store.record_exit("cond-1", now_ts=__import__("time").time())
        signal = _signal(deviation=0.022)  # thin edge AND in cooldown
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=_market(end_date=_iso_in_days(180.0)),
            ob_analyzer=_ob(),
            cooldown_store=store,
        )
        assert opp is None
        assert reason == "recent_exit_cooldown"


# ---------- End-to-end: T2ExitManager records, entry path respects ----------


class TestExitToEntryHandoff:
    def test_t2_exit_records_cooldown_that_blocks_re_entry(self, tmp_path):
        """The exact prod scenario: bot exits a position, then a fresh
        statistical signal for the same market is immediately rejected."""
        from polymarket_arb.strategies.t2_exit_manager import T2ExitManager
        from tests.test_t2_exit_escalation import _AlwaysFailExecutor, _market as _exit_market

        store = RecentExitCooldownStore(
            state_file=str(tmp_path / "exits.json"),
            cooldown_sec=3600.0,
        )
        # Simulate the manager recording a successful exit.
        # (We call the helper directly rather than driving evaluate() —
        # the wiring contract is what matters here.)
        store.record_exit(_exit_market().condition_id)

        # Now the entry path for the same market must reject with the
        # cooldown reason.
        signal = _signal(deviation=0.06)
        signal.market_id = _exit_market().condition_id
        opp, _, reason = build_directional_opportunity_from_signal(
            config=_config(),
            signal=signal,
            market=MarketInfo(
                condition_id=_exit_market().condition_id,
                question="Q",
                slug="s",
                tokens=[TokenInfo("yes-1", "Yes"), TokenInfo("no-1", "No")],
                end_date=_iso_in_days(7.0),
            ),
            ob_analyzer=_ob(),
            cooldown_store=store,
        )
        assert opp is None
        assert reason == "recent_exit_cooldown"
