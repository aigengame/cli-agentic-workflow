"""Real ``claude.print`` graph-run e2e tests (#86).

These cover the cases whose correctness depends on the REAL Agent CLI: a real
``claude -p`` invocation, its result-wrapper shape, and a real agent Node flowing through
``execute_run`` into the Output Contract and State. This is a LIVING suite, not a fixed
set: today it bootstraps three runs (structured-output, freeform, real non-zero failure)
because ``claude.print`` is the only real node surface so far, and it grows as features
land (codex #11, multi-node graphs, patterns, resume). Behaviors a fixture can verify
completely and offline are covered by the mock suite instead — the two are co-weighted,
neither privileged.

Assertions are contract/structure-based, never free-text (decision #4): a structured
run is judged by exit 0 + the kernel validating the Output Contract + the typed shape
of the persisted ``structured_output``, not by the model's exact words. The agent is
selected by ``CAW_E2E_AGENT`` (default ``claude``); the suite runs locally only and
FAILS when the selected CLI is unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from caw.adapter import AdapterRegistry
from caw.claude_print import ClaudePrintAdapter
from caw.executor import FAILED, RunResult, execute_run
from caw.model import Workflow, normalize_workflow
from caw.state import StateStore
from e2e import harness

# A generous per-Node wall-clock budget so ordinary model latency never trips the
# kernel's timeout; a genuine hang still fails rather than blocking forever.
_NODE_TIMEOUT_S = 300.0
_NODE_ID = "agent"


def _agent_workflow(
    agent: str,
    *,
    prompt: str,
    output_schema: Path | None = None,
    args: tuple[str, ...] = (),
) -> Workflow:
    """A one-node agent Workflow targeting the selected agent's Adapter.

    The Node declares the ambient env-var names (ADR 0006 allow-list) so the real CLI
    inherits the developer's auth/config, and a generous timeout for model latency.
    """
    inputs: dict[str, Any] = {
        "adapter": harness.adapter_for_agent(agent),
        "prompt": prompt,
        "env": list(harness.agent_env_names()),
    }
    if output_schema is not None:
        inputs["output_schema"] = str(output_schema)
    if args:
        inputs["args"] = list(args)
    raw = {
        "name": "e2e",
        "version": 1,
        "nodes": [{"id": _NODE_ID, "kind": "agent", "timeout": _NODE_TIMEOUT_S, "inputs": inputs}],
    }
    return normalize_workflow(raw, source="<e2e>")


def _why(result: RunResult) -> str:
    """A debuggable reason string surfacing failed Nodes' stderr in an assertion."""
    return "; ".join(
        f"{node.node_id}: {node.status}: {node.stderr.strip()}"
        for node in result.node_results
        if not node.succeeded
    )


def _persisted_output(runs_root: Path, result: RunResult) -> dict[str, Any]:
    """Read the Node's persisted normalized output back from State (proves persistence)."""
    with StateStore(runs_root / result.run_id / "state.sqlite") as state:
        output = state.node_output(result.run_id, _NODE_ID)
    assert output is not None, "the node's output must be persisted to State"
    return output


@pytest.mark.asyncio
async def test_structured_output_graph_run(agent: str, tmp_path: Path) -> None:
    # A real `claude -p` structured run through execute_run: exit 0, the kernel
    # validates the real output against the node's tightly-constraining Output
    # Contract (so `succeeded` already implies the schema passed), and the
    # structured_output is persisted to State with the contracted shape.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    schema = tmp_path / "answer.schema.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            }
        ),
        encoding="utf-8",
    )
    workflow = _agent_workflow(
        agent,
        prompt="Compute 2 + 2. Put the result in the 'answer' field as an integer.",
        output_schema=schema,
    )
    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)

    assert result.succeeded, f"structured run failed: {_why(result)}"
    structured = _persisted_output(runs_root, result)["structured_output"]
    # Structure, not the exact value (robust to LLM nondeterminism, decision #4).
    assert isinstance(structured, dict)
    assert isinstance(structured.get("answer"), int)


@pytest.mark.asyncio
async def test_freeform_graph_run(agent: str, tmp_path: Path) -> None:
    # A real `claude -p` freeform run (no output_schema): exit 0, no structured
    # output, and a non-empty answer persisted to State. The only content assertion
    # is a weak non-empty check (allowed by decision #4) — never the exact text.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    workflow = _agent_workflow(agent, prompt="Reply with a one-word greeting.")
    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)

    assert result.succeeded, f"freeform run failed: {_why(result)}"
    (node,) = result.node_results
    assert node.structured_output is None, "a freeform run carries no structured output"
    assert node.stdout.strip(), "the freeform agent produced non-empty output"
    assert _persisted_output(runs_root, result)["stdout"].strip()


@pytest.mark.asyncio
async def test_real_failure_non_zero_path(agent: str, tmp_path: Path) -> None:
    # A real non-zero exit must flow through the executor into State as a FAILED
    # node. An invalid CLI flag makes the real `claude -p` exit non-zero at argument
    # parsing — deterministic, auth-free, and free of model nondeterminism — so this
    # exercises the failure path without flakiness (decision: invalid-flag trigger).
    # Not wrapped in transient retry: the failure is EXPECTED and deterministic.
    #
    # The CLI guard here is load-bearing: without it a MISSING claude would also make
    # the run not-succeed, silently satisfying the `not succeeded` assertion below — a
    # false green. require_agent_cli FAILS the test instead, so this only passes on a
    # real non-zero exit from a present CLI.
    harness.require_agent_cli(agent)
    workflow = _agent_workflow(agent, prompt="hello", args=("--caw-e2e-nonexistent-flag",))
    runs_root = tmp_path / "runs"

    result = await execute_run(workflow, runs_root, registry=AdapterRegistry())

    assert not result.succeeded, "an invalid CLI flag must fail the run"
    (node,) = result.node_results
    assert node.exit_status != 0, "the real CLI exited non-zero"
    assert node.failure_kind == FAILED
    with StateStore(runs_root / result.run_id / "state.sqlite") as state:
        statuses = state.node_statuses(result.run_id)
    assert statuses[_NODE_ID] == "failed", "the non-zero exit is recorded as FAILED in State"


@pytest.mark.asyncio
async def test_capability_check_reports_a_version(agent: str) -> None:
    # The real-CLI capability probe (`claude --version`): adapter infrastructure that
    # is free and tokenless, but still real-CLI-dependent — so it belongs in the e2e
    # tier (fail, never skip), not the mock suite. NOTE: capability_check is currently
    # claude.print-specific (not on the base Adapter), so this drives ClaudePrintAdapter
    # directly; today `claude` is the only wired agent, so a non-claude CAW_E2E_AGENT
    # already fails earlier at the require_agent_cli / adapter-resolution guard.
    harness.require_agent_cli(agent)

    version = await ClaudePrintAdapter().capability_check()

    assert version.strip(), "a real capability check reports a non-empty version string"
