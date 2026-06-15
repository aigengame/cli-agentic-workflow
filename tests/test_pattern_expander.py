"""Pattern-expander seam: a `pattern:` block compiles to plain IR at normalize time.

ADR 0008: a Pattern Expander compiles a reusable shape into plain Workflow nodes
and edges inside a single Run at normalize time. These tests exercise that through
the public ``normalize_workflow`` surface — the expanded ``Workflow`` is asserted
to be the SAME IR a hand-authored ``nodes:`` workflow normalizes to, so the
acyclic validation, checksum, graph, and run machinery operate on it unchanged.
"""

from typing import Any

import pytest

from caw.config import WorkflowConfigError
from caw.model import definition_checksum, normalize_workflow, workflow_snapshot


def test_pipeline_pattern_expands_steps_into_a_linear_chain() -> None:
    # A `pattern: pipeline` lists ordered steps; the expander chains each step
    # onto its predecessor via `needs`, producing a linear plain-node IR.
    raw: dict[str, Any] = {
        "name": "ci",
        "version": 1,
        "pattern": {
            "type": "pipeline",
            "steps": [
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                {"id": "test", "kind": "shell", "inputs": {"command": "echo test"}},
                {"id": "deploy", "kind": "shell", "inputs": {"command": "echo deploy"}},
            ],
        },
    }

    workflow = normalize_workflow(raw, source="ci.yaml")

    ids = [node.id for node in workflow.nodes]
    assert ids == ["build", "test", "deploy"]
    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {"build": (), "test": ("build",), "deploy": ("test",)}


