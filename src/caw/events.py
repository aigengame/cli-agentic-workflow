"""The append-only Event log: the machine-readable trace of one Run."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _last_seq(path: Path) -> int:
    """The highest ``seq`` already recorded in an Events file, or 0 if none.

    A fresh Run starts from 0; a resume reads the prior maximum so its first
    appended Event is ``last + 1`` and the trace stays strictly increasing. A
    missing or empty file (a fresh Run) has no Events, hence 0.

    A hard kill can leave a half-written trailing line (the append is not atomic),
    so a line that does not parse as a complete Event record is skipped rather than
    crashing the resume that is meant to recover from exactly such an interruption;
    the last COMPLETE record's ``seq`` is authoritative.
    """
    if not path.exists():
        return 0
    last = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            last = int(json.loads(line)["seq"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return last


class EventLog:
    """Appends Events for one Run to its events.jsonl file.

    The sequence numbers are strictly increasing within the file, which is the
    append-only trace's ordering invariant. A resume reopens an EXISTING log and
    continues that sequence past the last recorded Event (#6), so the resumed
    Events extend the same monotonic trace rather than restarting at 1 and
    colliding with the prior run's numbers.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._seq = _last_seq(path)
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
