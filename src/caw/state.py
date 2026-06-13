"""Durable Run State persisted as SQLite inside the run directory."""

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    definition_checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS node (
    run_id TEXT NOT NULL REFERENCES run (run_id),
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS attempt (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_status INTEGER,
    output_json TEXT,
    PRIMARY KEY (run_id, node_id, attempt),
    FOREIGN KEY (run_id, node_id) REFERENCES node (run_id, node_id)
);
"""


class StateStore:
    """Owns the State database of one Run."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def record_run_started(
        self, run_id: str, workflow_name: str, definition_checksum: str, created_at: str
    ) -> None:
        self._execute(
            "INSERT INTO run (run_id, workflow_name, definition_checksum, status, created_at)"
            " VALUES (?, ?, ?, 'running', ?)",
            (run_id, workflow_name, definition_checksum, created_at),
        )

    def record_run_finished(self, run_id: str, status: str, finished_at: str) -> None:
        self._execute(
            "UPDATE run SET status = ?, finished_at = ? WHERE run_id = ?",
            (status, finished_at, run_id),
        )

    def record_run_running(self, run_id: str) -> None:
        """Flip a finished Run row back to ``running`` for a resume (#6).

        A resume reuses the same Run row; setting it ``running`` and clearing the
        prior ``finished_at`` / ``error`` reflects that the Run is in flight again,
        so the row's final state after the resume is the resumed outcome, not a
        stale mix of the interrupted run's terminal fields.
        """
        self._execute(
            "UPDATE run SET status = 'running', finished_at = NULL, error = NULL WHERE run_id = ?",
            (run_id,),
        )

    def record_run_errored(self, run_id: str, error: str, finished_at: str) -> None:
        self._execute(
            "UPDATE run SET status = 'errored', error = ?, finished_at = ? WHERE run_id = ?",
            (error, finished_at, run_id),
        )

    def record_node_started(self, run_id: str, node_id: str) -> None:
        self._execute(
            "INSERT INTO node (run_id, node_id, status) VALUES (?, ?, 'running')",
            (run_id, node_id),
        )

    def record_node_running(self, run_id: str, node_id: str) -> None:
        """Flip an existing Node row back to ``running`` for a re-Attempt (#6).

        A retry re-launch within a Run, and a resume re-running an incomplete
        Node, both target a Node whose row already exists, so they UPDATE its
        status rather than INSERT (which would breach the ``(run_id, node_id)``
        PK). The first launch of a Node still goes through ``record_node_started``.
        """
        self._execute(
            "UPDATE node SET status = 'running' WHERE run_id = ? AND node_id = ?",
            (run_id, node_id),
        )

    def record_node_finished(self, run_id: str, node_id: str, status: str) -> None:
        self._execute(
            "UPDATE node SET status = ? WHERE run_id = ? AND node_id = ?",
            (status, run_id, node_id),
        )

    def record_node_skipped(self, run_id: str, node_id: str) -> None:
        """Record a Node that was never attempted because a dependency failed.

        A skipped Node has no prior ``running`` row and no Attempt, so it is
        inserted straight into its terminal ``skipped`` status (issue #4).
        """
        self._execute(
            "INSERT INTO node (run_id, node_id, status) VALUES (?, ?, 'skipped')",
            (run_id, node_id),
        )

    def record_attempt(
        self,
        run_id: str,
        node_id: str,
        attempt: int,
        started_at: str,
        finished_at: str,
        exit_status: int,
        output: dict[str, Any],
    ) -> None:
        self._execute(
            "INSERT INTO attempt"
            " (run_id, node_id, attempt, started_at, finished_at, exit_status, output_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, node_id, attempt, started_at, finished_at, exit_status, json.dumps(output)),
        )

    def run_status(self, run_id: str) -> str | None:
        """The recorded status of a Run, or ``None`` if no such Run exists.

        Resume reads this to gate eligibility: a Run that already ``succeeded``
        has nothing to do, an unknown Run id (``None``) is refused, and any other
        terminal/interrupted status is resumable (#6). Returning ``None`` rather
        than raising lets the resume entry point own the error message.
        """
        row = self._connection.execute(
            "SELECT status FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def node_statuses(self, run_id: str) -> dict[str, str]:
        """Map each recorded Node of a Run to its status.

        Resume classifies from this map: a ``succeeded`` Node is done (seeded
        satisfied so its dependents can run); every other recorded Node — and any
        Node with no row at all (never started) — is eligible to (re-)run (#6).
        """
        return {
            str(node_id): str(status)
            for node_id, status in self._connection.execute(
                "SELECT node_id, status FROM node WHERE run_id = ?", (run_id,)
            )
        }

    def max_attempt_per_node(self, run_id: str) -> dict[str, int]:
        """Map each Node of a Run to the highest Attempt number it has recorded.

        Resume continues numbering a re-run Node from ``max + 1`` so a fresh
        Attempt never collides with one already in the ``attempt`` table for that
        Node (the ``(run_id, node_id, attempt)`` PK); a Node with no Attempt is
        simply absent and starts at Attempt 1 (#6).
        """
        return {
            str(node_id): int(highest)
            for node_id, highest in self._connection.execute(
                "SELECT node_id, MAX(attempt) FROM attempt WHERE run_id = ? GROUP BY node_id",
                (run_id,),
            )
        }

    def _execute(self, query: str, parameters: tuple[Any, ...]) -> None:
        self._connection.execute(query, parameters)
        self._connection.commit()
