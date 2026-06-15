"""Run-directory-seam tests: inspect SQLite State, JSONL Events, and the snapshot on disk."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import read_events
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def invoke_run(workflow_file: Path) -> tuple[int, str]:
    result = runner.invoke(app, ["run", str(workflow_file)])
    return result.exit_code, result.output


def single_run_dir(tmp_path: Path) -> Path:
    run_dirs = list((tmp_path / ".caw" / "runs").iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0]


def state_row(run_dir: Path, query: str) -> dict[str, Any]:
    connection = sqlite3.connect(run_dir / "state.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(query).fetchone()
    finally:
        connection.close()
    assert row is not None
    return dict(row)


def test_run_creates_run_directory_with_state_events_and_snapshot(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)

    exit_code, output = invoke_run(workflow_file)

    assert exit_code == 0
    run_dir = single_run_dir(tmp_path)
    assert run_dir.name in output, "the CLI result names the run id"
    assert (run_dir / "state.sqlite").is_file()
    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "workflow.normalized.json").is_file()


def test_snapshot_carries_a_definition_checksum_over_the_normalized_workflow(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)

    invoke_run(workflow_file)

    snapshot = json.loads(
        (single_run_dir(tmp_path) / "workflow.normalized.json").read_text(encoding="utf-8")
    )
    workflow = snapshot["workflow"]
    assert workflow["name"] == "sample"
    assert workflow["nodes"][0] == {
        "id": "greet",
        "kind": "shell",
        # The normalized inputs carry the discriminator tag `kind` since #5 added
        # the agent Node kind: `inputs` is a discriminated union and the tag is
        # part of its serialized form. The shell-node env allow-list (#66) is part
        # of the persisted inputs too, so a resume reconstructs the SAME env scope;
        # the default (no declared env) round-trips explicitly as an empty list.
        "inputs": {"kind": "shell", "command": "echo hello", "env": []},
        "needs": [],
        # The per-Node failure-semantics policy (#6) is part of the persisted
        # snapshot so a resume reconstructs the SAME retry/timeout budgets it ran
        # under; the defaults round-trip explicitly.
        "retries": 0,
        "timeout": None,
        # The node-level `when` predicate and `join` policy (#7) round-trip in the
        # snapshot too, so a resume reconstructs the SAME conditional/join
        # behavior; the defaults (no predicate, `all` join) serialize explicitly.
        "when": None,
        "join": "all",
    }
    canonical = json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    assert snapshot["definition_checksum"] == expected


def test_workflow_fixture_round_trips_commands_with_backslash_newline_and_mixed_quotes(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = 'echo "first line"\necho \'second\\nline\' "with\'mixed"'
    workflow_file = write_workflow(command)
    monkeypatch.chdir(tmp_path)

    exit_code, _ = invoke_run(workflow_file)

    assert exit_code == 0
    snapshot = json.loads(
        (single_run_dir(tmp_path) / "workflow.normalized.json").read_text(encoding="utf-8")
    )
    assert snapshot["workflow"]["nodes"][0]["inputs"]["command"] == command


def test_state_records_succeeded_run_node_attempt_and_normalized_output(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)

    invoke_run(workflow_file)

    run_dir = single_run_dir(tmp_path)
    snapshot = json.loads((run_dir / "workflow.normalized.json").read_text(encoding="utf-8"))

    run = state_row(run_dir, "SELECT * FROM run")
    assert run["run_id"] == run_dir.name
    assert run["workflow_name"] == "sample"
    assert run["definition_checksum"] == snapshot["definition_checksum"]
    assert run["status"] == "succeeded"
    assert datetime.fromisoformat(run["created_at"]) <= datetime.fromisoformat(run["finished_at"])

    node = state_row(run_dir, "SELECT * FROM node")
    assert node["node_id"] == "greet"
    assert node["status"] == "succeeded"

    attempt = state_row(run_dir, "SELECT * FROM attempt")
    assert attempt["node_id"] == "greet"
    assert attempt["attempt"] == 1
    assert attempt["exit_status"] == 0
    assert datetime.fromisoformat(attempt["started_at"]) <= datetime.fromisoformat(
        attempt["finished_at"]
    )
    assert json.loads(attempt["output_json"]) == {
        "exit_status": 0,
        "stdout": "hello\n",
        "stderr": "",
    }


def test_state_records_failed_run_node_and_attempt_exit_status(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo oops >&2; exit 7")
    monkeypatch.chdir(tmp_path)

    exit_code, _ = invoke_run(workflow_file)

    assert exit_code != 0
    run_dir = single_run_dir(tmp_path)
    assert state_row(run_dir, "SELECT * FROM run")["status"] == "failed"
    assert state_row(run_dir, "SELECT * FROM node")["status"] == "failed"

    attempt = state_row(run_dir, "SELECT * FROM attempt")
    assert attempt["attempt"] == 1
    assert attempt["exit_status"] == 7
    output = json.loads(attempt["output_json"])
    assert output["exit_status"] == 7
    assert "oops" in output["stderr"]


def test_a_failed_node_skips_its_dependent_but_not_an_independent_node(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Branch-failure isolation (#4): this replaces the old positional
    # stop-the-run, which wrongly withheld an independent node declared after a
    # failure. `deploy` needs the failing `build`, so it is skipped; `audit` is
    # independent, so it still runs even though the run as a whole fails.
    deployed = tmp_path / "deployed.txt"
    audited = tmp_path / "audited.txt"
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "build", "kind": "shell", "inputs": {"command": "exit 7"}},
                {
                    "id": "deploy",
                    "kind": "shell",
                    "needs": ["build"],
                    "inputs": {"command": f"touch {deployed}"},
                },
                {"id": "audit", "kind": "shell", "inputs": {"command": f"touch {audited}"}},
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    exit_code, _ = invoke_run(workflow_file)

    assert exit_code == 1, "the run fails because a node failed"
    assert not deployed.exists(), "a dependent of the failed node never runs"
    assert audited.exists(), "an independent node still runs despite a peer's failure"
    run_dir = single_run_dir(tmp_path)
    assert state_row(run_dir, "SELECT * FROM run")["status"] == "failed"

    connection = sqlite3.connect(run_dir / "state.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        statuses = {
            row["node_id"]: row["status"]
            for row in connection.execute("SELECT node_id, status FROM node")
        }
    finally:
        connection.close()
    assert statuses == {"build": "failed", "deploy": "skipped", "audit": "succeeded"}, (
        "the dependent is recorded skipped, distinguishable from nodes that ran"
    )

    events = read_events(run_dir)
    started_nodes = {e["data"]["node_id"] for e in events if e["type"] == "node_started"}
    assert started_nodes == {"build", "audit"}, "a skipped node is never recorded as started"
    skipped = [e["data"]["node_id"] for e in events if e["type"] == "node_skipped"]
    assert skipped == ["deploy"], "the skipped node is recorded in the event trace too"
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["data"]["status"] == "failed"


def test_non_utf8_node_output_is_preserved_with_backslash_escapes(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow(r"printf '\377\376'")
    monkeypatch.chdir(tmp_path)

    exit_code, _ = invoke_run(workflow_file)

    assert exit_code == 0
    attempt = state_row(single_run_dir(tmp_path), "SELECT * FROM attempt")
    output = json.loads(attempt["output_json"])
    assert output["stdout"] == "\\xff\\xfe", "invalid UTF-8 bytes survive as backslash escapes"
    assert "�" not in output["stdout"]


def test_events_form_an_append_only_trace_of_a_succeeding_run(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)

    invoke_run(workflow_file)

    run_dir = single_run_dir(tmp_path)
    events = read_events(run_dir)
    assert [event["type"] for event in events] == [
        "run_started",
        "node_started",
        "node_finished",
        "run_finished",
    ]
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    for event in events:
        assert event["run_id"] == run_dir.name
        datetime.fromisoformat(event["ts"])

    node_finished = events[2]
    assert node_finished["data"]["node_id"] == "greet"
    assert node_finished["data"]["attempt"] == 1
    assert node_finished["data"]["exit_status"] == 0
    assert node_finished["data"]["status"] == "succeeded"
    assert events[3]["data"]["status"] == "succeeded"


def test_events_trace_a_failing_run(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("exit 7")
    monkeypatch.chdir(tmp_path)

    invoke_run(workflow_file)

    events = read_events(single_run_dir(tmp_path))
    assert [event["type"] for event in events] == [
        "run_started",
        "node_started",
        "node_finished",
        "run_finished",
    ]
    node_finished = events[2]
    assert node_finished["data"]["exit_status"] == 7
    assert node_finished["data"]["status"] == "failed"
    assert events[3]["data"]["status"] == "failed"
