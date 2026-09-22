"""跨平台配对的实体一致性否决.

T1 的配对是手写的。最危险的不是配漏，而是配错：阈值 / 日期 / 方向不同
的两个市场，`poly_yes + kalshi_no < 1` 看起来仍像无风险套利，实际两条腿
可能同时输。这些用例锁住否决层能抓到哪些、放过哪些。
"""

from __future__ import annotations

import pytest

from polymarket_arb.strategies.cross_platform import (
    CrossPlatformPair,
    CrossPlatformScanner,
)
from polymarket_arb.strategies.market_entity_match import (
    extract_entities,
    verify_pair_match,
)


# --------- 实体抽取 ----------


def test_extracts_money_thresholds_with_scale():
    ents = extract_entities("Will BTC be above $100k by year end?")
    assert "$100000" in ents.thresholds


def test_money_thresholds_normalize_commas():
    a = extract_entities("BTC above $100,000")
    b = extract_entities("BTC above $100k")
    assert a.thresholds & b.thresholds


def test_extracts_percent_and_bps():
    ents = extract_entities("Will CPI come in above 3.5% and rates cut by 25 bps?")
    assert "3.5%" in ents.thresholds
    assert "25bp" in ents.thresholds


def test_extracts_dates_in_multiple_formats():
    for text in ("Dec 31, 2026", "31 December 2026", "2026-12-31"):
        assert "12-31" in extract_entities(text).dates


def test_extracts_year():
    assert "2026" in extract_entities("Who wins the 2026 election?").years


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Will BTC go above $100k", "above"),
        ("Will BTC fall below $100k", "below"),
        ("Will BTC be at least $100k", "above"),
        ("Will BTC trade between above $90k but below $110k", ""),
        ("Who wins the election", ""),
    ],
)
def test_extracts_direction(text, expected):
    assert extract_entities(text).direction == expected


# --------- 否决判定 ----------


def test_same_event_passes_despite_different_wording():
    v = verify_pair_match(
        "Will BTC be above $100k on Dec 31, 2026?",
        "BTC >= $100,000 December 31 2026",
    )
    assert v.ok is True
    assert v.mismatches == ()


def test_different_date_is_vetoed():
    v = verify_pair_match(
        "Will BTC be above $100k on Dec 31, 2026?",
        "Will BTC be above $100k on Jan 31, 2026?",
    )
    assert v.ok is False
    assert "dates_mismatch" in v.mismatches


def test_different_threshold_is_vetoed():
    v = verify_pair_match("Will BTC be above $100k?", "Will BTC be above $120k?")
    assert "thresholds_mismatch" in v.mismatches


def test_opposite_direction_is_vetoed():
    """实体完全相同、含义正好相反 —— 最隐蔽的错配."""
    v = verify_pair_match("Will BTC be above $100k?", "Will BTC be below $100k?")
    assert v.ok is False
    assert "direction_mismatch" in v.mismatches


def test_different_year_is_vetoed():
    v = verify_pair_match(
        "Who wins the 2026 election?", "Who wins the 2028 election?"
    )
    assert "years_mismatch" in v.mismatches


def test_missing_text_never_vetoes():
    """缺信息不构成矛盾，宁可放过也不静默关掉用户手写的配对."""
    assert verify_pair_match("", "Will BTC be above $100k?").ok is True
    assert verify_pair_match("Will BTC be above $100k?", "   ").ok is True


def test_one_sided_entity_is_not_a_mismatch():
    """一边提到年份、另一边没提，不算矛盾."""
    v = verify_pair_match("Will BTC be above $100k in 2026?", "Will BTC be above $100k?")
    assert v.ok is True


def test_token_overlap_gate_is_opt_in():
    left, right = "Will the Lakers win the title?", "Lakers championship winner"
    assert verify_pair_match(left, right).ok is True
    strict = verify_pair_match(left, right, min_token_overlap=0.95)
    assert strict.ok is False
    assert "low_token_overlap" in strict.mismatches


