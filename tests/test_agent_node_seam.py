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

from caw.config import WorkflowConfigError
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


def test_agent_node_missing_adapter_is_a_config_error() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": "agent", "kind": "agent", "inputs": {"prompt": "do it"}}],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "nodes[0 'agent'].inputs.adapter" in str(excinfo.value)


def test_agent_node_blank_prompt_is_a_config_error() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": "agent", "kind": "agent", "inputs": {"adapter": "mock", "prompt": "  "}}],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "nodes[0 'agent'].inputs.prompt" in message
    assert "must not be blank" in message


def test_agent_node_with_shell_command_input_is_a_config_error() -> None:
    # An agent Node carrying a shell `command` is a malformed mix; the discriminated
    # inputs union forbids the foreign field rather than silently ignoring it.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "inputs": {"adapter": "mock", "prompt": "do it", "command": "echo hi"},
            }
        ],
    }

    with pytest.raises(WorkflowConfigError):
        normalize_workflow(raw, source="workflow.yaml")


@pytest.mark.asyncio
async def test_parallel_agent_and_shell_nodes_run_fully_offline(tmp_path: Path) -> None:
    left = write_fixture(tmp_path / "left.json", exit_status=0, stdout="left agent")
    right = write_fixture(tmp_path / "right.json", exit_status=0, stdout="right agent")
    log = tmp_path / "join.log"
    raw: dict[str, Any] = {
        "name": "mixed",
        "version": 1,
        "nodes": [
            {
                "id": "left",
                "kind": "agent",
                "inputs": {"adapter": "mock", "prompt": "left", "fixture": str(left)},
            },
            {"id": "right", "kind": "shell", "inputs": {"command": f"echo right > {log}"}},
            {
                "id": "join",
                "kind": "agent",
                "needs": ["left", "right"],
                "inputs": {"adapter": "mock", "prompt": "join", "fixture": str(right)},
            },
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    statuses = {r.node_id: r.exit_status for r in result.node_results}
    assert statuses == {"left": 0, "right": 0, "join": 0}
    assert log.read_text(encoding="utf-8").strip() == "right"
