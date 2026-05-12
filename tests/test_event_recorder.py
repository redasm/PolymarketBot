"""Event recorder tests."""

from __future__ import annotations

import json
from pathlib import Path

from polymarket_arb.event_recorder import EventRecorder


def test_event_recorder_writes_category_ndjson(tmp_path: Path):
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=True)

    recorder.write_event("opportunities", {"event_id": "e1", "net_edge": 0.12})
    recorder.close()

    files = list(tmp_path.glob("*.opportunities.ndjson"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert '"category":"opportunities"' in content
    assert '"event_id":"e1"' in content


def test_event_recorder_rotates_when_file_is_too_large(tmp_path: Path):
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=True, max_file_size_mb=0.00001)

    recorder.write_event("trades", {"trade_id": "t1", "payload": "x" * 256})
    recorder.write_event("trades", {"trade_id": "t2", "payload": "x" * 256})
    recorder.close()

    files = list(tmp_path.glob("*.trades*.ndjson"))
    assert len(files) >= 2


def test_event_recorder_flushes_without_close(tmp_path: Path):
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=True)

    recorder.write_event("risk_events", {"event": "startup"})

    files = list(tmp_path.glob("*.risk_events.ndjson"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0
    recorder.close()


def test_event_recorder_preserves_canonical_ts_when_payload_has_ts(tmp_path: Path):
    recorder = EventRecorder(output_dir=str(tmp_path), enabled=True)

    recorder.write_event("risk_events", {"event": "sync", "ts": 123.45, "category": "payload"})
    recorder.close()

    files = list(tmp_path.glob("*.risk_events.ndjson"))
    row = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert isinstance(row["ts"], str)
    assert row["category"] == "risk_events"
    assert row["payload_ts"] == 123.45
    assert row["payload_category"] == "payload"


def test_event_recorder_async_write_drains_on_close(tmp_path: Path):
    """Async mode must flush every queued event by the time close()
    returns — otherwise end-of-run summaries would silently lose
    data.
    """
    recorder = EventRecorder(
        output_dir=str(tmp_path),
        enabled=True,
        async_write=True,
        queue_size=1024,
    )
    for i in range(50):
        recorder.write_event("opportunities", {"i": i})
    recorder.close()

    files = list(tmp_path.glob("*.opportunities.ndjson"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 50
    assert recorder.event_count == 50
    assert recorder.dropped_events == 0


def test_event_recorder_async_write_drops_oldest_when_queue_full(tmp_path: Path):
    """Backpressure policy: when the queue can't accept new events,
    the OLDEST item is dropped so the newest (most actionable for
    live debugging) survives.
    """
    recorder = EventRecorder(
        output_dir=str(tmp_path),
        enabled=True,
        async_write=True,
        queue_size=100,
    )
    # Block the writer thread so the queue fills up. Easiest way is
    # to flood it faster than disk can absorb in a normal scenario,
    # but for determinism we artificially throttle by patching the
    # internal lock so the writer can't make progress.
    recorder._lock.acquire()
    try:
        for i in range(500):
            recorder.write_event("trades", {"i": i})
        assert recorder.dropped_events > 0
    finally:
        recorder._lock.release()
    recorder.close()
