"""Real-agent-CLI e2e for the `pattern:` expansion path (#8, #86).

A pattern-authored workflow must reach the SAME real Agent CLI path a hand-authored
`nodes:` workflow does. `pattern:` compiles to plain Workflow IR at normalize time
(ADR 0008), so an agent Node inside a `pipeline` flows through `execute_run` into the
Output Contract and State identically to a hand-authored agent Node. The offline
mock / CLI-seam tests prove the expansion SHAPE (and that expanded == handwritten by
snapshot + checksum); this proves the expanded agent Node actually reaches the real
CLI. It is the patterns entry of the living e2e suite #86 anticipates.

Token-frugal by construction: ONE real agent call. The pipeline's upstream step is a
free shell node, so this also exercises a multi-step expanded chain (the agent step is
chained after the shell step) reaching the real CLI — not just a degenerate single node.
Assertions are contract/structure-based, never free-text (decision #4).
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
_AGENT_ID = "answer"


def _why(result: RunResult) -> str:
    """A debuggable reason string surfacing failed Nodes' stderr in an assertion."""
    return "; ".join(
        f"{node.node_id}: {node.status}: {node.stderr.strip()}"
        for node in result.node_results
        if not node.succeeded
    )


@pytest.mark.asyncio
async def test_pipeline_pattern_agent_step_reaches_the_real_agent_cli(
    agent: str, tmp_path: Path
) -> None:
    # A `pattern: pipeline` wrapping a real agent step: the expander compiles it to
    # plain IR (shell -> agent chain), then the agent Node reaches the real Agent CLI
    # (the adapter the selected agent maps to) through execute_run, the kernel validates
    # the real output against the Node's tightly-constraining Output Contract, and the
    # structured_output is persisted.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    schema = tmp_path / "answer.schema.json"
    # `additionalProperties: false` and a fully-listed `required` keep the schema valid
    # under codex's strict (OpenAI structured-output) mode and are harmless for claude,
    # so the expanded agent step runs under either CAW_E2E_AGENT (#11 symmetry).
    schema.write_text(
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
    agent_inputs: dict[str, Any] = {
        "adapter": harness.adapter_for_agent(agent),
        "prompt": "Compute 2 + 2. Put the result in the 'answer' field as an integer.",
        "output_schema": str(schema),
        "env": list(harness.agent_env_names()),
    }
    raw = {
        "name": "e2e-pattern",
        "version": 1,
        "pattern": {
            "type": "pipeline",
            "steps": [
                {"id": "seed", "kind": "shell", "inputs": {"command": "echo go"}},
                {
                    "id": _AGENT_ID,
                    "kind": "agent",
                    "timeout": _NODE_TIMEOUT_S,
                    "inputs": agent_inputs,
                },
            ],
        },
    }
    workflow = normalize_workflow(raw, source="<e2e>")

    # The pattern compiled to plain IR before anything ran: the agent step is chained
    # after the shell step, so this is the expanded path, not a hand-authored graph.
    assert [node.id for node in workflow.nodes] == ["seed", _AGENT_ID]
    answer_node = next(node for node in workflow.nodes if node.id == _AGENT_ID)
    assert answer_node.needs == ("seed",), (
        "the expander chained the agent step onto the shell step"
    )

    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)

    assert result.succeeded, f"pattern agent run failed: {_why(result)}"
    with StateStore(runs_root / result.run_id / "state.sqlite") as state:
        output = state.node_output(result.run_id, _AGENT_ID)
    assert output is not None, "the expanded agent Node's output is persisted to State"
    structured = output["structured_output"]
    # Structure, not the exact value (robust to LLM nondeterminism, decision #4).
    assert isinstance(structured, dict)
    assert isinstance(structured.get("answer"), int)
