"""The Event vocabulary seam: EventLog owns the typed event-type vocabulary (#30).

An Event's ``type`` is drawn from a single owned vocabulary, so an unrecognized
type is rejected at runtime — and, via the ``EventType`` Literal, at type-check
time — rather than silently writing an unknown record into the machine-readable
trace.
"""

from pathlib import Path

import pytest

from caw.events import EventLog


def test_append_rejects_an_unknown_event_type(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl", run_id="r1")
    with pytest.raises(ValueError, match="unknown event type"):
        log.append("bogus_event", {})  # type: ignore[arg-type]