def test_expanded_pipeline_is_identical_to_the_handwritten_equivalent() -> None:
    # AC: an expanded workflow validates and runs identically to the hand-authored
    # equivalent. Asserting the persisted snapshot (and thus the checksum) of a
    # `pattern: pipeline` equals the snapshot of the hand-written `nodes:` chain
    # proves the expander produces the SAME IR — so graph, checksum, resume, and
    # execution all operate on it unchanged, with no special-casing.
    pattern_workflow = normalize_workflow(
        {
            "name": "ci",
            "version": 1,
            "pattern": {
                "type": "pipeline",
                "steps": [
                    {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                    {"id": "test", "kind": "shell", "inputs": {"command": "echo test"}},
                ],
            },
        },
        source="pattern.yaml",
    )
    handwritten_workflow = normalize_workflow(
        {
            "name": "ci",
            "version": 1,
            "nodes": [
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                {
                    "id": "test",
                    "kind": "shell",
                    "needs": ["build"],
                    "inputs": {"command": "echo test"},
                },
            ],
        },
        source="handwritten.yaml",
    )

    assert workflow_snapshot(pattern_workflow) == workflow_snapshot(handwritten_workflow)
    assert definition_checksum(pattern_workflow) == definition_checksum(handwritten_workflow)


def test_parallel_pattern_expands_branches_and_a_join_that_needs_them_all() -> None:
    # A `pattern: parallel` lists independent branches and an optional `join` node;
    # the expander emits the branches (no `needs`, so they run concurrently) plus a
    # join node that `needs` every branch — fanning in the independent work.
    raw: dict[str, Any] = {
        "name": "fanout",
        "version": 1,
        "pattern": {
            "type": "parallel",
            "branches": [
                {"id": "left", "kind": "shell", "inputs": {"command": "echo left"}},
                {"id": "right", "kind": "shell", "inputs": {"command": "echo right"}},
            ],
            "join": {"id": "merge", "kind": "shell", "inputs": {"command": "echo merge"}},
        },
    }

    workflow = normalize_workflow(raw, source="fanout.yaml")

    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {"left": (), "right": (), "merge": ("left", "right")}


def test_parallel_pattern_join_node_carries_its_join_policy() -> None:
    # A parallel join may declare a `join: any` policy so it tolerates a SKIPPED
    # branch (ADR 0007). The branch/join entries carry the same node fields a
    # hand-authored node has, so the policy reaches the expanded Node unchanged.
    raw: dict[str, Any] = {
        "name": "fanout",
        "version": 1,
        "pattern": {
            "type": "parallel",
            "branches": [
                {"id": "left", "kind": "shell", "inputs": {"command": "echo left"}},
                {"id": "right", "kind": "shell", "inputs": {"command": "echo right"}},
            ],
            "join": {
                "id": "merge",
                "kind": "shell",
                "join": "any",
                "inputs": {"command": "echo merge"},
            },
        },
    }

    workflow = normalize_workflow(raw, source="fanout.yaml")

    merge = next(node for node in workflow.nodes if node.id == "merge")
    assert merge.join == "any"
    assert merge.needs == ("left", "right")


def test_parallel_pattern_without_a_join_expands_only_independent_branches() -> None:
    # The join is optional: a `parallel` with no join is a pure fan-out of
    # independent branches that never join downstream.
    raw: dict[str, Any] = {
        "name": "fanout",
        "version": 1,
        "pattern": {
            "type": "parallel",
            "branches": [
                {"id": "left", "kind": "shell", "inputs": {"command": "echo left"}},
                {"id": "right", "kind": "shell", "inputs": {"command": "echo right"}},
            ],
        },
    }

    workflow = normalize_workflow(raw, source="fanout.yaml")

    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {"left": (), "right": ()}


def test_expanded_parallel_is_identical_to_the_handwritten_equivalent() -> None:
    # AC: the expanded `parallel` workflow is the SAME IR as the hand-authored
    # fan-out + join — same snapshot, same checksum.
    pattern_workflow = normalize_workflow(
        {
            "name": "fanout",
            "version": 1,
            "pattern": {
                "type": "parallel",
                "branches": [
                    {"id": "left", "kind": "shell", "inputs": {"command": "echo left"}},
                    {"id": "right", "kind": "shell", "inputs": {"command": "echo right"}},
                ],
                "join": {"id": "merge", "kind": "shell", "inputs": {"command": "echo merge"}},
            },
        },
        source="pattern.yaml",
    )
    handwritten_workflow = normalize_workflow(
        {
            "name": "fanout",
            "version": 1,
            "nodes": [
                {"id": "left", "kind": "shell", "inputs": {"command": "echo left"}},
                {"id": "right", "kind": "shell", "inputs": {"command": "echo right"}},
                {
                    "id": "merge",
                    "kind": "shell",
                    "needs": ["left", "right"],
                    "inputs": {"command": "echo merge"},
                },
            ],
        },
        source="handwritten.yaml",
    )

    assert workflow_snapshot(pattern_workflow) == workflow_snapshot(handwritten_workflow)


def test_declaring_both_pattern_and_nodes_is_a_config_error() -> None:
    # The authoring surface is `pattern:` XOR `nodes:` — a file declares exactly
    # one. Declaring both is a config error, surfaced as one `WorkflowConfigError`.
    raw: dict[str, Any] = {
        "name": "ci",
        "version": 1,
        "pattern": {
            "type": "pipeline",
            "steps": [{"id": "build", "kind": "shell", "inputs": {"command": "echo build"}}],
        },
        "nodes": [{"id": "x", "kind": "shell", "inputs": {"command": "echo x"}}],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="both.yaml")

    message = str(excinfo.value)
    assert "pattern" in message and "nodes" in message
    assert "not both" in message


def test_unknown_pattern_type_is_a_config_error_naming_the_known_patterns() -> None:
    raw: dict[str, Any] = {
        "name": "ci",
        "version": 1,
        "pattern": {"type": "loopy", "steps": []},
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="unknown.yaml")

    message = str(excinfo.value)
    assert "unknown pattern type" in message
    assert "loopy" in message
    assert "pipeline" in message and "parallel" in message, "the known patterns are listed"


def test_bad_pattern_params_give_one_error_line_with_a_field_path() -> None:
    # An expander's params failure surfaces through the same one-line
    # `WorkflowConfigError` contract a malformed `nodes:` workflow uses, with a
    # field path locating the offending param. An empty `steps` list is invalid.
    raw: dict[str, Any] = {
        "name": "ci",
        "version": 1,
        "pattern": {"type": "pipeline", "steps": []},
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="empty.yaml")

    message = str(excinfo.value)
    assert message.count("\n") == 0, "a config error is one line"
    assert "pattern.steps" in message, "the field path locates the offending param"


def test_a_pipeline_step_declaring_its_own_needs_is_a_config_error() -> None:
    # The expander owns the chaining; a step that declares `needs` is rejected so
    # the authored shape cannot fight the expansion.
    raw: dict[str, Any] = {
        "name": "ci",
        "version": 1,
        "pattern": {
            "type": "pipeline",
            "steps": [
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                {
                    "id": "test",
                    "kind": "shell",
                    "needs": ["build"],
                    "inputs": {"command": "echo test"},
                },
            ],
        },
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="needs.yaml")

    assert "must not declare `needs`" in str(excinfo.value)
