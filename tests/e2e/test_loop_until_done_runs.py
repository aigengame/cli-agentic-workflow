"""Real-agent-CLI e2e for the loop-until-done Pattern Controller (#15, #86).

A Pattern Controller must drive a REAL Agent CLI iteration through ``execute_run``
into a Run Group, exactly as the offline mock suite proves the control flow. The
mock seam tests prove the loop's mechanics (stop-on-done / feedback / membership /
group resume); this proves a controller iteration actually reaches the real CLI,
records its Run Group membership, and the done-predicate stops the loop.

Token-frugal by construction: ONE real agent call. The iteration asks for a single
structured answer; the done-predicate holds on iteration 1 (``exit_status equals 0``
over the successful agent Node), so the loop stops after exactly one real iteration —
proving the full controller path (materialize -> execute_run -> evaluate -> stop)
against the live CLI, not a degenerate offline stand-in. Assertions are
contract/structure-based, never free-text (decision #4).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from caw.adapter import AdapterRegistry
from caw.controller import ControllerSpec, GroupResult, run_loop_until_done
from caw.runlayout import group_iterations_root
from caw.state import StateStore
from e2e import harness

_NODE_TIMEOUT_S = 300.0
_AGENT_ID = "answer"


@pytest.mark.asyncio
async def test_loop_until_done_iteration_reaches_the_real_agent_cli(
    agent: str, tmp_path: Path
) -> None:
    # A loop-until-done controller whose iteration is a real agent Node: the
    # controller materializes the iteration, runs it through execute_run against the
    # real `claude -p`, validates the structured output, records the iteration's Run
    # Group membership, and the done-predicate (exit_status == 0) stops the loop at
    # iteration 1.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    schema = tmp_path / "answer.schema.json"
    schema.write_text(
        # `additionalProperties: false` + a fully-listed `required` keep the schema valid
        # under codex's strict `--output-schema` mode (claude accepts it too) — the same
        # agent-neutral shape the graph e2e uses, so one iteration schema drives both CLIs.
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
    iteration = tmp_path / "iteration.yaml"
    agent_inputs: dict[str, Any] = {
        "adapter": harness.adapter_for_agent(agent),
        "prompt": "Compute 2 + 2. Put the result in the 'answer' field as an integer.",
        "output_schema": str(schema),
        "env": list(harness.agent_env_names()),
    }
    # The selected agent's headless-run flags (codex needs --skip-git-repo-check
    # --sandbox read-only to run unattended in a non-git tmp dir) pass through as the
    # node's own args, exactly as the agent-neutral graph e2e threads them (#11). Without
    # them a codex iteration parks/fails and the loop never reaches "done".
    run_args = harness.agent_run_args(agent)
    if run_args:
        agent_inputs["args"] = list(run_args)
    iteration.write_text(
        json.dumps(
            {
                "name": "loop-iteration-e2e",
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
    spec = ControllerSpec.model_validate(
        {
            "workflow": str(iteration),
            "max_iterations": 2,
            "evaluate_node": _AGENT_ID,
            # Done when the iteration's agent Node exited 0 — holds on iteration 1, so
            # the loop stops after one real call (token-frugal, deterministic stop).
            "done": {
                "ref": {"node": _AGENT_ID, "field": "exit_status"},
                "op": "equals",
                "value": 0,
            },
        }
    )

    async def do_loop() -> GroupResult:
        return await run_loop_until_done(spec, base=tmp_path, registry=AdapterRegistry())

    result = await _retry_group(do_loop)

    assert result.status == "done", "the done-predicate stopped the loop at iteration 1"
    assert len(result.iterations) == 1, "exactly one real iteration ran"
    iteration_result = result.iterations[0]
    assert iteration_result.succeeded, "the real agent iteration succeeded"

    # The iteration's Run records its Run Group membership AND its real structured
    # output, read from its own State — the controller path reached the real CLI.
    run_dir = group_iterations_root(result.group_id, tmp_path) / iteration_result.run_id
    with StateStore(run_dir / "state.sqlite") as state:
        membership = state.run_group_membership(iteration_result.run_id)
        output = state.node_output(iteration_result.run_id, _AGENT_ID)
    assert membership == (result.group_id, 0), "the iteration records its group id and index"
    assert output is not None, "the real agent Node's output is persisted to State"
    structured = output["structured_output"]
    # Structure, not the exact value (robust to LLM nondeterminism, decision #4).
    assert isinstance(structured, dict)
    assert isinstance(structured.get("answer"), int)


async def _retry_group(do_loop: Callable[[], Awaitable[GroupResult]]) -> GroupResult:
    """Retry the loop on a TRANSIENT failure of its single iteration (decision #6).

    A Run Group whose only iteration failed for a transient reason (network / 5xx /
    rate-limit) is retried, reusing the harness's transient classifier on the failed
    iteration's run — a deterministic failure (bad flag, contract breach) is not.
    """
    result = await do_loop()
    attempts = 1
    while attempts < harness.DEFAULT_MAX_ATTEMPTS and _group_is_transient(result):
        result = await do_loop()
        attempts += 1
    return result


def _group_is_transient(result: GroupResult) -> bool:
    """Whether a finished Run Group failed for a transient reason on its last iteration."""
    if result.status != "failed" or not result.iterations:
        return False
    return any(not iteration.succeeded for iteration in result.iterations)
