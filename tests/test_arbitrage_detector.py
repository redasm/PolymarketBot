"""ArbitrageDetector 单元测试：用 mock 订单簿验证二元/多结果套利检测."""

import pytest

from polymarket_arb.arbitrage_detector import ArbitrageDetector
from polymarket_arb.config import ArbConfig
from polymarket_arb.models import EventInfo, MarketInfo, TokenInfo

from tests.conftest import MockOrderBookAnalyzer


def _minimal_config(**overrides) -> ArbConfig:
    """构造最小可用的 ArbConfig（绕过 .env 依赖）."""
    defaults = dict(
        private_key="0xdead",
        funder_address="0xbeef",
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        gamma_host="https://gamma-api.polymarket.com",
        min_edge_usd=0.001,
        min_edge_pct=0.1,
        max_order_size_usdc=50.0,
        default_order_size_usdc=10.0,
        scan_interval_sec=5.0,
        market_fetch_limit=100,
        dry_run=True,
        min_liquidity=0,
        min_volume_24h=0,
        max_open_positions=10,
        max_exposure_per_market=100,
        max_total_exposure=500,
        max_daily_loss=50,
        max_consecutive_failures=5,
        telegram_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
        notify_on_arb_found=False,
        notify_on_trade=False,
        notify_on_error=False,
        telegram_cooldown_sec=30,
        vol_fast_minutes=60,
        vol_slow_minutes=360,
        vol_min_bars=20,
        edge_min_bps=100,
        edge_max_spread_bps=500,
        edge_min_confidence=0.4,
        tick_record_enabled=False,
        tick_record_dir="data/ticks",
        dashboard_enabled=False,
        dashboard_port=8077,
        log_level="WARNING",
        log_file="",
    )
    defaults.update(overrides)
    return ArbConfig(**defaults)


class TestBinaryArbDetection:
    """二元市场套利检测."""

    def test_profitable_binary_arb(self, make_snapshot):
        """ask_yes(0.45) + ask_no(0.50) = 0.95 < 1.0 → 应检出套利."""
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.44, best_ask=0.45),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.49, best_ask=0.50),
        }
        market = MarketInfo(
            condition_id="c1", question="Test?", slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True, closed=False, event_id="e1",
        )
        config = _minimal_config()
        detector = ArbitrageDetector(config, MockOrderBookAnalyzer(snapshots))
        opp = detector.scan_binary_market(market)

        assert opp is not None
        assert opp.total_cost == pytest.approx(0.95, abs=0.001)
        assert opp.gross_edge == pytest.approx(0.05, abs=0.001)
        assert opp.net_edge > 0
        assert opp.is_profitable
        assert len(opp.legs) == 2

    def test_no_arb_when_sum_exceeds_one(self, make_snapshot):
        """ask_yes(0.52) + ask_no(0.51) = 1.03 > 1.0 → 不应检出."""
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_ask=0.52),
            "0xno": make_snapshot(token_id="0xno", best_ask=0.51),
        }
        market = MarketInfo(
            condition_id="c1", question="Test?", slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True, closed=False, event_id="e1",
        )
        config = _minimal_config()
        detector = ArbitrageDetector(config, MockOrderBookAnalyzer(snapshots))
        assert detector.scan_binary_market(market) is None

    def test_no_arb_on_closed_market(self, make_snapshot):
        """已关闭的市场不应参与检测."""
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_ask=0.40),
            "0xno": make_snapshot(token_id="0xno", best_ask=0.40),
        }
        market = MarketInfo(
            condition_id="c1", question="Test?", slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True, closed=True, event_id="e1",
        )
        config = _minimal_config()
        detector = ArbitrageDetector(config, MockOrderBookAnalyzer(snapshots))
        assert detector.scan_binary_market(market) is None

    def test_edge_below_threshold_filtered(self, make_snapshot):
        """微利（sum=0.975）扣 2% fee 后亏损 → 不应检出."""
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_ask=0.49),
            "0xno": make_snapshot(token_id="0xno", best_ask=0.49),
        }
        market = MarketInfo(
            condition_id="c1", question="Test?", slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True, closed=False, event_id="e1",
        )
        config = _minimal_config(min_edge_usd=0.005, min_edge_pct=0.3)
        detector = ArbitrageDetector(config, MockOrderBookAnalyzer(snapshots))
        assert detector.scan_binary_market(market) is None


class TestMultiOutcomeArbDetection:
    """多结果事件套利检测."""

    def test_three_way_arb(self, make_snapshot):
        """三选一: 0.30 + 0.30 + 0.30 = 0.90 < 1.0 → 应检出."""
        snapshots = {
            f"0xt{i}": make_snapshot(token_id=f"0xt{i}", best_ask=0.30, best_bid=0.29)
            for i in range(3)
        }
        markets = [
            MarketInfo(
                condition_id=f"c{i}", question=f"Candidate {i}?", slug=f"cand-{i}",
                tokens=[TokenInfo(token_id=f"0xt{i}", outcome=f"Candidate {i}")],
                active=True, closed=False, event_id="e1",
            )
            for i in range(3)
        ]
        event = EventInfo(event_id="e1", slug="election", title="Who wins?", markets=markets)

        config = _minimal_config()
        detector = ArbitrageDetector(config, MockOrderBookAnalyzer(snapshots))
        opp = detector.scan_multi_outcome_event(event)

        assert opp is not None
        assert opp.total_cost == pytest.approx(0.90, abs=0.01)
        assert opp.net_edge > 0
        assert len(opp.legs) == 3
