"""Cross-platform scanner tests."""

from polymarket_arb.strategies.cross_platform import CrossPlatformPair, CrossPlatformScanner


def test_cross_platform_direction_b_uses_poly_no_price_directly():
    scanner = CrossPlatformScanner(None, None)  # type: ignore[arg-type]
    pair = CrossPlatformPair(
        pair_id="pair-1",
        event_description="BTC vs Kalshi",
        polymarket_condition_id="cond-1",
        polymarket_token_id_yes="yes-token",
        polymarket_slug="btc",
        kalshi_ticker="KXBTC-YES",
        kalshi_event_ticker="KXBTC",
    )

    pair.poly_yes_price = 0.72
    pair.poly_no_price = 0.35
    pair.kalshi_yes_price = 0.60
    pair.kalshi_no_price = 0.40

    opp = scanner._check_pair(pair)

    assert opp is not None
    assert opp.direction == "poly_no_kalshi_yes"
    assert opp.poly_cost == 0.35
    assert opp.total_cost == 0.95
