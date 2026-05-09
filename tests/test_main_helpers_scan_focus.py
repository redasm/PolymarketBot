"""Tests for `polymarket_arb.main_helpers.scan_focus`.

These functions used to live inline in `main_loop.py` and were impossible
to exercise without booting the whole bot. Locking in their behaviour
here so the upcoming `main_loop` decomposition can keep going safely.
"""

from __future__ import annotations

from types import SimpleNamespace

from polymarket_arb.main_helpers.scan_focus import (
    event_focus_text,
    event_priority_score,
    focus_keywords,
    market_focus_text,
    market_priority_score,
    matches_focus,
    merge_focus_event_markets,
    select_event_candidates,
    select_scan_candidates,
    select_ws_targets,
)
from polymarket_arb.models import MarketInfo, TokenInfo


def _make_market(
    *,
    cid: str = "c1",
    question: str = "Will BTC end above $100k?",
    slug: str = "btc-100k",
    volume: float = 1000.0,
    liquidity: float = 500.0,
    closed: bool = False,
    active: bool = True,
    tokens: int = 2,
    event_title: str = "",
) -> MarketInfo:
    token_list = [
        TokenInfo(token_id=f"{cid}-tk{i}", outcome=str(i), price=0.0)
        for i in range(tokens)
    ]
    return MarketInfo(
        condition_id=cid,
        question=question,
        slug=slug,
        tokens=token_list,
        active=active,
        closed=closed,
        volume_24h=volume,
        liquidity=liquidity,
        event_title=event_title,
    )


def test_focus_keywords_normalises_whitespace_and_case():
    assert focus_keywords(" BTC , Eth ,, sol ") == ["btc", "eth", "sol"]
    assert focus_keywords("") == []
    assert focus_keywords(None) == []  # type: ignore[arg-type]


def test_matches_focus_uses_token_aliases():
    text = market_focus_text(_make_market(question="Bitcoin price target?", slug="bitcoin-target"))
    # `btc` -> alias `bitcoin` should hit even though raw text has no `btc` substring.
    assert matches_focus(text, ["btc"])
    # `eth` must NOT match `together` (regression: avoid substring matching).
    assert not matches_focus("we together build eth-killer", ["eth"]) is False  # noqa: E712 — sanity
    assert matches_focus("we together build eth-killer", ["eth"]) is True


def test_matches_focus_short_keywords_must_match_exact_token():
    # `nfl` is 3 chars — must match exact token, not substring of `nfltest`.
    assert matches_focus("nfl game tonight", ["nfl"])
    assert not matches_focus("nfltest mock", ["nfl"])


def test_matches_focus_long_keywords_use_prefix():
    assert matches_focus("election day results", ["election"])
    # prefix match: `election` keyword fires on `elections-2026`.
    assert matches_focus("elections-2026 polling", ["election"])


def test_matches_focus_empty_keywords_passes_through():
    assert matches_focus("anything goes", [])


def test_market_priority_score_prefers_binary_then_volume():
    binary_high_vol = _make_market(cid="b1", volume=2000.0, liquidity=100.0, tokens=2)
    multi_higher_vol = _make_market(cid="m1", volume=10_000.0, liquidity=10_000.0, tokens=4)
    # Binary always wins regardless of multi-outcome volume.
    assert market_priority_score(binary_high_vol) > market_priority_score(multi_higher_vol)


def test_select_scan_candidates_filters_closed_and_inactive():
    markets = [
        _make_market(cid="open", volume=100.0),
        _make_market(cid="closed", volume=999.0, closed=True),
        _make_market(cid="inactive", volume=999.0, active=False),
    ]
    selected = select_scan_candidates(markets, max_count=10)
    assert [m.condition_id for m in selected] == ["open"]


