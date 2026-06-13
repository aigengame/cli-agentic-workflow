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


@pytest.mark.asyncio
async def test_only_declared_env_vars_reach_the_node_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DECLARED_VAR", "declared-value")
    monkeypatch.setenv("UNDECLARED_VAR", "undeclared-value")
    seen_env = tmp_path / "seen_env.json"
    # The mock Adapter writes the env it received to `echo_env_to`, standing in
    # for the env an Agent CLI process would see.
    fixture = write_fixture(tmp_path / "fixture.json", exit_status=0, echo_env_to=str(seen_env))
    workflow = agent_workflow(fixture, env=["DECLARED_VAR"])

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    received = json.loads(seen_env.read_text(encoding="utf-8"))
    assert received == {"DECLARED_VAR": "declared-value"}, (
        "only the declared var reaches the node, with no parent-environment leakage"
    )


@pytest.mark.asyncio
async def test_env_values_appear_nowhere_in_state_events_or_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "s3cr3t-sentinel-do-not-persist"
    monkeypatch.setenv("API_TOKEN", sentinel)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("a produced artifact with no secret\n", encoding="utf-8")
    fixture = write_fixture(
        tmp_path / "fixture.json",
        exit_status=0,
        stdout="done",
        artifacts=[str(artifact)],
    )
    workflow = agent_workflow(fixture, env=["API_TOKEN"])

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    run_dir = single_run_dir(tmp_path / "runs")
    state_bytes = (run_dir / "state.sqlite").read_bytes()
    events_text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    snapshot_text = (run_dir / "workflow.normalized.json").read_text(encoding="utf-8")
    assert sentinel.encode() not in state_bytes, "the secret value must not reach State"
    assert sentinel not in events_text, "the secret value must not reach Events"
    assert sentinel not in snapshot_text, "the secret value must not reach the snapshot"
    for indexed in result.node_results:
        for path in indexed.artifacts:
            assert sentinel not in Path(path).read_text(encoding="utf-8")


def test_env_declaration_carries_names_not_values(tmp_path: Path) -> None:
    # The workflow definition declares env NAMES; a `name=value` form is rejected
    # so a secret value can never be authored into the inspectable definition.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "inputs": {
                    "adapter": "mock",
                    "prompt": "do it",
                    "env": ["DECLARED", "DECLARED"],
                },
            }
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="<test>")

    assert "duplicate env name" in str(excinfo.value)


def write_schema(path: Path, schema: dict[str, Any]) -> Path:
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_output_contract_violation_fails_the_node_naming_the_contract(
    tmp_path: Path,
) -> None:
    schema = write_schema(
        tmp_path / "summary.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )
    # The Agent CLI itself exited 0, but its structured output omits the required
    # `summary`: the Output Contract must still fail the Node.
    fixture = write_fixture(
        tmp_path / "fixture.json", exit_status=0, structured_output={"title": "no summary"}
    )
    workflow = agent_workflow(fixture, output_schema=str(schema))

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    (agent_result,) = result.node_results
    assert agent_result.exit_status != 0
    assert str(schema) in agent_result.stderr, "the error names the failed contract"


@pytest.mark.asyncio
async def test_output_contract_satisfied_lets_the_node_succeed(tmp_path: Path) -> None:
    schema = write_schema(
        tmp_path / "summary.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )
    fixture = write_fixture(
        tmp_path / "fixture.json", exit_status=0, structured_output={"summary": "all good"}
    )
    workflow = agent_workflow(fixture, output_schema=str(schema))

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    (agent_result,) = result.node_results
    assert agent_result.exit_status == 0


@pytest.mark.asyncio
async def test_output_contract_failure_skips_the_failed_nodes_dependents(
    tmp_path: Path,
) -> None:
    schema = write_schema(
        tmp_path / "schema.json",
        {"type": "object", "required": ["summary"]},
    )
    upstream = write_fixture(
        tmp_path / "up.json", exit_status=0, structured_output={"wrong": True}
    )
    downstream = write_fixture(tmp_path / "down.json", exit_status=0, stdout="should not run")
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "up",
                "kind": "agent",
                "inputs": {
                    "adapter": "mock",
                    "prompt": "up",
                    "fixture": str(upstream),
                    "output_schema": str(schema),
                },
            },
            {
                "id": "down",
                "kind": "agent",
                "needs": ["up"],
                "inputs": {"adapter": "mock", "prompt": "down", "fixture": str(downstream)},
            },
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    assert result.skipped_node_ids == ("down",)
    assert {r.node_id for r in result.node_results} == {"up"}


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
