"""ArbitrageDetector 单元测试：用 mock 订单簿验证二元/多结果套利检测."""

import pytest

from polymarket_arb.arbitrage_detector import ArbitrageDetector
from polymarket_arb.models import EventInfo, FeeStructure, MarketInfo, TokenInfo

from tests.conftest import MockOrderBookAnalyzer, make_test_config


def test_fee_structure_uses_clob_price_shape():
    fees = FeeStructure(taker_fee_rate=0.072)

    assert fees.estimate_price_fee(0.5) == pytest.approx(0.018)
    assert fees.estimate_price_fee(0.9) == pytest.approx(0.00648)
    assert fees.estimate_leg_fees([0.45, 0.50]) == pytest.approx(0.072 * (0.45 * 0.55 + 0.5 * 0.5))


def test_fee_structure_uses_market_fee_metadata():
    market = MarketInfo(
        condition_id="c1",
        question="Crypto market?",
        slug="crypto",
        tokens=[],
        raw={"feesEnabled": True, "feeRateBps": 720},
    )

    fees = FeeStructure.for_market(0.02, market)

    assert fees.taker_fee_rate == pytest.approx(0.072)
    assert fees.estimate_price_fee(0.50) == pytest.approx(0.018)


def test_fee_structure_honors_fee_disabled_market():
    market = MarketInfo(
        condition_id="c1",
        question="No fee market?",
        slug="nofee",
        tokens=[],
        raw={"feesEnabled": False, "feeRateBps": 720},
    )

    fees = FeeStructure.for_market(0.02, market)

    assert fees.taker_fee_rate == 0.0
    assert fees.estimate_price_fee(0.50) == 0.0


def test_fee_structure_reads_modern_gamma_fee_schedule():
    """`feeSchedule.rate` is the live Gamma field as of 2026-04 (older fields absent)."""
    market = MarketInfo(
        condition_id="c1",
        question="Modern market",
        slug="modern",
        tokens=[],
        raw={
            "feesEnabled": True,
            "feeSchedule": {"exponent": 1, "rate": 0.05, "rebateRate": 0.25, "takerOnly": True},
        },
    )

    fees = FeeStructure.for_market(0.072, market)

    # feeSchedule.rate=0.05 should win over the env fallback of 0.072.
    assert fees.taker_fee_rate == pytest.approx(0.05)
    # At p=0.5: fee = 0.05 * 0.5 * 0.5 = 0.0125 per share (CLOB shape).
    assert fees.estimate_price_fee(0.50) == pytest.approx(0.0125)


def test_fee_structure_falls_back_when_metadata_missing():
    """Without feeSchedule/feeRateBps the env-configured default applies."""
    market = MarketInfo(
        condition_id="c1",
        question="Bare market",
        slug="bare",
        tokens=[],
        raw={"feesEnabled": True},
    )

    fees = FeeStructure.for_market(0.05, market)

    assert fees.taker_fee_rate == pytest.approx(0.05)


def test_fee_structure_takerbasefee_ppm():
    """`takerBaseFee=1000` (ppm) → 0.001 = 10 bps."""
    market = MarketInfo(
        condition_id="c1",
        question="ppm market",
        slug="ppm",
        tokens=[],
        raw={"feesEnabled": True, "takerBaseFee": 1000},
    )

    fees = FeeStructure.for_market(0.05, market)

    assert fees.taker_fee_rate == pytest.approx(0.001)


