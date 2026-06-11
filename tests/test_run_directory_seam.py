"""Run-directory-seam tests: inspect SQLite State, JSONL Events, and the snapshot on disk."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

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
