"""The append-only Event log: the machine-readable trace of one Run."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

# The Event-type vocabulary (#30): the single owned set of `type` strings an Event
# record may carry. Typed as a Literal so a typo is a static error at every
# `append` call site (mypy --strict checks src and tests), and mirrored into a
# frozenset derived from the SAME Literal so an unknown type is also rejected at
# runtime — the event sequence is the machine-readable trace of a run, so an
# unrecognized type must never slip in. New patterns extend this Literal in one
# place (e.g. the Human Gate's gate_* events, #10) rather than passing raw
# strings at call sites.
EventType = Literal[
    "run_started",
    "run_finished",
    "run_resumed",
    "run_errored",
    "node_started",
    "node_finished",
    "node_skipped",
    "node_retrying",
    # Human Gate events (#10, ADR 0010): a gate parks the Run (awaiting), and an
    # approval/rejection advances or ends it.
    "gate_awaiting",
    "gate_approved",
    "gate_rejected",
]

EVENT_TYPES: frozenset[str] = frozenset(get_args(EventType))


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

    def append(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Append one Event record as a JSON line.

        The ``event_type`` is drawn from the owned :data:`EventType` vocabulary;
        an unrecognized type is refused so a typo can never write an unknown
        record into the trace (#30).
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r}")
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