class TestBinaryArbDetection:
    """二元市场套利检测."""

    def test_profitable_binary_arb(self, make_snapshot):
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.44, best_ask=0.45),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.49, best_ask=0.50),
        }
        market = MarketInfo(
            condition_id="c1",
            question="Test?",
            slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True,
            closed=False,
            event_id="e1",
        )
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        opp = detector.scan_binary_market(market)

        assert opp is not None
        assert opp.total_cost == pytest.approx(0.95, abs=0.001)
        assert opp.gross_edge == pytest.approx(0.05, abs=0.001)
        assert opp.net_edge == pytest.approx(0.05 - 0.02 * (0.45 * 0.55 + 0.50 * 0.50), abs=0.001)
        assert opp.net_edge > 0
        assert opp.is_profitable
        assert len(opp.legs) == 2

    def test_binary_arb_uses_outcome_labels_not_token_order(self, make_snapshot):
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.44, best_ask=0.45),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.49, best_ask=0.50),
        }
        market = MarketInfo(
            condition_id="c1",
            question="Test?",
            slug="test",
            tokens=[
                TokenInfo(token_id="0xno", outcome="No"),
                TokenInfo(token_id="0xyes", outcome="Yes"),
            ],
            active=True,
            closed=False,
            event_id="e1",
        )
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        opp = detector.scan_binary_market(market)

        assert opp is not None
        assert [leg.outcome for leg in opp.legs] == ["Yes", "No"]
        assert [leg.token_id for leg in opp.legs] == ["0xyes", "0xno"]

    def test_binary_arb_requires_yes_and_no_outcomes(self, make_snapshot):
        snapshots = {
            "0xa": make_snapshot(token_id="0xa", best_ask=0.45),
            "0xb": make_snapshot(token_id="0xb", best_ask=0.50),
        }
        market = MarketInfo(
            condition_id="c1",
            question="Test?",
            slug="test",
            tokens=[
                TokenInfo(token_id="0xa", outcome="Alpha"),
                TokenInfo(token_id="0xb", outcome="Beta"),
            ],
            active=True,
            closed=False,
            event_id="e1",
        )
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))

        assert detector.scan_binary_market(market) is None

    def test_no_arb_when_sum_exceeds_one(self, make_snapshot):
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_ask=0.52),
            "0xno": make_snapshot(token_id="0xno", best_ask=0.51),
        }
        market = MarketInfo(
            condition_id="c1",
            question="Test?",
            slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True,
            closed=False,
            event_id="e1",
        )
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        assert detector.scan_binary_market(market) is None

    def test_no_arb_on_closed_market(self, make_snapshot):
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_ask=0.40),
            "0xno": make_snapshot(token_id="0xno", best_ask=0.40),
        }
        market = MarketInfo(
            condition_id="c1",
            question="Test?",
            slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True,
            closed=True,
            event_id="e1",
        )
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        assert detector.scan_binary_market(market) is None

    def test_edge_below_threshold_filtered(self, make_snapshot):
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_ask=0.493),
            "0xno": make_snapshot(token_id="0xno", best_ask=0.493),
        }
        market = MarketInfo(
            condition_id="c1",
            question="Test?",
            slug="test",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True,
            closed=False,
            event_id="e1",
        )
        detector = ArbitrageDetector(
            make_test_config(min_edge_usd=0.005, min_edge_pct=0.3),
            MockOrderBookAnalyzer(snapshots),
        )
        assert detector.scan_binary_market(market) is None


