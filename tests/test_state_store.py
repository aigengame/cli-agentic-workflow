"""StateStore tests: reopening an existing State database and constraint enforcement."""

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from caw.state import StateStore


def test_reopening_an_existing_state_database_attaches_without_error(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    with StateStore(db_path) as state:
        state.record_run_started(
            run_id="run-1",
            workflow_name="sample",
            definition_checksum="sha256:abc",
            created_at="2026-06-12T00:00:00+00:00",
        )

    with StateStore(db_path) as reopened:
        reopened.record_run_finished(
            run_id="run-1", status="succeeded", finished_at="2026-06-12T00:00:01+00:00"
        )


def test_recording_an_attempt_for_a_nonexistent_node_is_rejected(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as state, pytest.raises(sqlite3.IntegrityError):
        state.record_attempt(
            run_id="no-such-run",
            node_id="no-such-node",
            attempt=1,
            started_at="2026-06-12T00:00:00+00:00",
            finished_at="2026-06-12T00:00:01+00:00",
            exit_status=0,
            output={"exit_status": 0, "stdout": "", "stderr": ""},
        )


def test_a_failure_during_construction_closes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupt = tmp_path / "state.sqlite"
    corrupt.write_text("this file is not a SQLite database", encoding="utf-8")
    connections: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def capturing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = cast(sqlite3.Connection, real_connect(*args, **kwargs))
        connections.append(connection)
        return connection

    monkeypatch.setattr("caw.state.sqlite3.connect", capturing_connect)

    with pytest.raises(sqlite3.DatabaseError):
        StateStore(corrupt)

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
