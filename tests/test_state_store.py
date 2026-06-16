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


def test_run_group_membership_is_recorded_and_read_back(tmp_path: Path) -> None:
    # AC3 (#15): a Run records its run group id and iteration index. A Pattern
    # Controller writes the membership row into each iteration's run State after
    # execute_run mints the run, so the run itself carries which group and which
    # iteration it is — queryable from the run, not only from group.json.
    db_path = tmp_path / "state.sqlite"
    with StateStore(db_path) as state:
        state.record_run_started(
            run_id="run-1",
            workflow_name="loop",
            definition_checksum="sha256:abc",
            created_at="2026-06-16T00:00:00+00:00",
        )
        state.record_run_group_membership(
            run_id="run-1", run_group_id="grp-1", iteration_index=2
        )

    with StateStore(db_path) as reopened:
        assert reopened.run_group_membership("run-1") == ("grp-1", 2)


def test_run_group_membership_of_an_unmembered_run_is_none(tmp_path: Path) -> None:
    # A standalone run (not in a group) has no membership row, so the read returns
    # None — an ordinary single-Run directory is undisturbed by the additive table.
    with StateStore(tmp_path / "state.sqlite") as state:
        state.record_run_started(
            run_id="solo",
            workflow_name="sample",
            definition_checksum="sha256:abc",
            created_at="2026-06-16T00:00:00+00:00",
        )
        assert state.run_group_membership("solo") is None


def test_run_group_membership_table_is_additive_over_a_pre_existing_db(tmp_path: Path) -> None:
    # The membership table is added via CREATE TABLE IF NOT EXISTS (the established
    # convention, #76 lesson): reopening a State db created WITHOUT it must add the
    # table with no destructive migration and no error, so an older run directory is
    # forward-compatible. Simulate a pre-#15 db by creating the run/node/attempt
    # tables only, then reopening through StateStore.
    db_path = tmp_path / "state.sqlite"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        "CREATE TABLE run (run_id TEXT PRIMARY KEY, workflow_name TEXT, "
        "definition_checksum TEXT, status TEXT, created_at TEXT, finished_at TEXT, error TEXT);"
    )
    legacy.commit()
    legacy.close()

    with StateStore(db_path) as reopened:
        reopened.record_run_group_membership(
            run_id="run-1", run_group_id="grp-1", iteration_index=0
        )
        assert reopened.run_group_membership("run-1") == ("grp-1", 0)


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