class TestMultiOutcomeArbDetection:
    """多结果事件套利检测."""

    def test_three_way_arb(self, make_snapshot):
        snapshots = {
            f"0xt{i}": make_snapshot(token_id=f"0xt{i}", best_ask=0.30, best_bid=0.29)
            for i in range(3)
        }
        markets = [
            MarketInfo(
                condition_id=f"c{i}",
                question=f"Candidate {i}?",
                slug=f"cand-{i}",
                tokens=[TokenInfo(token_id=f"0xt{i}", outcome=f"Candidate {i}")],
                active=True,
                closed=False,
                event_id="e1",
            )
            for i in range(3)
        ]
        event = EventInfo(event_id="e1", slug="election", title="Who wins?", markets=markets)

        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        opp = detector.scan_multi_outcome_event(event)

        assert opp is not None
        assert opp.total_cost == pytest.approx(0.90, abs=0.01)
        assert opp.net_edge > 0
        assert len(opp.legs) == 3

    def test_multi_outcome_arb_skips_too_many_legs(self, make_snapshot):
        snapshots = {
            f"tok-{i}": make_snapshot(token_id=f"tok-{i}", best_ask=0.03, best_bid=0.02)
            for i in range(21)
        }
        markets = [
            MarketInfo(
                condition_id=f"c{i}",
                question=f"Candidate {i}?",
                slug=f"cand-{i}",
                tokens=[TokenInfo(token_id=f"tok-{i}", outcome=f"Candidate {i}")],
                active=True,
                closed=False,
                event_id="e-many",
            )
            for i in range(21)
        ]
        event = EventInfo(event_id="e-many", slug="e-many", title="Who wins many?", markets=markets)

        detector = ArbitrageDetector(
            make_test_config(max_multi_outcome_legs=20),
            MockOrderBookAnalyzer(snapshots),
        )

        assert detector.scan_multi_outcome_event(event) is None

    def test_multi_outcome_arb_skip_for_too_many_legs_is_debug_only(self, make_snapshot, caplog):
        snapshots = {
            f"tok-{i}": make_snapshot(token_id=f"tok-{i}", best_ask=0.03, best_bid=0.02)
            for i in range(21)
        }
        markets = [
            MarketInfo(
                condition_id=f"c{i}",
                question=f"Candidate {i}?",
                slug=f"cand-{i}",
                tokens=[TokenInfo(token_id=f"tok-{i}", outcome=f"Candidate {i}")],
                active=True,
                closed=False,
                event_id="e-many",
            )
            for i in range(21)
        ]
        event = EventInfo(event_id="e-many", slug="e-many", title="Who wins many?", markets=markets)
        detector = ArbitrageDetector(
            make_test_config(max_multi_outcome_legs=20),
            MockOrderBookAnalyzer(snapshots),
        )

        with caplog.at_level("INFO"):
            assert detector.scan_multi_outcome_event(event) is None

        assert "跳过超多腿多结果事件" not in caplog.text

    def test_neg_risk_sell_leg_uses_bid_for_execution_and_complement_for_cost(self, make_snapshot):
        snapshots = {
            "yes1": make_snapshot(
                token_id="yes1",
                best_bid=0.69,
                best_ask=0.72,
                bids=[(0.69, 50)],
                asks=[(0.72, 50)],
            ),
            "no1": make_snapshot(
                token_id="no1",
                best_bid=0.65,
                best_ask=0.68,
                bids=[(0.65, 50)],
                asks=[(0.68, 50)],
            ),
            "yes2": make_snapshot(
                token_id="yes2",
                best_bid=0.28,
                best_ask=0.30,
                bids=[(0.28, 50)],
                asks=[(0.30, 50)],
            ),
            "no2": make_snapshot(
                token_id="no2",
                best_bid=0.70,
                best_ask=0.72,
                bids=[(0.70, 50)],
                asks=[(0.72, 50)],
            ),
        }
        event = EventInfo(
            event_id="e-neg",
            slug="neg-risk",
            title="Neg risk event",
            markets=[
                MarketInfo(
                    condition_id="c1",
                    question="Outcome 1?",
                    slug="outcome-1",
                    tokens=[TokenInfo(token_id="yes1", outcome="Yes"), TokenInfo(token_id="no1", outcome="No")],
                    active=True,
                    closed=False,
                    event_id="e-neg",
                    neg_risk=True,
                ),
                MarketInfo(
                    condition_id="c2",
                    question="Outcome 2?",
                    slug="outcome-2",
                    tokens=[TokenInfo(token_id="yes2", outcome="Yes"), TokenInfo(token_id="no2", outcome="No")],
                    active=True,
                    closed=False,
                    event_id="e-neg",
                    neg_risk=True,
                ),
            ],
        )

        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        opp = detector.scan_multi_outcome_event(event)

        assert opp is not None
        sell_leg = next(leg for leg in opp.legs if leg.side.value == "SELL")
        assert sell_leg.execution_price == pytest.approx(0.65, abs=1e-6)
        assert sell_leg.economic_cost == pytest.approx(0.35, abs=1e-6)

        verified = detector.verify_opportunity_with_depth(opp, 10)
        assert verified is not None
        verified_sell_leg = next(leg for leg in verified.legs if leg.side.value == "SELL")
        assert verified_sell_leg.execution_price == pytest.approx(0.65, abs=1e-6)
        assert verified_sell_leg.economic_cost == pytest.approx(0.35, abs=1e-6)

    def test_neg_risk_leg_returns_none_when_complementary_bid_missing(self, make_snapshot):
        snapshots = {
            "yes1": make_snapshot(token_id="yes1", best_bid=None, best_ask=None, bids=[], asks=[]),
            "no1": make_snapshot(token_id="no1", best_bid=None, best_ask=0.68, bids=[], asks=[(0.68, 50)]),
        }
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        market = MarketInfo(
            condition_id="c-neg",
            question="Outcome 1?",
            slug="outcome-1",
            tokens=[TokenInfo(token_id="yes1", outcome="Yes"), TokenInfo(token_id="no1", outcome="No")],
            active=True,
            closed=False,
            event_id="e-neg",
            neg_risk=True,
        )

        assert detector._get_neg_risk_leg(market) is None

    def test_time_ladder_event_is_not_treated_as_multi_outcome_arb(self, make_snapshot):
        snapshots = {
            "yes-jun": make_snapshot(token_id="yes-jun", best_ask=0.02, best_bid=0.01),
            "no-jun": make_snapshot(token_id="no-jun", best_ask=0.99, best_bid=0.98),
            "yes-dec": make_snapshot(token_id="yes-dec", best_ask=0.10, best_bid=0.09),
            "no-dec": make_snapshot(token_id="no-dec", best_ask=0.91, best_bid=0.90),
        }
        event = EventInfo(
            event_id="e-time",
            slug="btc-150k",
            title="When will Bitcoin hit $150k?",
            markets=[
                MarketInfo(
                    condition_id="c-jun",
                    question="Will Bitcoin hit $150k by June 30, 2026?",
                    slug="btc-150k-jun",
                    tokens=[TokenInfo(token_id="yes-jun", outcome="Yes"), TokenInfo(token_id="no-jun", outcome="No")],
                    active=True,
                    closed=False,
                    event_id="e-time",
                ),
                MarketInfo(
                    condition_id="c-dec",
                    question="Will Bitcoin hit $150k by December 31, 2026?",
                    slug="btc-150k-dec",
                    tokens=[TokenInfo(token_id="yes-dec", outcome="Yes"), TokenInfo(token_id="no-dec", outcome="No")],
                    active=True,
                    closed=False,
                    event_id="e-time",
                ),
            ],
        )

        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        assert detector.scan_multi_outcome_event(event) is None


