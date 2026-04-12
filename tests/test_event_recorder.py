"""Event recorder tests."""

from __future__ import annotations

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
