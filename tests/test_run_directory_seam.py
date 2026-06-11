"""Run-directory-seam tests: inspect SQLite State, JSONL Events, and the snapshot on disk."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
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
        "inputs": {"command": "echo hello"},
    }
    canonical = json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    assert snapshot["definition_checksum"] == expected


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
    assert datetime.fromisoformat(run["created_at"]) <= datetime.fromisoformat(
        run["finished_at"]
    )

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