class TestCrossedBookGuard:
    """交叉簿(best_bid >= best_ask)= stale 快照, 必须拒绝, 否则产生不可成交的假套利.

    复刻 2026-05-26 ev=34584: Yes bid=0.71 > ask=0.58, No bid=0.42 > ask=0.29,
    asks 和 0.87 < 1 看似 12.9% 套利, 但 bid>ask 物理不可成交。
    """

    def _market(self):
        return MarketInfo(
            condition_id="c-cross",
            question="Crossed?",
            slug="crossed",
            tokens=[
                TokenInfo(token_id="0xyes", outcome="Yes"),
                TokenInfo(token_id="0xno", outcome="No"),
            ],
            active=True,
            closed=False,
            event_id="e-cross",
        )

    def test_is_crossed_book_helper(self, make_snapshot):
        from polymarket_arb.arbitrage_detector import _is_crossed_book
        assert _is_crossed_book(make_snapshot(best_bid=0.71, best_ask=0.58)) is True
        assert _is_crossed_book(make_snapshot(best_bid=0.58, best_ask=0.58)) is True  # locked
        assert _is_crossed_book(make_snapshot(best_bid=0.44, best_ask=0.45)) is False
        assert _is_crossed_book(None) is False

    def test_binary_crossed_book_rejected(self, make_snapshot):
        # 复刻 ev=34584 的交叉簿
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.71, best_ask=0.58),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.42, best_ask=0.29),
        }
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        # asks 和 = 0.87 < 1, 旧逻辑会报 12.9% 假套利; 加防护后应拒绝
        assert detector.scan_binary_market(self._market()) is None

    def test_binary_one_leg_crossed_rejected(self, make_snapshot):
        # 只有 Yes 腿交叉, No 正常 -> 仍应拒绝(任一腿 stale 即不可成交)
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.71, best_ask=0.58),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.28, best_ask=0.29),
        }
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        assert detector.scan_binary_market(self._market()) is None

    def test_normal_book_real_edge_not_killed(self, make_snapshot):
        # 正常盘口(bid<ask) + 真实 edge: 不能被误杀
        snapshots = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.44, best_ask=0.45),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.49, best_ask=0.50),
        }
        detector = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(snapshots))
        opp = detector.scan_binary_market(self._market())
        assert opp is not None
        assert opp.net_edge > 0

    def test_verify_opportunity_rejects_crossed_book(self, make_snapshot):
        # 先用正常盘口检出机会, 再用交叉簿验证 -> 最后一道闸应拒绝
        normal = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.44, best_ask=0.45),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.49, best_ask=0.50),
        }
        detector_normal = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(normal))
        opp = detector_normal.scan_binary_market(self._market())
        assert opp is not None

        crossed = {
            "0xyes": make_snapshot(token_id="0xyes", best_bid=0.71, best_ask=0.58),
            "0xno": make_snapshot(token_id="0xno", best_bid=0.42, best_ask=0.29),
        }
        detector_crossed = ArbitrageDetector(make_test_config(), MockOrderBookAnalyzer(crossed))
        assert detector_crossed.verify_opportunity_with_depth(opp, 10.0) is None

