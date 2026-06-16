"""Real-agent-CLI e2e for the adversarial-verification Pattern Controller (#17, #86).

A Pattern Controller must drive a REAL Agent CLI round through ``execute_run`` into a
Run Group, exactly as the offline mock suite proves the control flow. The mock seam
tests prove the accept/reject mechanics; this proves a verification round actually
reaches the real CLI, records its Run Group membership, and the accept Predicate stops
the loop.

Token-frugal by construction: ONE real agent call. The round asks for a single
structured answer; the accept Predicate holds on round 1 (``exit_status equals 0`` over
the successful agent Node), so the loop stops accepted after exactly one real round —
proving the full controller path (materialize -> execute_run -> evaluate -> accept)
against the live CLI. Assertions are contract/structure-based, never free-text.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from caw.adapter import AdapterRegistry
from caw.controller import AdversarialSpec, GroupResult, run_adversarial_verification
from caw.runlayout import group_iterations_root
from caw.state import StateStore
from e2e import harness

_NODE_TIMEOUT_S = 300.0
_AGENT_ID = "verify"


@pytest.mark.asyncio
async def test_adversarial_round_reaches_the_real_agent_cli(agent: str, tmp_path: Path) -> None:
    # An adversarial-verification controller whose round is a real agent Node: the
    # controller materializes the round, runs it through execute_run against the real
    # CLI, validates the structured output, records the round's Run Group membership,
    # and the accept Predicate (exit_status == 0) stops the loop at round 1.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    schema = tmp_path / "answer.schema.json"
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
    round_workflow = tmp_path / "round.yaml"
    agent_inputs: dict[str, Any] = {
        "adapter": harness.adapter_for_agent(agent),
        "prompt": "Compute 2 + 2. Put the result in the 'answer' field as an integer.",
        "output_schema": str(schema),
        "env": list(harness.agent_env_names()),
    }
    run_args = harness.agent_run_args(agent)
    if run_args:
        agent_inputs["args"] = list(run_args)
    round_workflow.write_text(
        json.dumps(
            {
                "name": "adversarial-round-e2e",
                "version": 1,
                "nodes": [
                    {
                        "id": _AGENT_ID,
                        "kind": "agent",
                        "timeout": _NODE_TIMEOUT_S,
                        "inputs": agent_inputs,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    spec = AdversarialSpec.model_validate(
        {
            "workflow": str(round_workflow),
            "max_rounds": 2,
            "verify_node": _AGENT_ID,
            # Accepted when the round's agent Node exited 0 — holds on round 1, so the
            # loop stops after one real call (token-frugal, deterministic stop).
            "accept": {
                "ref": {"node": _AGENT_ID, "field": "exit_status"},
                "op": "equals",
                "value": 0,
            },
        }
    )

    async def do_verify() -> GroupResult:
        return await run_adversarial_verification(spec, base=tmp_path, registry=AdapterRegistry())

    result = await _retry_group(do_verify, base=tmp_path)

    assert result.status == "accepted", "the accept Predicate stopped the loop at round 1"
    assert len(result.iterations) == 1, "exactly one real round ran"
    iteration_result = result.iterations[0]
    assert iteration_result.succeeded, "the real agent round succeeded"

    run_dir = group_iterations_root(result.group_id, tmp_path) / iteration_result.run_id
    with StateStore(run_dir / "state.sqlite") as state:
        membership = state.run_group_membership(iteration_result.run_id)
        output = state.node_output(iteration_result.run_id, _AGENT_ID)
    assert membership == (result.group_id, 0), "the round records its group id and index"
    assert output is not None, "the real agent Node's output is persisted to State"
    structured = output["structured_output"]
    assert isinstance(structured, dict)
    assert isinstance(structured.get("answer"), int)


async def _retry_group(do_loop: Callable[[], Awaitable[GroupResult]], base: Path) -> GroupResult:
    """Retry the loop on a TRANSIENT failure of its single round (decision #6)."""
    result = await do_loop()
    attempts = 1
    while attempts < harness.DEFAULT_MAX_ATTEMPTS and _group_is_transient(result, base):
        result = await do_loop()
        attempts += 1
    return result


def _group_is_transient(result: GroupResult, base: Path) -> bool:
    """Whether a finished Run Group failed for a TRANSIENT reason on its last round."""
    if result.status != "failed" or not result.iterations:
        return False
    last = result.iterations[-1]
    if last.succeeded:
        return False
    run_dir = group_iterations_root(result.group_id, base) / last.run_id
    return harness.cli_run_is_transient(run_dir)
