"""Regression: sub-cent tick prices must remain matchable.

Bug context: live T2 exit for token 9186316211830866 looped 21+ times with
"no orders found to match with FAK order" because the SELL limit @ 0.505
got quantized to 0.51 (legacy ROUND_HALF_UP, _PRICE_QUANT=0.01). The market
ran on a 0.001 tick so the bid sat at 0.505 — our SELL at 0.51 could never
match. The fix makes price quantization tick-aware and side-aware (BUY
rounds UP, SELL rounds DOWN). These tests pin the new behaviour and the
legacy fallback so we don't regress.
"""

from __future__ import annotations

from polymarket_arb.execution_engine import (
    _format_tick_size_for_clob,
    _quantize_clob_order_args,
    _quantize_clob_price,
    _quantize_common_clob_order_size,
)
from polymarket_arb.models import OrderSide


class TestSellRoundsDownTowardBid:
    """SELL limit must land at or below the actual bid so FAK can fill."""

    def test_sell_at_real_tick_preserves_sub_cent_price(self):
        # The actual T2 exit case: bid=0.505 on a 0.001-tick market.
        assert _quantize_clob_price(0.505, side=OrderSide.SELL, tick_size=0.001) == 0.505

    def test_sell_rounds_down_to_tick_grid(self):
        # 0.5051 on 0.005-tick → 0.505 (one tick below), not 0.510.
        assert _quantize_clob_price(0.5051, side=OrderSide.SELL, tick_size=0.005) == 0.505

    def test_sell_at_cent_tick_still_safe(self):
        # SELL with side specified now rounds down even at 0.01 tick.
        assert _quantize_clob_price(0.505, side=OrderSide.SELL, tick_size=0.01) == 0.50


class TestBuyRoundsUpTowardAsk:
    """BUY limit must land at or above the ask so FAK/FOK can fill."""

    def test_buy_at_real_tick_preserves_sub_cent_price(self):
        # The actual T2 entry case: ask=0.507 on a 0.001-tick market.
        assert _quantize_clob_price(0.507, side=OrderSide.BUY, tick_size=0.001) == 0.507

    def test_buy_rounds_up_to_tick_grid(self):
        # 0.5071 on 0.005-tick → 0.51 (one tick above), not 0.505.
        assert _quantize_clob_price(0.5071, side=OrderSide.BUY, tick_size=0.005) == 0.510

    def test_buy_at_cent_tick_rounds_up(self):
        # BUY with side specified rounds up at 0.01 tick.
        assert _quantize_clob_price(0.505, side=OrderSide.BUY, tick_size=0.01) == 0.51


class TestLegacyCallersUnaffected:
    """Callers that don't pass side/tick still see ROUND_HALF_UP at 0.01."""

    def test_no_side_no_tick_uses_round_half_up(self):
        assert _quantize_clob_price(0.505) == 0.51
        assert _quantize_clob_price(0.504) == 0.50

    def test_no_side_with_tick_uses_round_half_up(self):
        # tick passed but no side → still ROUND_HALF_UP (only side changes mode)
        assert _quantize_clob_price(0.5055, tick_size=0.001) == 0.506


class TestQuantizeOrderArgsEndToEnd:
    """`_quantize_clob_order_args` is the executor's entry point — keep the
    full pipeline (price + size legality) coherent at sub-cent ticks."""

    def test_sell_exit_at_001_tick(self):
        # The T2 exit that was failing in prod.
        price, size = _quantize_clob_order_args(
            0.505, 15, side=OrderSide.SELL, tick_size=0.001
        )
        assert price == 0.505
        assert size == 15.0

    def test_buy_entry_at_001_tick(self):
        # The T2 entry that succeeded only because BUY rounding UP was lucky
        # (matched a higher ask). The new code preserves the real ask price.
        price, size = _quantize_clob_order_args(
            0.507, 15, side=OrderSide.BUY, tick_size=0.001
        )
        assert price == 0.507
        assert size == 15.0

    def test_legacy_pair_unchanged(self):
        # No side, no tick — pre-fix behaviour, still valid for 0.01-tick markets.
        price, size = _quantize_clob_order_args(0.51, 15)
        assert price == 0.51
        assert size == 15.0


class TestSizeLegalityScalesWithTick:
    """Size legality denominator scales with tick so sub-cent prices aren't
    silently coerced to cents during the legality check."""

    def test_legacy_size_at_cent_tick(self):
        assert _quantize_common_clob_order_size([0.51], 15) == 15.0

    def test_size_at_milli_tick(self):
        # 0.507 with size 15 is legal: 0.507 * 15 = 7.605 USDC (3 decimals,
        # within 5-decimal USDC base) and the maker amount checks pass.
        assert _quantize_common_clob_order_size([0.507], 15, tick_size=0.001) == 15.0

    def test_size_at_half_cent_tick(self):
        assert _quantize_common_clob_order_size([0.505], 0.5, tick_size=0.005) == 0.5


class TestTickFormatting:
    """The string we pass to PartialCreateOrderOptions must match the CLOB's
    canonical tick strings."""

    def test_canonical_ticks(self):
        assert _format_tick_size_for_clob(0.01) == "0.01"
        assert _format_tick_size_for_clob(0.005) == "0.005"
        assert _format_tick_size_for_clob(0.001) == "0.001"
        assert _format_tick_size_for_clob(0.0001) == "0.0001"

    def test_none_falls_back_to_legacy_default(self):
        assert _format_tick_size_for_clob(None) == "0.01"

    def test_unknown_tick_falls_back_to_legacy_default(self):
        # Defensive: non-canonical tick won't cause a CLOB rejection on string
        # format alone; we play safe and emit "0.01".
        assert _format_tick_size_for_clob(0.003) == "0.01"