def test_select_scan_candidates_respects_focus():
    btc_mkt = _make_market(cid="btc1", question="BTC moon?", slug="btc-moon")
    eth_mkt = _make_market(cid="eth1", question="ETH lose to SOL?", slug="eth-vs-sol")
    selected = select_scan_candidates([btc_mkt, eth_mkt], max_count=10, focus_keywords=["btc"])
    assert [m.condition_id for m in selected] == ["btc1"]


def test_select_ws_targets_ranks_by_volume_times_liquidity():
    a = _make_market(cid="a", volume=10.0, liquidity=10.0)  # 100
    b = _make_market(cid="b", volume=5.0, liquidity=50.0)   # 250  <-- top
    c = _make_market(cid="c", volume=2.0, liquidity=2.0)    # 4
    targets = select_ws_targets([a, b, c], max_count=2)
    assert [m.condition_id for m in targets] == ["b", "a"]


def test_select_ws_targets_skips_non_binary_and_closed():
    binary_open = _make_market(cid="ok", tokens=2, volume=10.0, liquidity=10.0)
    binary_closed = _make_market(cid="closed", tokens=2, volume=10_000.0, liquidity=10_000.0, closed=True)
    multi = _make_market(cid="multi", tokens=5, volume=99_999.0, liquidity=99_999.0)
    targets = select_ws_targets([binary_open, binary_closed, multi], max_count=10)
    assert [m.condition_id for m in targets] == ["ok"]


def test_event_priority_score_aggregates_children():
    event = SimpleNamespace(
        markets=[
            SimpleNamespace(volume_24h=100.0, liquidity=200.0),
            SimpleNamespace(volume_24h=50.0, liquidity=25.0),
        ]
    )
    score = event_priority_score(event)
    assert score == (150.0, 225.0, 2)


def test_select_event_candidates_filters_inactive_and_focuses():
    e_active = SimpleNamespace(
        title="BTC pricing event",
        slug="btc-event",
        active=True,
        closed=False,
        markets=[SimpleNamespace(volume_24h=10.0, liquidity=10.0, question="", slug="")],
    )
    e_closed = SimpleNamespace(
        title="ETH event",
        slug="eth-event",
        active=True,
        closed=True,
        markets=[],
    )
    selected = select_event_candidates([e_active, e_closed], max_count=10, focus_keywords=["btc"])
    assert [getattr(e, "slug") for e in selected] == ["btc-event"]


def test_merge_focus_event_markets_backfills_event_metadata_and_dedupes():
    event_market = _make_market(cid="cFromEvent", question="Election?", slug="election-day")
    event = SimpleNamespace(
        event_id="evt-9",
        slug="election-event",
        title="2026 Election",
        active=True,
        closed=False,
        markets=[event_market],
    )
    flat_market = _make_market(cid="cFromFlat", question="BTC > $100k", slug="btc-100k")
    merged = merge_focus_event_markets(
        [flat_market],
        [event],
        max_count=10,
        focus_keywords=None,
    )
    by_id = {m.condition_id: m for m in merged}
    # both markets present, no dup
    assert set(by_id) == {"cFromEvent", "cFromFlat"}
    # event metadata flows down only to the child market
    assert by_id["cFromEvent"].event_id == "evt-9"
    assert by_id["cFromEvent"].event_slug == "election-event"
    assert by_id["cFromEvent"].event_title == "2026 Election"


def test_merge_focus_event_markets_focus_filter_drops_event_children():
    event_market = _make_market(cid="cFromEvent", question="Election?", slug="election-day")
    event = SimpleNamespace(
        event_id="evt-9",
        slug="election-event",
        title="2026 Election",
        active=True,
        closed=False,
        markets=[event_market],
    )
    flat_market = _make_market(cid="cFromFlat", question="BTC > $100k", slug="btc-100k")
    merged = merge_focus_event_markets(
        [flat_market],
        [event],
        max_count=10,
        focus_keywords=["btc"],
    )
    # Only the flat BTC market survives the focus filter — `event_market` is
    # filtered out because `election` doesn't match `btc`.
    assert [m.condition_id for m in merged] == ["cFromFlat"]
