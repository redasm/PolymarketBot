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
