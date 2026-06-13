"""Agent-node-seam tests: agent Nodes run offline through the mock Adapter (#5).

These tests prove the vendor-neutral Adapter interface and the ``agent`` Node
kind end-to-end through ``execute_run`` with NO external Agent CLI installed:
the mock Adapter replays a fixture file as a normalized result.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from caw.executor import execute_run
from caw.model import Workflow, normalize_workflow


def single_run_dir(runs_root: Path) -> Path:
    run_dirs = list(runs_root.iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0]


def state_rows(run_dir: Path, query: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(run_dir / "state.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


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


@pytest.mark.asyncio
async def test_agent_node_runs_through_mock_adapter_replaying_a_fixture(
    tmp_path: Path,
) -> None:
    fixture = write_fixture(tmp_path / "fixture.json", exit_status=0, stdout="a one-line summary")
    workflow = agent_workflow(fixture)

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    (agent_result,) = result.node_results
    assert agent_result.node_id == "agent"
    assert agent_result.exit_status == 0
    assert agent_result.stdout == "a one-line summary"
