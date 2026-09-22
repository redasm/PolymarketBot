"""钱包盈利质量评分.

旧的钱包发现按 (交易笔数, 名义额) 排序 —— 挑出来的是最活跃的钱包，不是
最赚钱的。这些用例锁住新口径能把"活跃但亏钱"和"靠一票运气"的钱包区分
出来。
"""

from __future__ import annotations

import pytest

from polymarket_arb.quant_input_scanner import (
    discover_quality_wallets,
    score_wallets_from_closed_positions,
)
from polymarket_arb.strategies.wallet_quality import (
    WalletQualityThresholds,
    rank_wallets_by_quality,
    score_wallet_quality,
)


def _rows(pnls, start_ts: float = 1_700_000_000.0):
    return [
        {"realizedPnl": pnl, "timestamp": start_ts + index * 3600}
        for index, pnl in enumerate(pnls)
    ]


def _steady_winner(n: int = 24):
    """胜多败少、盈利分散、各时段都赚."""
    return _rows([120.0, -30.0, 90.0] * (n // 3))


# --------- 指标 ----------


def test_basic_metrics():
    q = score_wallet_quality("0x1", _rows([100.0, -50.0, 100.0, -50.0]))
    assert q.wins == 2 and q.losses == 2
    assert q.win_rate == pytest.approx(0.5)
    assert q.gross_profit == pytest.approx(200.0)
    assert q.gross_loss == pytest.approx(100.0)
    assert q.profit_factor == pytest.approx(2.0)
    assert q.total_pnl == pytest.approx(100.0)


def test_steady_winner_passes():
    q = score_wallet_quality("0x1", _steady_winner())
    assert q.passed is True
    assert q.reject_reasons == ()


def test_lucky_one_hit_wonder_is_rejected():
    """90% 利润来自一笔 = 运气，不是可复制的能力."""
    pnls = [5000.0] + [50.0] * 20 + [-20.0] * 4
    q = score_wallet_quality("0x1", _rows(pnls))
    assert "single_trade_concentration" in q.reject_reasons
    assert q.top_trade_share > 0.30


def test_early_winner_then_bleeding_is_rejected():
    """总 PnL 和 profit factor 都好看，但后段一路亏 —— consistency 抓住它."""
    q = score_wallet_quality("0x1", _rows([500.0] * 5 + [-20.0] * 20))
    assert q.total_pnl > 0
    assert q.profit_factor > 1.5
    assert "inconsistent_across_periods" in q.reject_reasons


def test_active_but_losing_wallet_is_rejected():
    q = score_wallet_quality("0x1", _rows([30.0, -80.0] * 15))
    assert q.total_pnl < 0
    assert "profit_factor_below_min" in q.reject_reasons
    assert "total_pnl_below_min" in q.reject_reasons


def test_small_sample_is_rejected_even_if_perfect():
    q = score_wallet_quality("0x1", _rows([400.0, 400.0, 400.0]))
    assert q.profit_factor is None  # 从没亏过，无法定义
    assert "too_few_closed_positions" in q.reject_reasons
    assert q.passed is False


def test_empty_history_is_safe():
    q = score_wallet_quality("0x1", [])
    assert q.closed_positions == 0
    assert q.win_rate == 0.0
    assert q.passed is False
    assert q.to_dict()["profit_factor"] == 0.0


def test_malformed_rows_are_skipped():
    rows = [{"realizedPnl": "abc"}, {"nope": 1}, "junk", {"realizedPnl": 100.0}]
    q = score_wallet_quality("0x1", rows)
    assert q.closed_positions == 1
    assert q.total_pnl == pytest.approx(100.0)


def test_alternate_pnl_and_timestamp_field_names():
    rows = [
        {"cashPnl": 100.0, "closedAt": 1_700_000_000_000},
        {"realized_pnl": -20.0, "ts": 1_700_000_100},
    ]
    q = score_wallet_quality("0x1", rows)
    assert q.closed_positions == 2
    assert q.total_pnl == pytest.approx(80.0)


def test_thresholds_are_configurable():
    loose = WalletQualityThresholds(
        min_closed_positions=1,
        min_win_rate=0.0,
        min_profit_factor=0.0,
        min_total_pnl_usdc=-1e9,
        max_top_trade_share=1.0,
        min_consistency=0.0,
    )
    q = score_wallet_quality("0x1", _rows([-10.0]), thresholds=loose)
    assert q.passed is True


# --------- 排序 ----------


def test_ranking_prefers_profitability_over_activity():
    busy_loser = score_wallet_quality("0xbusy", _rows([10.0, -30.0] * 50))
    calm_winner = score_wallet_quality("0xcalm", _steady_winner())
    ranked = rank_wallets_by_quality(
        [busy_loser, calm_winner], require_passed=False, max_wallets=2
    )
    assert ranked[0].wallet_address == "0xcalm"


def test_ranking_can_filter_to_passing_only():
    good = score_wallet_quality("0xgood", _steady_winner())
    bad = score_wallet_quality("0xbad", _rows([-10.0] * 30))
    assert [q.wallet_address for q in rank_wallets_by_quality([good, bad])] == ["0xgood"]


def test_never_lost_small_sample_does_not_outrank_proven_wallet():
    """3 笔全赚不该压过 200 笔稳定盈利."""
    lucky = score_wallet_quality("0xlucky", _rows([10.0, 10.0, 10.0]))
    proven = score_wallet_quality("0xproven", _steady_winner(60))
    ranked = rank_wallets_by_quality([lucky, proven], require_passed=False, max_wallets=2)
    assert ranked[0].wallet_address == "0xproven"


# --------- 与 Data API 客户端的接线 ----------


class _StubClient:
    def __init__(self, by_wallet, *, fail=()):
        self._by_wallet = by_wallet
        self._fail = set(fail)
        self.calls: list[str] = []

    def fetch_closed_positions(self, wallet, *, limit=500, offset=0):
        self.calls.append(wallet)
        if wallet in self._fail:
            raise RuntimeError("data api down")
        return self._by_wallet.get(wallet, [])


def _trade_rows(wallet, n=5):
    return [
        {
            "proxyWallet": wallet,
            "conditionId": f"c{i}",
            "outcome": "Yes",
            "side": "BUY",
            "size": 200.0,
            "price": 0.5,
        }
        for i in range(n)
    ]


def test_score_wallets_skips_failing_fetches():
    client = _StubClient({"0xa": _rows([100.0])}, fail={"0xb"})
    out = score_wallets_from_closed_positions(client, ["0xa", "0xb"])
    assert [q.wallet_address for q in out] == ["0xa"]


def test_discover_quality_wallets_ranks_by_pnl_not_activity():
    recent = _trade_rows("0xbusy", n=40) + _trade_rows("0xcalm", n=5)
    client = _StubClient(
        {
            "0xbusy": _rows([10.0, -30.0] * 50),
            "0xcalm": _steady_winner(),
        }
    )
    wallets, qualities = discover_quality_wallets(
        client, recent, min_trades=3, min_notional_usdc=0.0, max_wallets=5
    )
    assert wallets == ["0xcalm"]
    # 画像是全量的，被过滤掉的也在里面 —— 产物要能看到分布。
    assert {q.wallet_address for q in qualities} == {"0xbusy", "0xcalm"}


def test_discover_quality_wallets_observation_mode_keeps_everyone():
    recent = _trade_rows("0xbusy", n=40) + _trade_rows("0xcalm", n=5)
    client = _StubClient(
        {"0xbusy": _rows([10.0, -30.0] * 50), "0xcalm": _steady_winner()}
    )
    wallets, _ = discover_quality_wallets(
        client,
        recent,
        min_trades=3,
        min_notional_usdc=0.0,
        max_wallets=5,
        require_passed=False,
    )
    assert set(wallets) == {"0xbusy", "0xcalm"}
