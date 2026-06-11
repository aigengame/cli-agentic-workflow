"""Durable Run State persisted as SQLite inside the run directory."""

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any

_SCHEMA = """
CREATE TABLE run (
    run_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    definition_checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE node (
    run_id TEXT NOT NULL REFERENCES run (run_id),
    node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE attempt (
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
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

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

    def record_node_started(self, run_id: str, node_id: str) -> None:
        self._execute(
            "INSERT INTO node (run_id, node_id, status) VALUES (?, ?, 'running')",
            (run_id, node_id),
        )

    def record_node_finished(self, run_id: str, node_id: str, status: str) -> None:
        self._execute(
            "UPDATE node SET status = ? WHERE run_id = ? AND node_id = ?",
            (status, run_id, node_id),
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

    def _execute(self, query: str, parameters: tuple[Any, ...]) -> None:
        self._connection.execute(query, parameters)
        self._connection.commit()
