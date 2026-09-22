"""MarketScanner parsing tests."""

import logging

import requests

from polymarket_arb.market_scanner import (
    MarketScanner,
    _PARSE_FAILURE_LOGGED,
    _normalize_text,
    _parse_event,
    _parse_market,
    _safe_parse_market,
)
from tests.conftest import make_test_config


def test_parse_market_supports_token_id_and_tokenId():
    market = _parse_market(
        {
            "conditionId": "c1",
            "question": "Will BTC go up?",
            "tokens": [
                {"tokenId": "yes-token", "outcome": "Yes", "price": "0.44"},
                {"token_id": "no-token", "outcome": "No", "price": "0.55"},
            ],
        }
    )

    assert market is not None
    assert [token.token_id for token in market.tokens] == ["yes-token", "no-token"]


def test_parse_market_falls_back_to_clob_token_ids():
    market = _parse_market(
        {
            "conditionId": "c2",
            "question": "Will ETH go up?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.41", "0.59"],
            "clobTokenIds": ["yes-id", "no-id"],
        }
    )

    assert market is not None
    assert [token.token_id for token in market.tokens] == ["yes-id", "no-id"]
    assert [token.outcome for token in market.tokens] == ["Yes", "No"]


def test_parse_market_inherits_primary_event_metadata():
    market = _parse_market(
        {
            "conditionId": "c3",
            "question": "Will it happen by Friday?",
            "slug": "happen-by-friday",
            "tokens": [
                {"tokenId": "yes-token", "outcome": "Yes", "price": "0.44"},
                {"tokenId": "no-token", "outcome": "No", "price": "0.55"},
            ],
            "events": [
                {
                    "id": "evt-1",
                    "slug": "bitcoin-halving-2026",
                    "title": "Bitcoin halving event",
                    "ticker": "BTC-HALVING-2026",
                }
            ],
        }
    )

    assert market is not None
    assert market.event_id == "evt-1"
    assert market.event_slug == "bitcoin-halving-2026"
    assert market.event_title == "Bitcoin halving event"
    assert market.event_ticker == "BTC-HALVING-2026"


def test_parse_market_tolerates_json_null_outcomes_and_prices():
    """Gamma sometimes ships outcomes / outcomePrices as the JSON string 'null'.

    Before the fix this hit `for x in None` and tripped APIResponseValidationError
    at the call site. The market is unusable (no tokens) so we expect None back,
    not an exception.
    """
    market = _parse_market(
        {
            "conditionId": "c-null",
            "question": "Will Foo happen?",
            "outcomes": "null",
            "outcomePrices": "null",
            "clobTokenIds": "null",
        }
    )

    assert market is not None
    assert market.tokens == []
    assert market.outcomes == []


def test_safe_parse_market_dedups_repeat_failures(caplog):
    _PARSE_FAILURE_LOGGED.clear()

    class _BoomDict(dict):
        # Real dict so _safe_parse_market's isinstance(dict) check passes,
        # but get() blows up the way real Gamma rows with type-coerced fields do.
        def get(self, key, default=None):
            if key in ("condition_id", "conditionId"):
                return "c-repeat"
            raise RuntimeError("synthetic parse failure")

    bad = _BoomDict()
    with caplog.at_level(logging.DEBUG, logger="polymarket_arb.market_scanner"):
        _safe_parse_market(bad, context="ctx")
        _safe_parse_market(bad, context="ctx")
        _safe_parse_market(bad, context="ctx")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "c-repeat" in r.getMessage()]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "c-repeat" in r.getMessage()]
    assert len(warnings) == 1
    assert len(debugs) == 2


def test_normalize_text_repairs_common_mojibake_and_whitespace():
    assert _normalize_text("Claude 5 released byâ¦?") == "Claude 5 released by…?"
    assert _normalize_text("  Mike   Johnson out as Speaker by...?   ") == "Mike Johnson out as Speaker by...?"


def test_parse_event_normalizes_titles_and_market_questions():
    event = _parse_event(
        {
            "id": "e1",
            "title": "Claude 5 released byâ¦?",
            "slug": "claude-5",
            "markets": [
                {
                    "conditionId": "c1",
                    "question": "Claude 5 released byâ¦?   ",
                    "tokens": [
                        {"tokenId": "yes-token", "outcome": "Yes", "price": "0.1"},
                        {"tokenId": "no-token", "outcome": "No", "price": "0.9"},
                    ],
                }
            ],
        }
    )

    assert event is not None
    assert event.title == "Claude 5 released by…?"
    assert event.markets[0].question == "Claude 5 released by…?"
    assert event.markets[0].event_id == "e1"
    assert event.markets[0].event_slug == "claude-5"
    assert event.markets[0].event_title == "Claude 5 released by…?"


