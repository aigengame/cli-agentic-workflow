"""Shared fixtures and helpers for the seam tests.

The plain functions below are the run-directory / State / Events inspection helpers
and the schema/fixture/agent-workflow builders the seam tests share; they live here
once (imported via ``from conftest import ...``) rather than being copied into each
test file (#67). The fixtures (``write_workflow*``) follow the existing
callable-returning-fixture pattern.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from caw.model import Workflow, normalize_workflow


def single_run_dir(runs_root: Path) -> Path:
    """The sole run directory under ``runs_root`` (a Run materializes exactly one)."""
    run_dirs = list(runs_root.iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0]


def state_rows(run_dir: Path, query: str) -> list[dict[str, Any]]:
    """Every row a SELECT returns from a Run's SQLite State, as dicts."""
    connection = sqlite3.connect(run_dir / "state.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    """The Run's append-only Event trace, parsed from its JSONL file."""
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def write_schema(path: Path, schema: dict[str, Any]) -> Path:
    """Write a JSON Schema file (an Output Contract) and return its path."""
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def write_fixture(path: Path, **result: Any) -> Path:
    """Write a mock-Adapter fixture file (a canned normalized result)."""
    payload: dict[str, Any] = {"exit_status": 0, "stdout": "", "stderr": ""}
    payload.update(result)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def agent_workflow(fixture: Path, **inputs: Any) -> Workflow:
    """Build a single mock-Adapter agent-Node Workflow from one fixture file."""
    node_inputs: dict[str, Any] = {
        "adapter": "mock",
        "prompt": "summarize the repository",
        "fixture": str(fixture),
    }
    node_inputs.update(inputs)
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": "agent", "kind": "agent", "needs": [], "inputs": node_inputs}],
    }
    return normalize_workflow(raw, source="<test>")


@pytest.fixture
def write_workflow_data(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    """Return a factory that serializes a workflow mapping into tmp_path as YAML."""

    def _write(data: dict[str, Any]) -> Path:
        workflow_file = tmp_path / "workflow.yaml"
        workflow_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        return workflow_file

    return _write


@pytest.fixture
def write_workflow(
    write_workflow_data: Callable[[dict[str, Any]], Path],
) -> Callable[[str], Path]:
    """Return a factory that writes a single shell-node workflow file into tmp_path."""

    def _write(command: str) -> Path:
        return write_workflow_data(
            {
                "name": "sample",
                "version": 1,
                "nodes": [{"id": "greet", "kind": "shell", "inputs": {"command": command}}],
            }
        )

    return _write
