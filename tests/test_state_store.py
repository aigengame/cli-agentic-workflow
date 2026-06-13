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


def test_read_methods_reconstruct_prior_run_state_for_resume(tmp_path: Path) -> None:
    # Resume reads prior State to decide what to (re-)run (#6): the run's status
    # gates resume eligibility, the node_id -> status map distinguishes the
    # succeeded Nodes (done) from the rest (eligible), and the max-attempt-per-node
    # map lets a re-run continue numbering past Attempts already recorded so the
    # `attempt` PK never collides. These read methods are what resume relies on.
    db_path = tmp_path / "state.sqlite"
    with StateStore(db_path) as state:
        state.record_run_started(
            run_id="run-1",
            workflow_name="sample",
            definition_checksum="sha256:abc",
            created_at="2026-06-12T00:00:00+00:00",
        )
        state.record_node_started(run_id="run-1", node_id="build")
        state.record_attempt(
            run_id="run-1",
            node_id="build",
            attempt=1,
            started_at="2026-06-12T00:00:00+00:00",
            finished_at="2026-06-12T00:00:01+00:00",
            exit_status=7,
            output={"exit_status": 7, "stdout": "", "stderr": "boom"},
        )
        state.record_node_finished(run_id="run-1", node_id="build", status="failed")
        state.record_node_skipped(run_id="run-1", node_id="deploy")
        state.record_run_finished(
            run_id="run-1", status="failed", finished_at="2026-06-12T00:00:02+00:00"
        )

    with StateStore(db_path) as reopened:
        assert reopened.run_status("run-1") == "failed"
        assert reopened.node_statuses("run-1") == {"build": "failed", "deploy": "skipped"}
        assert reopened.max_attempt_per_node("run-1") == {"build": 1}


def test_run_status_of_an_unknown_run_is_none(tmp_path: Path) -> None:
    # An unknown run id is reported as None rather than raising, so the resume
    # entry point can refuse it with a clear error of its own choosing.
    with StateStore(tmp_path / "state.sqlite") as state:
        assert state.run_status("no-such-run") is None


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
