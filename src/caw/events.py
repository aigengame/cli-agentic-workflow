"""The append-only Event log: the machine-readable trace of one Run."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventLog:
    """Appends Events for one Run to its events.jsonl file."""

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._seq = 0
        path.touch()

    def append(self, event_type: str, data: dict[str, Any]) -> None:
        """Append one Event record as a JSON line."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self._run_id,
            "type": event_type,
            "data": data,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