def test_fetch_active_markets_ignores_invalid_response_shape(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"markets": []}

    class _Session:
        def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr("polymarket_arb.market_scanner._get_session", lambda: _Session())

    scanner = MarketScanner(make_test_config())

    assert scanner.fetch_active_markets(limit=10) == []


def test_fetch_active_events_handles_request_exception(monkeypatch):
    class _Session:
        def get(self, *args, **kwargs):
            raise requests.RequestException("boom")

    monkeypatch.setattr("polymarket_arb.market_scanner._get_session", lambda: _Session())

    scanner = MarketScanner(make_test_config())

    assert scanner.fetch_active_events(limit=10) == []


# --- fetch_updown_markets: slug-direct probe (Phase 1 fix) ---
#
# UPDOWN markets carry ~0 24h volume and are dropped by the volume-filtered
# fetch_active_markets. This probe resolves them by their deterministic slug
# `{sym}-updown-{w}m-{slot}` so the scan-pool boost has something to promote.

def _updown_event_payload(slug: str) -> list:
    """One gamma /events row shaped like a btc-updown-15m event."""
    return [{
        "id": f"evt-{slug}",
        "slug": slug,
        "title": slug,
        "active": True,
        "closed": False,
        "markets": [{
            "conditionId": f"cond-{slug}",
            "question": slug,
            "slug": slug,
            "active": True,
            "closed": False,
            "outcomes": ["Up", "Down"],
            "clobTokenIds": [f"{slug}-up", f"{slug}-down"],
            "liquidity": "23000",
            "volume24hr": "0",
        }],
    }]


def test_fetch_updown_markets_resolves_by_slug(monkeypatch):
    captured_slugs = []

    class _Resp:
        def __init__(self, slug):
            self._slug = slug

        def raise_for_status(self):
            return None

        def json(self):
            return _updown_event_payload(self._slug)

    class _Session:
        def get(self, url, params=None, **kwargs):
            slug = (params or {}).get("slug", "")
            captured_slugs.append(slug)
            return _Resp(slug)

    monkeypatch.setattr("polymarket_arb.market_scanner._get_session", lambda: _Session())

    scanner = MarketScanner(make_test_config())
    out = scanner.fetch_updown_markets(symbols=["btc", "eth"], window_minutes=[15], slots_ahead=2)

    # 2 symbols * (slots_ahead + 1) windows = 6 slug probes
    assert len(captured_slugs) == 6
    # slug convention + UTC-aligned slot
    assert all(s.startswith(("btc-updown-15m-", "eth-updown-15m-")) for s in captured_slugs)
    # parsed into MarketInfo with Up/Down tokens
    assert len(out) == 6
    sample = out[0]
    assert is_updown_outcomes(sample)


def is_updown_outcomes(market) -> bool:
    return {(t.outcome or "").lower() for t in market.tokens} == {"up", "down"}


def test_fetch_updown_markets_dedups_repeated_condition_id(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            # Always the same condition_id regardless of slug
            return [{
                "id": "evt-x",
                "slug": "btc-updown-15m-x",
                "markets": [{
                    "conditionId": "same-cond",
                    "question": "q",
                    "slug": "btc-updown-15m-x",
                    "outcomes": ["Up", "Down"],
                    "clobTokenIds": ["a", "b"],
                }],
            }]

    class _Session:
        def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr("polymarket_arb.market_scanner._get_session", lambda: _Session())

    scanner = MarketScanner(make_test_config())
    out = scanner.fetch_updown_markets(symbols=["btc"], window_minutes=[15], slots_ahead=3)
    assert len(out) == 1  # deduped by condition_id


def test_fetch_updown_markets_swallows_request_exception(monkeypatch):
    class _Session:
        def get(self, *args, **kwargs):
            raise requests.RequestException("boom")

    monkeypatch.setattr("polymarket_arb.market_scanner._get_session", lambda: _Session())

    scanner = MarketScanner(make_test_config())
    # Must not raise — additive path, failures are per-slug best-effort.
    assert scanner.fetch_updown_markets(symbols=["btc"], window_minutes=[15], slots_ahead=2) == []


def test_fetch_updown_markets_empty_inputs_noop(monkeypatch):
    def _boom():
        raise AssertionError("should not open a session for empty inputs")

    monkeypatch.setattr("polymarket_arb.market_scanner._get_session", _boom)
    scanner = MarketScanner(make_test_config())
    assert scanner.fetch_updown_markets(symbols=[], window_minutes=[15], slots_ahead=2) == []
    assert scanner.fetch_updown_markets(symbols=["btc"], window_minutes=[], slots_ahead=2) == []
