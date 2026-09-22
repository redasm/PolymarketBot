"""scripts/shadow_typesafe_baselines.py 单元测试（无网络）."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections import Counter

import pytest

from polymarket_arb.models import MarketInfo, TokenInfo
from polymarket_arb.typesafe_provider import (
    ChoiceAnswer,
    NoulAnswer,
    SystemOneResponse,
    TypeSafeRequestError,
)
from scripts import shadow_typesafe_baselines as shadow

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _market(
    cid: str,
    question: str,
    *,
    end_date: str = "2027-01-30T12:00:00Z",
    yes_price: float = 0.62,
    raw_extra: dict | None = None,
    outcomes: tuple[str, str] = ("Yes", "No"),
    active: bool = True,
    closed: bool = False,
) -> MarketInfo:
    raw = {"description": f"Resolves YES if {question}", "bestBid": 0.60, "bestAsk": 0.64}
    if raw_extra:
        raw.update(raw_extra)
    return MarketInfo(
        condition_id=cid,
        question=question,
        slug=cid,
        tokens=[
            TokenInfo(token_id=f"{cid}-y", outcome=outcomes[0], price=yes_price),
            TokenInfo(token_id=f"{cid}-n", outcome=outcomes[1], price=round(1 - yes_price, 2)),
        ],
        active=active,
        closed=closed,
        liquidity=5000.0,
        volume_24h=100.0,
        event_id="ev",
        event_title="Event",
        end_date=end_date,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# 启发式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Will Bitcoin be above $100,000 on June 30?", True),
        ("Will the US CPI print above 3.2% for August?", True),
        ("Will the Lakers win by more than 5 points?", True),
        ("Will the highest temperature in NYC exceed 90°F on July 4?", True),
        ("Will BTC updown 15m close up?", True),
        ("Will Elon Musk post 180-199 tweets from September 11 to September 18, 2026?", True),
        ("Will Elon Musk post <40 tweets from September 17 to September 19, 2026?", True),
        ("Will United Russia win fewer than 280 seats in the next Russian State Duma election?", True),
        ("Bitcoin all time high by September 30, 2026?", True),
        ("Will Trump reach a deal with Iran by June 30?", False),
        ("Will there be a ceasefire in Gaza before September 30, 2026?", False),
        ("Will the Warriors win Game 5?", False),
        ("Will OpenAI release GPT-6 by December 31?", False),
        ("Will FC Bayern München win on 2026-09-18?", False),
        ("Will Anthropic have the best AI model at the end of September 2026?", False),
        ("Will there be no change in Fed interest rates after the October 2026 meeting?", False),
        ("", False),
    ],
)
def test_is_numeric_market(question, expected):
    assert shadow.is_numeric_market(question) is expected


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Will there be a ceasefire in Gaza?", "geopolitics"),
        ("Will the Iranian regime fall by September 30?", "geopolitics"),
        ("Will Ethereum ETF be approved?", "crypto"),
        ("Will Real Madrid win the Champions League?", "sports"),
        ("Will FC Bayern München win on 2026-09-18?", "sports"),
        ("Whether Ethan wins the election?", "politics"),
        ("Will CDU win the most seats in the 2026 Berlin state elections?", "politics"),
        ("Will the Fed cut rates in September?", "macro"),
        ("Will it rain in Paris tomorrow?", "other"),
    ],
)
def test_guess_category(question, expected):
    assert shadow.guess_category(question) == expected


def test_extract_tags_from_market_and_event():
    raw = {"tags": [{"label": "Politics"}, "Elections"], "events": [{"tags": [{"slug": "us-politics"}, {"label": "Politics"}]}]}
    assert shadow.extract_tags(raw) == ["Politics", "Elections", "us-politics"]
    assert shadow.extract_tags(None) == []


def test_days_to_resolution_handles_z_and_offsets():
    assert shadow.days_to_resolution("2027-01-16T08:00:00Z", now=NOW) == pytest.approx(1.0, abs=1e-3)
    assert shadow.days_to_resolution("2027-01-16T08:00:00+00:00", now=NOW) == pytest.approx(1.0, abs=1e-3)
    assert shadow.days_to_resolution("garbage", now=NOW) is None
    assert shadow.days_to_resolution("", now=NOW) is None


def test_market_prices_prefers_bid_ask_mid_then_last():
    m = _market("a", "q", yes_price=0.7)
    assert shadow.market_prices(m)["market_mid"] == pytest.approx(0.62)
    m2 = _market("b", "q", yes_price=0.7, raw_extra={"bestBid": None, "bestAsk": None})
    assert shadow.market_prices(m2)["market_mid"] == pytest.approx(0.7)
    m3 = _market("c", "q", yes_price=0.7, raw_extra={"bestBid": 0.9, "bestAsk": 0.1})  # crossed → 回落到 last
    assert shadow.market_prices(m3)["market_mid"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 候选筛选
# ---------------------------------------------------------------------------


def test_select_candidates_filters_and_counts_skips():
    markets = [
        _market("ok1", "Will there be a ceasefire in Gaza?"),
        _market("ok1", "duplicate"),
        _market("num", "Will Bitcoin be above $100k?"),
        _market("closed", "Will X happen?", closed=True),
        _market("multi", "Who wins?", outcomes=("Alice", "Bob")),
        _market("nodate", "Will Y happen?", end_date=""),
        _market("soon", "Will Z happen?", end_date="2027-01-15T09:00:00Z"),
        _market("late", "Will W happen?", end_date="2027-12-30T12:00:00Z"),
        _market("ok2", "Will the Fed cut rates in September?"),
        _market("ok3", "Will Real Madrid win the Champions League?"),
    ]
    candidates, skips = shadow.select_candidates(markets, now=NOW, limit=2, min_days=0.25, max_days=45.0)
    assert [c["condition_id"] for c in candidates] == ["ok1", "ok2"]
    assert skips["duplicate"] == 1
    assert skips["numeric"] == 1
    assert skips["inactive"] == 1
    assert skips["not_binary_yes_no"] == 1
    assert skips["no_end_date"] == 1
    assert skips["resolves_too_soon"] == 1
    assert skips["resolves_too_late"] == 1
    assert skips["limit_reached"] == 1
    first = candidates[0]
    assert first["category"] == "geopolitics"
    assert first["market_mid"] == pytest.approx(0.62)
    assert first["days_to_resolution"] == pytest.approx(15.167, abs=0.01)
    assert first["description"].startswith("Resolves YES if")


# ---------------------------------------------------------------------------
# state / questions / row
# ---------------------------------------------------------------------------


def _candidate(**overrides) -> dict:
    base = {
        "market": None,
        "condition_id": "cid",
        "slug": "slug",
        "event_id": "ev",
        "event_title": "Event title",
        "question": "Will there be a ceasefire?",
        "description": "Resolves YES if a ceasefire is announced.",
        "category": "geopolitics",
        "tags": ["Politics"],
        "days_to_resolution": 12.34,
        "resolution_at": "2027-01-30T12:00:00Z",
        "liquidity": 5000.0,
        "volume_24h": 100.0,
        "market_yes_price": 0.62,
        "best_bid": 0.60,
        "best_ask": 0.64,
        "market_mid": 0.62,
    }
    base.update(overrides)
    return base


def test_build_state_excludes_market_price_and_includes_news():
    state = shadow.build_state(_candidate(), [])
    dumped = json.dumps(state)
    assert state["market_question"] == "Will there be a ceasefire?"
    assert state["resolution_criteria"].startswith("Resolves YES")
    assert state["days_to_resolution"] == 12.3
    assert "0.62" not in dumped
    assert "price" not in dumped.lower()
    assert "recent_news" not in state

    news = [{"summary": "Talks resume", "source": "gn", "published_at": "Mon"}] * 7
    with_news = shadow.build_state(_candidate(), news)
    assert len(with_news["recent_news"]) == 5
    assert with_news["recent_news"][0]["headline"] == "Talks resume"


def test_build_questions_shape():
    q = shadow.build_questions(has_news=False)
    assert set(q) == {"resolves_yes", "ambiguous_resolution"}
    assert q["resolves_yes"].to_dict()["type"] == "choice"
    assert set(q["resolves_yes"].to_dict()["criteria"]) == {"yes", "no"}
    q2 = shadow.build_questions(has_news=True)
    assert "news_relevant" in q2


def _response(p_yes: float = 0.31, *, with_news: bool = False) -> SystemOneResponse:
    answers = {
        "resolves_yes": ChoiceAnswer(choice="no", probabilities={"yes": p_yes, "no": 1 - p_yes}, confidence=0.55),
        "ambiguous_resolution": NoulAnswer(noul=0.2),
    }
    if with_news:
        answers["news_relevant"] = NoulAnswer(noul=0.7)
    return SystemOneResponse(model="jev-1.13.0", answers=answers, input_tokens=400, output_tokens=10, latency_ms=95.0, request_id="r1", attempts=1)


def test_build_row_success_and_error():
    row = shadow.build_row(_candidate(), _response(with_news=True), run_id="run", round_id=3, ts=NOW, news_count=2)
    assert row["kind"] == "typesafe_shadow"
    assert row["jev_p_yes"] == pytest.approx(0.31)
    assert row["jev_choice"] == "no"
    assert row["jev_confidence"] == pytest.approx(0.55)
    assert row["jev_ambiguous"] == pytest.approx(0.2)
    assert row["jev_news_relevant"] == pytest.approx(0.7)
    assert row["market_mid"] == pytest.approx(0.62)
    assert row["jev_model"] == "jev-1.13.0"
    assert row["round_id"] == 3 and row["run_id"] == "run"
    assert "error" not in row

    err = shadow.build_row(_candidate(), None, run_id="run", round_id=3, ts=NOW, news_count=0, error="HTTP 429")
    assert err["jev_p_yes"] is None
    assert err["error"] == "HTTP 429"
    assert err["market_mid"] == pytest.approx(0.62)


# ---------------------------------------------------------------------------
# score_candidates（stub client）
# ---------------------------------------------------------------------------


async def _noop_aclose() -> None:
    return None


class _StubClient:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[dict, dict, str]] = []
        self._fail_for = fail_for or set()

    async def system_one(self, state, questions, *, tag=""):
        self.calls.append((state, questions, tag))
        if tag in self._fail_for:
            raise TypeSafeRequestError("HTTP 422: nope", status=422)
        return _response(0.8)


def test_score_candidates_writes_rows_and_tolerates_failures():
    client = _StubClient(fail_for={"c2"})
    candidates = [_candidate(condition_id="c1"), _candidate(condition_id="c2"), _candidate(condition_id="c3")]
    rows = asyncio.run(
        shadow.score_candidates(client, candidates, run_id="run", round_id=1, feeds=[], concurrency=2, news_per_market=2)
    )
    assert [r["condition_id"] for r in rows] == ["c1", "c2", "c3"]
    assert rows[0]["jev_p_yes"] == pytest.approx(0.8)
    assert rows[1]["jev_p_yes"] is None and "422" in rows[1]["error"]
    assert rows[2]["jev_p_yes"] == pytest.approx(0.8)
    assert {tag for _, _, tag in client.calls} == {"c1", "c2", "c3"}
    state, questions, _ = client.calls[0]
    assert "market_mid" not in state and "recent_news" not in state
    assert "news_relevant" not in questions


# ---------------------------------------------------------------------------
# replay（历史回放）
# ---------------------------------------------------------------------------

DAY_MS = 86_400_000


def test_pick_strata_targets_dedup_and_skips_post_settlement():
    end = 1_800_000_000_000
    ideal = [end - 30 * DAY_MS, end - 7 * DAY_MS, end - 1 * DAY_MS]
    assert shadow.pick_strata(ideal, end) == [("T-30", ideal[0]), ("T-7", ideal[1]), ("T-1", ideal[2])]

    # 只有一条可用快照：三个目标都命中它，去重后保留一次，标签取提前量最小的目标
    single = end - 5 * DAY_MS
    assert shadow.pick_strata([single], end) == [("T-1", single)]

    # 结算时刻之后的 tick 不是预测输入，必须剔除
    assert shadow.pick_strata([end + DAY_MS, end - 2 * DAY_MS], end) == [("T-1", end - 2 * DAY_MS)]
    assert shadow.pick_strata([end, end + 1], end) == []
    assert shadow.pick_strata([], end) == []

    # 自定义目标
    assert shadow.pick_strata([end - 3 * DAY_MS], end, (3.0,)) == [("T-3", end - 3 * DAY_MS)]


def _snapshot(**overrides) -> dict:
    base = {
        "condition_id": "0xdead",
        "slug": "will-x-happen",
        "event_id": "42",
        "event_title": "Event title",
        "question": "Will X happen?",
        "description": "Resolves YES if X happens.",
        "category": "politics",
        "source": "tick",
        "stratum": "T-7",
        "stratum_target_days": 7.0,
        "stratum_gap_days": 0.5,
        "snapshot_ts_ms": 1_780_000_000_000,
        "days_to_resolution": 6.5,
        "best_bid": 0.30,
        "best_ask": 0.34,
        "market_mid": 0.32,
        "mid_source": "two_sided",
        "market_yes_price": 0.32,
        "end_date": "2026-06-21T14:00:00Z",
        "outcome": 1,
    }
    base.update(overrides)
    return base


def test_build_replay_candidate_matches_candidate_contract():
    candidate = shadow.build_replay_candidate(_snapshot())
    # 契约必须与 select_candidates 的输出完全同构，否则 build_row 会 KeyError
    assert set(candidate) == set(_candidate())
    assert candidate["market"] is None
    assert candidate["resolution_at"] == "2026-06-21T14:00:00Z"
    assert candidate["market_mid"] == pytest.approx(0.32)
    # 历史流动性不在 tick 行里，不拿今天的 Gamma 值冒充
    assert candidate["liquidity"] is None and candidate["volume_24h"] is None

    with_as_of = shadow.build_replay_candidate(_snapshot(), as_of_date="2026-06-15")
    assert with_as_of["as_of_date"] == "2026-06-15"


def test_snapshot_as_of_date():
    assert shadow.snapshot_as_of_date({"snapshot_ts_ms": 1_780_000_000_000}) == "2026-05-28"
    assert shadow.snapshot_as_of_date({}) == ""
    assert shadow.snapshot_as_of_date({"snapshot_ts_ms": "junk"}) == ""


def test_as_of_date_is_opt_in_and_keeps_price_out_of_state():
    plain_state = shadow.build_state(_candidate(), [])
    plain_questions = shadow.build_questions(has_news=False)
    as_of_state = shadow.build_state(_candidate(), [], as_of_date="2026-06-15")
    as_of_questions = shadow.build_questions(has_news=False, as_of_date="2026-06-15")

    # 默认路径（前向影子）逐字不变
    assert "as_of_date" not in plain_state
    assert shadow.build_state(_candidate(), [], as_of_date=None) == plain_state
    assert (
        shadow.build_questions(has_news=False, as_of_date=None)["resolves_yes"].to_dict()
        == plain_questions["resolves_yes"].to_dict()
    )

    assert as_of_state["as_of_date"] == "2026-06-15"
    assert {k: v for k, v in as_of_state.items() if k != "as_of_date"} == plain_state
    instructions = as_of_questions["resolves_yes"].to_dict()["instructions"]
    assert "2026-06-15" in instructions and "afterwards" in instructions
    assert set(as_of_questions) == set(plain_questions)
    # 两个变体都不许泄漏盘口价
    for state in (plain_state, as_of_state):
        assert "market_mid" not in state
        assert "0.62" not in json.dumps(state)


def test_score_candidates_passes_candidate_as_of_date_through():
    client = _StubClient()
    candidates = [
        _candidate(condition_id="c1"),
        {**_candidate(condition_id="c2"), "as_of_date": "2026-06-15"},
    ]
    asyncio.run(
        shadow.score_candidates(client, candidates, run_id="run", round_id=1, feeds=[], concurrency=2, news_per_market=0)
    )
    by_tag = {tag: (state, questions) for state, questions, tag in client.calls}
    assert "as_of_date" not in by_tag["c1"][0]
    assert by_tag["c2"][0]["as_of_date"] == "2026-06-15"
    assert "2026-06-15" in by_tag["c2"][1]["resolves_yes"].to_dict()["instructions"]
    assert "2026-06-15" not in by_tag["c1"][1]["resolves_yes"].to_dict()["instructions"]


def test_run_replay_writes_both_variants_with_labels(tmp_path, monkeypatch, capsys):
    snapshots = tmp_path / "snapshots.jsonl"
    shadow.append_ndjson(
        snapshots,
        [
            _snapshot(condition_id="c1", outcome=1),
            _snapshot(condition_id="c2", outcome=0, source="hf"),
            _snapshot(condition_id="c3", outcome=None),  # 未结算，必须被过滤
        ],
    )
    output = tmp_path / "replay.ndjson"
    client = _StubClient()
    client.stats = types.SimpleNamespace(to_dict=lambda: {"requests": 4})
    client.aclose = _noop_aclose

    monkeypatch.setattr(shadow.ArbConfig, "from_env", classmethod(lambda cls, *a, **k: types.SimpleNamespace()))
    monkeypatch.setattr(shadow.TypeSafeConfig, "from_env", classmethod(lambda cls: types.SimpleNamespace(model="jev-1.13.0")))
    monkeypatch.setattr(shadow, "TypeSafeJevClient", lambda config, **kwargs: client)

    args = types.SimpleNamespace(
        dotenv_path=None,
        snapshots=str(snapshots),
        output=str(output),
        status_output=str(tmp_path / "status.json"),
        requests_telemetry=str(tmp_path / "requests.ndjson"),
        variant="both",
        source="",
        limit=0,
        concurrency=2,
    )
    assert asyncio.run(shadow.run_replay(args)) == 0

    rows = shadow.read_ndjson(output)
    assert len(rows) == 4  # 2 个已结算快照 × 2 个变体
    assert {r["condition_id"] for r in rows} == {"c1", "c2"}
    assert Counter(r["variant"] for r in rows) == {"plain": 2, "as_of": 2}
    assert all(r["kind"] == "typesafe_replay" for r in rows)
    assert {r["outcome"] for r in rows} == {0, 1}
    assert all(r["mid_source"] == "two_sided" for r in rows)
    assert all(r["stratum"] == "T-7" for r in rows)
    plain = [r for r in rows if r["variant"] == "plain"]
    as_of = [r for r in rows if r["variant"] == "as_of"]
    assert all(r["as_of_date"] is None for r in plain)
    assert all(r["as_of_date"] == "2026-05-28" for r in as_of)

    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["rows_written"] == 4 and status["errors"] == 0
    assert status["news"] is False  # 回放恒定无新闻，不能与带新闻的前向影子混比
    capsys.readouterr()


def test_run_replay_missing_snapshots_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow.ArbConfig, "from_env", classmethod(lambda cls, *a, **k: types.SimpleNamespace()))
    args = types.SimpleNamespace(
        dotenv_path=None,
        snapshots=str(tmp_path / "nope.jsonl"),
        output=str(tmp_path / "out.ndjson"),
        status_output=str(tmp_path / "status.json"),
        requests_telemetry=str(tmp_path / "req.ndjson"),
        variant="plain",
        source="",
        limit=0,
        concurrency=1,
    )
    assert asyncio.run(shadow.run_replay(args)) == 2


def test_parser_wires_replay_subcommand():
    args = shadow.build_parser().parse_args(["replay", "--variant", "as_of", "--limit", "3"])
    assert args.kind == "replay" and args.variant == "as_of" and args.limit == 3
    assert args.snapshots == shadow.DEFAULT_REPLAY_SNAPSHOTS
    # 回放产物不落在 data/telemetry（那里有 DataJanitor 的 14 天淘汰）
    assert "telemetry" not in args.output


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------


def _raw_market(*, yes_price: str, closed: bool, uma: str = "", winner_yes: bool | None = None) -> dict:
    tokens = [
        {"token_id": "y", "outcome": "Yes", "price": yes_price, "winner": winner_yes},
        {"token_id": "n", "outcome": "No", "price": str(round(1 - float(yes_price), 2)), "winner": (not winner_yes) if winner_yes is not None else None},
    ]
    return {"condition_id": "0xabc", "question": "q", "tokens": tokens, "closed": closed, "active": not closed, "umaResolutionStatus": uma}


def test_resolve_outcome_variants():
    assert shadow.resolve_outcome(_raw_market(yes_price="1", closed=True, uma="resolved")) == (1, "resolved")
    assert shadow.resolve_outcome(_raw_market(yes_price="0", closed=True)) == (0, "resolved")
    assert shadow.resolve_outcome(_raw_market(yes_price="0.5", closed=True)) == (None, "closed_unresolved")
    assert shadow.resolve_outcome(_raw_market(yes_price="0.5", closed=False)) == (None, "open")
    assert shadow.resolve_outcome(_raw_market(yes_price="0.5", closed=False, winner_yes=True)) == (1, "resolved")
    assert shadow.resolve_outcome(_raw_market(yes_price="0.5", closed=False, winner_yes=False)) == (0, "resolved")
    assert shadow.resolve_outcome({"tokens": []}) == (None, "unparseable")


def test_fetch_market_raw_retries_with_closed_true(monkeypatch):
    """Gamma 默认过滤掉已结算市场，必须带 closed=true 再查一次（否则 backfill 永远 fetch_failed）."""
    calls: list[dict] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        if "closed" not in (params or {}):
            return _Resp([])  # 默认查询：已结算市场返回空数组
        return _Resp([{"condition_id": "0xabc", "closed": True}])

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_fake_get))
    row = shadow.fetch_market_raw("https://gamma.test", "0xabc")
    assert row == {"condition_id": "0xabc", "closed": True}
    assert len(calls) == 2
    assert calls[0] == {"condition_ids": "0xabc", "limit": 1}
    assert calls[1]["closed"] == "true"


def test_fetch_market_raw_returns_first_hit_without_second_call(monkeypatch):
    calls: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"condition_id": "0xopen", "closed": False}]

    def _fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        return _Resp()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_fake_get))
    assert shadow.fetch_market_raw("https://gamma.test", "0xopen")["condition_id"] == "0xopen"
    assert len(calls) == 1


def test_run_backfill_skips_settled_and_writes_new(tmp_path, monkeypatch, capsys):
    shadow_path = tmp_path / "shadow.ndjson"
    settle_path = tmp_path / "settle.ndjson"
    shadow.append_ndjson(shadow_path, [{"condition_id": "done"}, {"condition_id": "new1"}, {"condition_id": "new1"}, {"condition_id": "open1"}])
    shadow.append_ndjson(settle_path, [{"condition_id": "done", "outcome": 1}])

    lookups: list[str] = []

    def _fake_fetch(gamma_host, cid, *, timeout=15.0):
        lookups.append(cid)
        if cid == "new1":
            return _raw_market(yes_price="0", closed=True, uma="resolved")
        return _raw_market(yes_price="0.4", closed=False)

    monkeypatch.setattr(shadow, "fetch_market_raw", _fake_fetch)
    monkeypatch.setattr(shadow.ArbConfig, "from_env", classmethod(lambda cls, *a, **k: types.SimpleNamespace(gamma_host="https://gamma.test")))

    args = types.SimpleNamespace(
        dotenv_path=None,
        input=str(shadow_path),
        settlements_output=str(settle_path),
        lookup_interval_sec=0.0,
        max_lookups=0,
        record_open=False,
    )
    assert shadow.run_backfill(args) == 0
    assert lookups == ["new1", "open1"]
    rows = shadow.read_ndjson(settle_path)
    assert [r["condition_id"] for r in rows] == ["done", "new1"]
    assert rows[1]["outcome"] == 0 and rows[1]["status"] == "resolved"
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["pending"] == 2 and summary["written"] == 1
    assert summary["status_counts"] == {"resolved": 1, "open": 1}
