"""Agent-node-seam tests: agent Nodes run offline through the mock Adapter (#5).

These tests prove the vendor-neutral Adapter interface and the ``agent`` Node
kind end-to-end through ``execute_run`` with NO external Agent CLI installed:
the mock Adapter replays a fixture file as a normalized result.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    agent_workflow,
    read_events,
    single_run_dir,
    state_rows,
    write_fixture,
    write_schema,
)

from caw.adapter import Adapter, AdapterRegistry, AgentInvocation, AgentResult
from caw.config import WorkflowConfigError
from caw.executor import execute_run
from caw.model import normalize_workflow


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
async def test_agent_node_structured_output_and_artifacts_are_indexed_in_state(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")
    fixture = write_fixture(
        tmp_path / "fixture.json",
        exit_status=0,
        stdout="ok",
        structured_output={"summary": "s"},
        artifacts=[str(artifact)],
    )
    workflow = agent_workflow(fixture)

    await execute_run(workflow, tmp_path / "runs")

    run_dir = single_run_dir(tmp_path / "runs")
    (attempt,) = state_rows(run_dir, "SELECT output_json FROM attempt")
    output = json.loads(attempt["output_json"])
    assert output["structured_output"] == {"summary": "s"}
    assert output["artifacts"] == [str(artifact)]


@pytest.mark.asyncio
async def test_a_nonexistent_artifact_path_is_not_indexed_in_state(tmp_path: Path) -> None:
    # #67: an adapter-supplied artifact path is validated for existence BEFORE being
    # indexed in State, so State never claims a "durable file produced by the run"
    # that never existed. A real artifact alongside a phantom one is kept; the
    # phantom is dropped rather than over-promising durability (full lifecycle is #16).
    real = tmp_path / "real.md"
    real.write_text("# real\n", encoding="utf-8")
    phantom = tmp_path / "does-not-exist.md"
    fixture = write_fixture(
        tmp_path / "fixture.json",
        exit_status=0,
        artifacts=[str(real), str(phantom)],
    )
    workflow = agent_workflow(fixture)

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    (agent_result,) = result.node_results
    assert [str(p) for p in agent_result.artifacts] == [str(real)], (
        "only the existing artifact is indexed; the phantom path is dropped"
    )
    run_dir = single_run_dir(tmp_path / "runs")
    (attempt,) = state_rows(run_dir, "SELECT output_json FROM attempt")
    output = json.loads(attempt["output_json"])
    assert output["artifacts"] == [str(real)], "State indexes only the existing artifact"


@pytest.mark.asyncio
async def test_a_directory_artifact_path_is_not_indexed_as_a_durable_file(
    tmp_path: Path,
) -> None:
    # #67: an Artifact is a durable FILE produced by the run; a path that exists but
    # is a directory is not a produced file, so it is not indexed either.
    a_dir = tmp_path / "a-directory"
    a_dir.mkdir()
    fixture = write_fixture(tmp_path / "fixture.json", exit_status=0, artifacts=[str(a_dir)])
    workflow = agent_workflow(fixture)

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    (agent_result,) = result.node_results
    assert agent_result.artifacts == (), "a directory is not a durable produced file"


@pytest.mark.asyncio
async def test_agent_node_records_an_attempt_and_node_events_like_a_shell_node(
    tmp_path: Path,
) -> None:
    fixture = write_fixture(tmp_path / "fixture.json", exit_status=0, stdout="hi")
    workflow = agent_workflow(fixture)

    await execute_run(workflow, tmp_path / "runs")

    run_dir = single_run_dir(tmp_path / "runs")
    assert [event["type"] for event in read_events(run_dir)] == [
        "run_started",
        "node_started",
        "node_finished",
        "run_finished",
    ]
    (node_row,) = state_rows(run_dir, "SELECT status FROM node")
    assert node_row["status"] == "succeeded"


class EnvObservingAdapter(Adapter):
    """A test-only Adapter that records the env it received (the env-observation seam).

    The production MockAdapter must not write resolved env values to a
    fixture-controlled path (#65); this test-only subclass stands in for the
    environment a real Agent CLI process would observe, capturing it in memory so
    a test can assert that ONLY declared variables — already filtered by the
    kernel's env policy — reached the Node, with no secret-bearing filesystem sink.
    """

    def __init__(self) -> None:
        self.observed_env: dict[str, str] | None = None

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        self.observed_env = dict(invocation.env)
        return AgentResult(exit_status=0)


@pytest.mark.asyncio
async def test_only_declared_env_vars_reach_the_node_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DECLARED_VAR", "declared-value")
    monkeypatch.setenv("UNDECLARED_VAR", "undeclared-value")
    # The test-only EnvObservingAdapter records the env it received, standing in
    # for the env an Agent CLI process would see (the seam moved out of the
    # production mock adapter, #65).
    observer = EnvObservingAdapter()
    registry = AdapterRegistry({"observe": observer})
    raw = agent_workflow(write_fixture(tmp_path / "f.json"), env=["DECLARED_VAR"]).model_dump(
        mode="json"
    )
    raw["nodes"][0]["inputs"]["adapter"] = "observe"
    workflow = normalize_workflow(raw, source="<test>", known_adapters=frozenset({"observe"}))

    result = await execute_run(workflow, tmp_path / "runs", registry=registry)

    assert result.succeeded
    assert observer.observed_env == {"DECLARED_VAR": "declared-value"}, (
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


def test_agent_invocation_repr_does_not_expose_env_values() -> None:
    # #65a: AgentInvocation.env holds resolved secret VALUES. Its repr must not
    # serialize them, so a future log line, exception message, or event payload
    # that stringifies an invocation cannot leak a secret. The declared NAME may
    # appear (it is already in the inspectable definition); the VALUE must not.
    from caw.adapter import AgentInvocation

    sentinel = "s3cr3t-sentinel-do-not-leak"
    invocation = AgentInvocation(
        node_id="n", adapter="mock", prompt="p", env={"API_TOKEN": sentinel}
    )

    rendered = repr(invocation)
    assert sentinel not in rendered, "the env VALUE must not appear in the repr"


def _agent_env_workflow(env: list[str]) -> dict[str, Any]:
    """A raw agent-Node workflow carrying ``env``, for env-validation tests."""
    return {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "inputs": {"adapter": "mock", "prompt": "do it", "env": env},
            }
        ],
    }


def test_agent_env_declaration_rejects_a_duplicate_name(tmp_path: Path) -> None:
    # The workflow definition declares env NAMES; a duplicate name is a config error.
    raw = _agent_env_workflow(["DECLARED", "DECLARED"])

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="<test>")

    assert "duplicate env name" in str(excinfo.value)


@pytest.mark.parametrize(
    "name",
    [
        "API_TOKEN=s3cr3t",  # value-shaped: a secret value smuggled past the allow-list
        "1INVALID",  # leading digit is not a valid POSIX env-var name
    ],
)
def test_agent_env_declaration_rejects_an_invalid_env_name(name: str, tmp_path: Path) -> None:
    # `env` is an allow-list of variable NAMES, never values. A `NAME=value` form
    # (or any entry that is not a valid POSIX env-var name) is rejected so a
    # secret-looking value can never be authored into the inspectable definition
    # and persisted into the normalized snapshot.
    raw = _agent_env_workflow([name])

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="<test>")

    assert "is not a valid env variable name" in str(excinfo.value)


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
async def test_output_contract_permitting_null_lets_a_null_output_succeed(tmp_path: Path) -> None:
    # #63: a schema that permits null and a null structured output passes the
    # contract end-to-end (no special-casing of None as an automatic violation).
    schema = write_schema(tmp_path / "nullable.schema.json", {"type": ["object", "null"]})
    fixture = write_fixture(tmp_path / "fixture.json", exit_status=0, structured_output=None)
    workflow = agent_workflow(fixture, output_schema=str(schema))

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    (agent_result,) = result.node_results
    assert agent_result.exit_status == 0


@pytest.mark.asyncio
async def test_output_contract_is_not_evaluated_when_the_agent_exited_non_zero(
    tmp_path: Path,
) -> None:
    # #63 exit-status gating (documented in the executor and ADR 0006): the Output
    # Contract guards a SUCCESSFUL invocation's structured output. A non-zero exit
    # is already a node failure, so the contract is not evaluated — the failure
    # reflects the agent's own exit, not a contract message that would mask it.
    schema = write_schema(tmp_path / "summary.schema.json", {"type": "object", "required": ["x"]})
    # exit_status 3 with a structured output that WOULD violate the schema:
    fixture = write_fixture(
        tmp_path / "fixture.json", exit_status=3, structured_output={"wrong": True}
    )
    workflow = agent_workflow(fixture, output_schema=str(schema))

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    (agent_result,) = result.node_results
    assert agent_result.exit_status == 3, "the node fails by the agent's own exit status"
    assert str(schema) not in agent_result.stderr, (
        "the contract is not evaluated on a non-zero exit, so it cannot mask the real cause"
    )


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


def agent_plus_independent_shell(agent_inputs: dict[str, Any], marker: Path) -> dict[str, Any]:
    """An agent Node and an INDEPENDENT shell Node with no edge between them.

    Used to assert that a failure on the agent branch fails only that node while
    the independent shell branch still completes (the scheduler never tears down
    a peer for another node's failure) — #61's whole-Run-crash guard.
    """
    return {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "agent", "kind": "agent", "inputs": {"adapter": "mock", **agent_inputs}},
            {"id": "side", "kind": "shell", "inputs": {"command": f"touch {marker}"}},
        ],
    }


@pytest.mark.asyncio
async def test_malformed_fixture_artifacts_fail_the_node_not_the_run(tmp_path: Path) -> None:
    # A fixture whose `artifacts` is not a list of path strings must fail the agent
    # Node as a clean adapter error, never crash the whole Run with a raw TypeError;
    # the independent shell branch still completes (#61).
    bad = write_fixture(tmp_path / "bad.json", exit_status=0, artifacts=[123, 456])
    marker = tmp_path / "side.txt"
    workflow = normalize_workflow(
        agent_plus_independent_shell({"prompt": "p", "fixture": str(bad)}, marker),
        source="<test>",
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    statuses = {r.node_id: r.exit_status for r in result.node_results}
    assert statuses["agent"] != 0, "the malformed fixture fails the agent node"
    assert statuses["side"] == 0, "the independent branch still completes"
    assert marker.exists(), "the independent shell node ran to completion"


@pytest.mark.asyncio
async def test_a_generic_adapter_exception_fails_the_node_not_the_run(tmp_path: Path) -> None:
    # An Adapter whose invoke() raises an arbitrary (non-AdapterError) Exception
    # must be normalized into a failed Node so the scheduler skips dependents,
    # never escape and crash the whole Run; the independent branch still completes.
    class ExplodingAdapter(Adapter):
        async def invoke(self, invocation: AgentInvocation) -> AgentResult:
            raise RuntimeError("boom from a real CLI parse/subprocess/timeout")

    registry = AdapterRegistry({"explode": ExplodingAdapter()})
    marker = tmp_path / "side.txt"
    raw = agent_plus_independent_shell({"prompt": "p"}, marker)
    raw["nodes"][0]["inputs"]["adapter"] = "explode"
    # A runtime-injected adapter is not in the built-in set, so its name is passed
    # through the validation context (#64).
    workflow = normalize_workflow(raw, source="<test>", known_adapters=frozenset({"explode"}))

    result = await execute_run(workflow, tmp_path / "runs", registry=registry)

    assert not result.succeeded
    statuses = {r.node_id: r.exit_status for r in result.node_results}
    assert statuses["agent"] != 0, "the generic exception fails the agent node"
    assert "boom" in next(r for r in result.node_results if r.node_id == "agent").stderr
    assert statuses["side"] == 0, "the independent branch still completes"
    assert marker.exists()


@pytest.mark.asyncio
async def test_adapter_determined_failure_fails_the_node_even_on_a_zero_exit(
    tmp_path: Path,
) -> None:
    # The adapter-determined-failure contract (#83): an Adapter that ran the agent
    # but normalized its result as a FAILURE signals it with the first-class
    # `AgentResult.adapter_failure` flag — NOT by manufacturing a non-zero
    # exit_status. The kernel honors the flag ONCE: a result with adapter_failure
    # set fails the Node even when the process's own exit_status is 0, so the
    # scheduler skips the failed Node's dependents exactly as a non-zero exit does.
    # The real exit_status is preserved in the trace (no fake exit code).
    class AdapterFailureAdapter(Adapter):
        async def invoke(self, invocation: AgentInvocation) -> AgentResult:
            return AgentResult(
                exit_status=0,
                stdout="partial work",
                stderr="claude reported an error (subtype: error_max_turns)",
                adapter_failure=True,
            )

    registry = AdapterRegistry({"adapterfail": AdapterFailureAdapter()})
    marker = tmp_path / "side.txt"
    raw = agent_plus_independent_shell({"prompt": "p"}, marker)
    raw["nodes"][0]["inputs"]["adapter"] = "adapterfail"
    workflow = normalize_workflow(raw, source="<test>", known_adapters=frozenset({"adapterfail"}))

    result = await execute_run(workflow, tmp_path / "runs", registry=registry)

    assert not result.succeeded, "an adapter-determined failure fails the run"
    agent_result = next(r for r in result.node_results if r.node_id == "agent")
    assert agent_result.failure_kind is not None, "the node is failed, not succeeded"
    assert agent_result.exit_status == 0, (
        "the adapter's real exit_status is preserved — no manufactured non-zero code"
    )
    assert "error" in agent_result.stderr.lower(), "the failure cause rides on stderr"
    assert marker.exists(), "the independent branch still completes"


@pytest.mark.asyncio
async def test_remote_ref_output_schema_fails_the_node_not_the_run(tmp_path: Path) -> None:
    # An output_schema with a remote `$ref` must fail the agent Node as a contract
    # error WITHOUT network resolution, never crash the Run; the independent branch
    # still completes (#61).
    schema = write_schema(
        tmp_path / "remote.schema.json", {"$ref": "https://example.com/remote.json"}
    )
    fixture = write_fixture(tmp_path / "fixture.json", exit_status=0, structured_output={"x": 1})
    marker = tmp_path / "side.txt"
    raw = agent_plus_independent_shell(
        {"prompt": "p", "fixture": str(fixture), "output_schema": str(schema)}, marker
    )
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    statuses = {r.node_id: r.exit_status for r in result.node_results}
    assert statuses["agent"] != 0, "the remote-$ref schema fails the agent node"
    assert str(schema) in next(r for r in result.node_results if r.node_id == "agent").stderr, (
        "the failure names the failed contract"
    )
    assert statuses["side"] == 0, "the independent branch still completes"
    assert marker.exists()


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
