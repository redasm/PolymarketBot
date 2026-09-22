"""Tests for the delay-injection look-ahead gate."""

from __future__ import annotations

from research.backtest.gates.latency_gate import (
    PositionRow,
    run_gate,
    window_minutes,
)


def test_window_minutes_parses_ranges():
    assert window_minutes("Bitcoin Up or Down - June 20, 7:45AM-8:00AM ET") == 15
    assert window_minutes("Bitcoin Up or Down - June 20, 2:55AM-3:00AM ET") == 5
    # crossing midnight
    assert window_minutes("X - 11:55PM-12:10AM ET") == 15
    assert window_minutes("no time range here") is None
    assert window_minutes(None) is None


def _series(token, ticks):
    """ticks: list of (ts_ms, bid, ask). bids_top3 unused by gate PnL path."""
    return {token: sorted([(t, b, a, [(b, 100.0)]) for t, b, a in ticks], key=lambda x: x[0])}


def test_clean_strategy_passes():
    # A position whose edge does NOT depend on sub-second moves: book is flat
    # around both decision instants, so delayed re-pricing keeps the PnL.
    tok = "T"
    series = _series(
        tok,
        [
            (1000, 0.40, 0.41),  # entry decision instant
            (1500, 0.40, 0.41),  # +0.5s, unchanged
            (2100, 0.40, 0.41),  # just after +1s, unchanged
            (5000, 0.49, 0.50),  # exit decision instant
            (5500, 0.49, 0.50),  # +0.5s, unchanged
            (6100, 0.49, 0.50),  # just after +1s, unchanged
        ],
    )
    row = PositionRow(
        token_id=tok,
        open_ts=1.0,
        close_ts=5.0,
        open_price=0.41,   # paid ask
        close_price=0.49,  # hit bid
        close_size=100.0,
        fee=0.0,
        realized_pnl=(0.49 - 0.41) * 100.0,
    )
    report = run_gate([row], series, delays=[1.0], fail_delay=1.0, min_retention=0.5, window=None)
    assert report.passed
    assert report.delays[0].retention >= 0.99


def test_lookahead_strategy_fails():
    # Edge exists only at the decision instant: the ask jumps right after entry,
    # so a 1s-delayed buy pays the higher price and the edge vanishes.
    tok = "T"
    series = _series(
        tok,
        [
            (1000, 0.40, 0.41),  # entry decision: cheap ask
            (1500, 0.48, 0.49),  # +0.5s: ask jumped — real fill is here
            (2100, 0.48, 0.49),  # just after +1s
            (5000, 0.49, 0.50),  # exit decision
            (5500, 0.49, 0.50),
            (6100, 0.49, 0.50),
        ],
    )
    row = PositionRow(
        token_id=tok,
        open_ts=1.0,
        close_ts=5.0,
        open_price=0.41,
        close_price=0.49,
        close_size=100.0,
        fee=0.0,
        realized_pnl=(0.49 - 0.41) * 100.0,  # shadow books +8.0
    )
    report = run_gate([row], series, delays=[1.0], fail_delay=1.0, min_retention=0.5, window=None)
    assert not report.passed
    # entry-only delay should account for the whole leak
    assert report.entry_only_1s is not None and report.entry_only_1s < report.baseline_pnl
    assert report.entry_ask_drift_1s is not None and report.entry_ask_drift_1s > 0


def test_rebuild_matches_shadow():
    tok = "T"
    series = _series(tok, [(1000, 0.40, 0.41), (5000, 0.49, 0.50)])
    row = PositionRow(
        token_id=tok, open_ts=1.0, close_ts=5.0,
        open_price=0.41, close_price=0.49, close_size=100.0,
        fee=0.0, realized_pnl=8.0,
    )
    report = run_gate([row], series, delays=[1.0], fail_delay=1.0, min_retention=0.5, window=None)
    assert report.rebuild_error < 1e-6
