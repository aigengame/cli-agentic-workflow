"""Real-agent-CLI e2e for the `classify-and-act` expansion path (#13, #86).

The `classify-and-act` expander routes on a CLASSIFIER's output: each branch entry
is gated by a `when` Predicate reading the classifier's `structured_output`. The
offline mock / CLI-seam tests prove the expansion SHAPE (classifier -> gated branches
-> join, and that expanded == handwritten by snapshot + checksum). This proves the
genuinely distinct integration boundary the mock cannot: a REAL agent's parsed
`structured_output` actually drives the `when` gate at runtime through `execute_run`,
so the matching branch RUNS and the non-matching one SKIPS. It is the classify-and-act
entry of the living e2e suite #86 anticipates.

Token-frugal by construction: ONE real agent call (the classifier). The branches and
join are free shell nodes, so this exercises the real-classifier-drives-routing path
without a second model call. Assertions are contract/structure-based, never free-text
(decision #4): the routing OUTCOME (which branch ran vs skipped) is asserted, not the
classifier's wording.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from caw.adapter import AdapterRegistry
from caw.executor import RunResult, execute_run
from caw.model import normalize_workflow
from e2e import harness

# A generous per-Node wall-clock budget so ordinary model latency never trips the
# kernel's timeout; a genuine hang still fails rather than blocking forever.
_NODE_TIMEOUT_S = 300.0
_CLASSIFIER_ID = "classify"
_FRUIT_ID = "handle-fruit"
_VEGGIE_ID = "handle-vegetable"


def _why(result: RunResult) -> str:
    """A debuggable reason string surfacing failed Nodes' stderr in an assertion."""
    return "; ".join(
        f"{node.node_id}: {node.status}: {node.stderr.strip()}"
        for node in result.node_results
        if not node.succeeded
    )


def _ran_succeeded(result: RunResult, node_id: str) -> bool:
    """Whether ``node_id`` was ATTEMPTED and succeeded (it is in ``node_results``)."""
    return any(node.node_id == node_id and node.succeeded for node in result.node_results)


@pytest.mark.asyncio
async def test_classify_and_act_routes_on_a_real_classifier_output(
    agent: str, tmp_path: Path
) -> None:
    # A `pattern: classify-and-act` whose classifier is a REAL agent: the expander
    # compiles it to plain IR (classifier -> two `when`-gated shell branches), then the
    # classifier reaches the real Agent CLI through execute_run and emits a `category`.
    # The kernel evaluates each branch's `when` against that REAL structured_output, so
    # the matching branch RUNS and the other SKIPS — the routing the mock cannot prove.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    schema = tmp_path / "category.schema.json"
    # `additionalProperties: false` + a fully-listed `required` keeps the schema valid
    # under codex's strict structured-output mode and is harmless for claude, so the
    # classifier runs under either CAW_E2E_AGENT (#11 symmetry).
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"category": {"type": "string", "enum": ["fruit", "vegetable"]}},
                "required": ["category"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    classifier_inputs: dict[str, Any] = {
        "adapter": harness.adapter_for_agent(agent),
        "prompt": (
            "Classify the word 'banana'. Put exactly 'fruit' or 'vegetable' "
            "in the 'category' field."
        ),
        "output_schema": str(schema),
        "env": list(harness.agent_env_names()),
    }
    run_args = harness.agent_run_args(agent)
    if run_args:
        classifier_inputs["args"] = list(run_args)

    def _branch(node_id: str, category: str, command: str) -> dict[str, Any]:
        # A `when`-gated free shell branch: runs iff the classifier's REAL
        # structured_output.category equals this branch's category (the sole
        # conditional mechanism, ADR 0007 / #75 path addressing).
        return {
            "id": node_id,
            "kind": "shell",
            "when": {
                "ref": {
                    "node": _CLASSIFIER_ID,
                    "field": "structured_output",
                    "path": ["category"],
                },
                "op": "equals",
                "value": category,
            },
            "inputs": {"command": command},
        }

    raw = {
        "name": "e2e-classify-and-act",
        "version": 1,
        "pattern": {
            "type": "classify-and-act",
            "classifier": {
                "id": _CLASSIFIER_ID,
                "kind": "agent",
                "timeout": _NODE_TIMEOUT_S,
                "inputs": classifier_inputs,
            },
            "branches": [
                _branch(_FRUIT_ID, "fruit", "echo handled fruit"),
                _branch(_VEGGIE_ID, "vegetable", "echo handled vegetable"),
            ],
            # `join: any` so the report runs on the one taken branch and tolerates the
            # skipped one (ADR 0007).
            "join": {
                "id": "report",
                "kind": "shell",
                "join": "any",
                "inputs": {"command": "echo reported"},
            },
        },
    }
    workflow = normalize_workflow(raw, source="<e2e>")

    # The pattern compiled to plain IR before anything ran: the branches are chained
    # onto the classifier, the report onto the branches — the expanded routing graph.
    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {
        _CLASSIFIER_ID: (),
        _FRUIT_ID: (_CLASSIFIER_ID,),
        _VEGGIE_ID: (_CLASSIFIER_ID,),
        "report": (_FRUIT_ID, _VEGGIE_ID),
    }

    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)

    assert result.succeeded, f"classify-and-act run failed: {_why(result)}"
    # 'banana' is a fruit, so the real classifier routes to the fruit branch: it RUNS
    # (succeeded, so it is in node_results) while the vegetable branch SKIPS on a closed
    # `when` gate (it is in skipped_node_ids with cause `when_false`, NOT node_results)
    # — a real structured_output driving the `when` gate. Asserting the routing OUTCOME,
    # not the classifier's wording.
    assert _ran_succeeded(result, _FRUIT_ID), (
        f"the fruit branch should run on a real 'fruit' classification: {_why(result)}"
    )
    assert _VEGGIE_ID in result.skipped_node_ids, (
        "the vegetable branch should skip when the classifier says 'fruit'"
    )
    assert result.skipped_causes.get(_VEGGIE_ID) == "when_false", (
        "the vegetable branch skip is a closed `when` gate, not a failure-driven skip"
    )
    # The `join: any` report ran on the one taken branch despite the skipped one.
    assert _ran_succeeded(result, "report")
