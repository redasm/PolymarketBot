"""Tick recorder tests."""

from __future__ import annotations

from pathlib import Path

from polymarket_arb.models import MarketInfo, OrderBookLevel, OrderBookSnapshot, TokenInfo
from polymarket_arb.tick_recorder import TickRecorder


def _make_snapshot() -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="token-1",
        best_bid=0.40,
        best_ask=0.42,
        bids=[OrderBookLevel(0.40, 100), OrderBookLevel(0.39, 50)],
        asks=[OrderBookLevel(0.42, 120), OrderBookLevel(0.43, 40)],
        timestamp=1_700_000_000.0,
    )


def test_tick_recorder_writes_ndjson_record(tmp_path: Path):
    recorder = TickRecorder(output_dir=str(tmp_path), enabled=True)

    recorder.on_book_update("token-1", _make_snapshot())
    recorder.close()

    files = list(tmp_path.glob("*.ndjson"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert '"token_id":"token-1"' in content
    assert recorder.tick_count == 1


def test_tick_recorder_rotates_when_max_size_exceeded(tmp_path: Path):
    recorder = TickRecorder(output_dir=str(tmp_path), enabled=True, max_file_size_mb=0.00001)

    recorder.on_book_update("token-1", _make_snapshot())
    recorder.on_book_update("token-1", _make_snapshot())
    recorder.close()

    files = list(tmp_path.glob("*.ndjson"))
    assert len(files) >= 2


def test_tick_recorder_flushes_without_close(tmp_path: Path):
    recorder = TickRecorder(output_dir=str(tmp_path), enabled=True)

    recorder.on_book_update("token-1", _make_snapshot())

    files = list(tmp_path.glob("*.ndjson"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0
    recorder.close()


def test_tick_recorder_can_include_market_metadata(tmp_path: Path):
    recorder = TickRecorder(output_dir=str(tmp_path), enabled=True)
    recorder.register_markets(
        [
            MarketInfo(
                condition_id="cond-1",
                event_id="event-1",
                event_slug="btc-event",
                event_title="BTC event",
                question="Will BTC go up?",
                slug="btc-go-up",
                tokens=[TokenInfo("token-1", "Yes"), TokenInfo("token-2", "No")],
            )
        ]
    )

    recorder.on_book_update("token-1", _make_snapshot())
    recorder.close()

    files = list(tmp_path.glob("*.ndjson"))
    content = files[0].read_text(encoding="utf-8")
    assert '"condition_id":"cond-1"' in content
    assert '"outcome_role":"yes"' in content
