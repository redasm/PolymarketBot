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