def test_verification_serializes_for_telemetry():
    payload = verify_pair_match("BTC above $100k", "BTC below $100k").to_dict()
    assert payload["ok"] is False
    assert payload["left"]["direction"] == "above"
    assert payload["right"]["direction"] == "below"


# --------- 接进 scanner ----------


class _StubKalshi:
    def __init__(self, market=None, yes=0.40, no=0.55):
        self._market = market if market is not None else {"title": ""}
        self._prices = (yes, no)

    def get_market(self, ticker):
        return self._market

    def extract_prices(self, market):
        return self._prices


class _StubBook:
    def get_snapshot(self, token_id):
        from polymarket_arb.models import OrderBookSnapshot

        return OrderBookSnapshot(token_id=token_id, best_bid=0.40, best_ask=0.42)


def _pair(poly_q="Will BTC be above $100k on Dec 31, 2026?", kalshi_title=""):
    return CrossPlatformPair(
        pair_id="p1",
        event_description="btc 100k",
        polymarket_condition_id="c1",
        polymarket_token_id_yes="tok",
        polymarket_slug="btc-100k",
        kalshi_ticker="KXBTC",
        kalshi_event_ticker="KXBTC-EVENT",
        polymarket_question=poly_q,
        kalshi_title=kalshi_title,
    )


def _scanner(kalshi, **kwargs):
    scanner = CrossPlatformScanner(kalshi, _StubBook(), **kwargs)
    return scanner


def test_scanner_vetoes_mismatched_pair():
    kalshi = _StubKalshi({"title": "Will BTC be above $100k on Jan 31, 2026?"})
    scanner = _scanner(kalshi)
    scanner.add_pair(_pair())
    assert scanner.scan() == []
    assert scanner.last_veto_summary["p1"] == ["dates_mismatch"]


def test_scanner_keeps_consistent_pair():
    kalshi = _StubKalshi({"title": "BTC >= $100,000 December 31 2026"})
    scanner = _scanner(kalshi)
    scanner.add_pair(_pair())
    scanner.scan()
    assert scanner.last_veto_summary == {}


def test_scanner_veto_can_be_disabled():
    kalshi = _StubKalshi({"title": "Will BTC be above $100k on Jan 31, 2026?"})
    scanner = _scanner(kalshi, entity_veto_enabled=False)
    scanner.add_pair(_pair())
    scanner.scan()
    assert scanner.last_veto_summary == {}


def test_scanner_backfills_kalshi_title_from_api():
    """既有配置没写 kalshi_title 也能生效，无需用户重写配对表."""
    kalshi = _StubKalshi({"title": "BTC above $100k", "subtitle": "Dec 31 2026"})
    scanner = _scanner(kalshi)
    pair = _pair()
    scanner.add_pair(pair)
    scanner.scan()
    assert pair.kalshi_title == "BTC above $100k Dec 31 2026"


def test_scanner_without_any_text_is_unaffected():
    kalshi = _StubKalshi({})
    scanner = _scanner(kalshi)
    scanner.add_pair(_pair(poly_q="", kalshi_title=""))
    scanner.scan()
    assert scanner.last_veto_summary == {}


def test_load_pairs_from_config_reads_question_fields():
    scanner = _scanner(_StubKalshi())
    scanner.load_pairs_from_config(
        [
            {
                "pair_id": "p1",
                "poly_question": "Will BTC be above $100k?",
                "kalshi_title": "BTC above $100k",
                "kalshi_ticker": "KXBTC",
            }
        ]
    )
    loaded = scanner._pairs[0]
    assert loaded.polymarket_question == "Will BTC be above $100k?"
    assert loaded.kalshi_title == "BTC above $100k"


def test_load_pairs_falls_back_to_description():
    scanner = _scanner(_StubKalshi())
    scanner.load_pairs_from_config(
        [{"pair_id": "p1", "description": "BTC above $100k", "kalshi_ticker": "K"}]
    )
    assert scanner._pairs[0].polymarket_question == "BTC above $100k"
