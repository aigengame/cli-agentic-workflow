"""Real-agent-CLI e2e for the fan-out-synthesis sample (#14, #86).

The fan-out-synthesis sample fans the SAME task out to agent branches in parallel
and joins them in a synthesis node (CONTEXT.md: Parallel; the issue's hand-authored
fan-out-synthesis shape, built on the existing `parallel` expander). The offline
mock variant proves the SHAPE (two branches -> one synthesis node) and that the
report separates conclusion from trace; this proves the fan-out branches AND the
synthesis node actually reach the real Agent CLI through ``execute_run``, validate
their structured output against their Output Contract, and persist it to State.

Token-frugal by construction: a SMALL fan-out of TWO real agent branches plus one
synthesis call (three real calls total). Agent-neutral — the same `pattern: parallel`
shape runs under either ``CAW_E2E_AGENT`` (each branch targets the selected agent's
adapter), with `additionalProperties: false` schemas valid under codex strict mode.
The synthesis node depends on BOTH branches, so a green run proves the join fanned in
real branch outputs. Assertions are contract/structure-based, never free-text
(decision #4); the suite FAILS (never skips) when the selected CLI is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from caw.adapter import AdapterRegistry
from caw.executor import RunResult, execute_run
from caw.model import normalize_workflow
from caw.state import StateStore
from e2e import harness

# A generous per-Node wall-clock budget so ordinary model latency never trips the
# kernel's timeout; a genuine hang still fails rather than blocking forever.
_NODE_TIMEOUT_S = 300.0
_BRANCH_IDS = ("branch_a", "branch_b")
_SYNTH_ID = "synthesize"


def _why(result: RunResult) -> str:
    """A debuggable reason string surfacing failed Nodes' stderr in an assertion."""
    return "; ".join(
        f"{node.node_id}: {node.status}: {node.stderr.strip()}"
        for node in result.node_results
        if not node.succeeded
    )


def _agent_inputs(agent: str, *, prompt: str, schema: Path) -> dict[str, Any]:
    """The agent-Node inputs targeting the selected agent's Adapter (agent-neutral)."""
    inputs: dict[str, Any] = {
        "adapter": harness.adapter_for_agent(agent),
        "prompt": prompt,
        "output_schema": str(schema),
        "env": list(harness.agent_env_names()),
    }
    # The selected agent's headless-run flags (codex: sandbox + skip-git-repo-check;
    # claude: none) pass through as node args so the run is non-interactive everywhere.
    run_args = harness.agent_run_args(agent)
    if run_args:
        inputs["args"] = list(run_args)
    return inputs


@pytest.mark.asyncio
async def test_fanout_synthesis_branches_and_join_reach_the_real_agent_cli(
    agent: str, tmp_path: Path
) -> None:
    # A `pattern: parallel` fan-out-synthesis sample wrapping real agent Nodes: the
    # expander compiles it to plain IR (two independent branches + a synthesis node
    # that needs both), each branch reaches the real Agent CLI through execute_run, the
    # kernel validates each real output against its Output Contract, and the synthesis
    # node — gated on BOTH branches finishing — runs last against the real CLI too.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent

    answer_schema = tmp_path / "answer.schema.json"
    # `additionalProperties: false` + a fully-listed `required` keep the schema valid
    # under codex's strict (OpenAI structured-output) mode and are harmless for claude,
    # so the same fan-out runs under either CAW_E2E_AGENT (#11 symmetry).
    answer_schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    synth_schema = tmp_path / "synthesis.schema.json"
    synth_schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"total": {"type": "integer"}},
                "required": ["total"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )

    raw = {
        "name": "e2e-fanout-synthesis",
        "version": 1,
        "pattern": {
            "type": "parallel",
            "branches": [
                {
                    "id": _BRANCH_IDS[0],
                    "kind": "agent",
                    "timeout": _NODE_TIMEOUT_S,
                    "inputs": _agent_inputs(
                        agent,
                        prompt=(
                            "Compute 1 + 1. Put the result in the 'answer' field "
                            "as an integer."
                        ),
                        schema=answer_schema,
                    ),
                },
                {
                    "id": _BRANCH_IDS[1],
                    "kind": "agent",
                    "timeout": _NODE_TIMEOUT_S,
                    "inputs": _agent_inputs(
                        agent,
                        prompt=(
                            "Compute 2 + 2. Put the result in the 'answer' field "
                            "as an integer."
                        ),
                        schema=answer_schema,
                    ),
                },
            ],
            "join": {
                "id": _SYNTH_ID,
                "kind": "agent",
                "timeout": _NODE_TIMEOUT_S,
                "inputs": _agent_inputs(
                    agent,
                    prompt=(
                        "Synthesize: add 2 and 4. Put the sum in the 'total' field "
                        "as an integer."
                    ),
                    schema=synth_schema,
                ),
            },
        },
    }
    workflow = normalize_workflow(raw, source="<e2e>")

    # The pattern compiled to plain IR before anything ran: two independent branches
    # (no needs) and a synthesis node that needs BOTH — the fan-out-synthesis shape,
    # not a hand-authored graph.
    synth = next(node for node in workflow.nodes if node.id == _SYNTH_ID)
    assert sorted(synth.needs) == sorted(_BRANCH_IDS), (
        "the expander fanned both branches into the synthesis node"
    )
    for branch_id in _BRANCH_IDS:
        branch = next(node for node in workflow.nodes if node.id == branch_id)
        assert branch.needs == (), "a fan-out branch is independent (no needs)"

    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)

    assert result.succeeded, f"fan-out-synthesis run failed: {_why(result)}"
    with StateStore(runs_root / result.run_id / "state.sqlite") as state:
        # Each real fan-out branch persisted its contracted structured output.
        for branch_id in _BRANCH_IDS:
            output = state.node_output(result.run_id, branch_id)
            assert output is not None, f"branch {branch_id} output is persisted to State"
            structured = output["structured_output"]
            assert isinstance(structured, dict)
            # Structure, not the exact value (robust to LLM nondeterminism, decision #4).
            assert isinstance(structured.get("answer"), int)
        # The synthesis node ran last (gated on both branches) and persisted its output.
        synth_output = state.node_output(result.run_id, _SYNTH_ID)
    assert synth_output is not None, "the synthesis node's output is persisted to State"
    synth_structured = synth_output["structured_output"]
    assert isinstance(synth_structured, dict)
    assert isinstance(synth_structured.get("total"), int)
