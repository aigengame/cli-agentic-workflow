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


def test_a_pattern_without_a_type_is_a_config_error_listing_known_patterns() -> None:
    raw: dict[str, Any] = {
        "name": "ci",
        "version": 1,
        "pattern": {"steps": []},
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="notype.yaml")

    message = str(excinfo.value)
    assert "must declare a `type`" in message
    assert "pipeline" in message and "parallel" in message


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


# --- classify-and-act (#13) --------------------------------------------------


def _classify_and_act_raw() -> dict[str, Any]:
    # A classifier emits a category in its structured_output; each branch entry
    # carries a `when` that gates on the classifier's output (the sole conditional
    # mechanism — `path` addresses into structured_output, ADR 0007 / #75); the join
    # carries an explicit `join: any` policy so it runs when the one taken branch
    # succeeds and the others skip.
    return {
        "name": "triage",
        "version": 1,
        "pattern": {
            "type": "classify-and-act",
            "classifier": {
                "id": "classify",
                "kind": "agent",
                "inputs": {"adapter": "mock", "prompt": "Classify it", "fixture": "c.json"},
            },
            "branches": [
                {
                    "id": "handle-bug",
                    "kind": "shell",
                    "when": {
                        "ref": {
                            "node": "classify",
                            "field": "structured_output",
                            "path": ["category"],
                        },
                        "op": "equals",
                        "value": "bug",
                    },
                    "inputs": {"command": "echo bug"},
                },
                {
                    "id": "handle-feature",
                    "kind": "shell",
                    "when": {
                        "ref": {
                            "node": "classify",
                            "field": "structured_output",
                            "path": ["category"],
                        },
                        "op": "equals",
                        "value": "feature",
                    },
                    "inputs": {"command": "echo feature"},
                },
            ],
            "join": {
                "id": "report",
                "kind": "shell",
                "join": "any",
                "inputs": {"command": "echo report"},
            },
        },
    }


def test_classify_and_act_expands_into_classifier_gated_branches_and_a_join() -> None:
    # AC: classify-and-act expands into a classifier node, when-gated branch entry
    # nodes (each `needs` the classifier so its `when` reads the classifier output),
    # and a join that `needs` every branch and carries its explicit join policy.
    workflow = normalize_workflow(_classify_and_act_raw(), source="triage.yaml")

    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {
        "classify": (),
        "handle-bug": ("classify",),
        "handle-feature": ("classify",),
        "report": ("handle-bug", "handle-feature"),
    }
    # The branch `when` gates survived the expansion (the gating is the author's; the
    # expander only injects `needs`), and the join carries its explicit policy.
    handle_bug = next(node for node in workflow.nodes if node.id == "handle-bug")
    assert handle_bug.when is not None
    report = next(node for node in workflow.nodes if node.id == "report")
    assert report.join == "any"


def test_expanded_classify_and_act_is_identical_to_the_handwritten_equivalent() -> None:
    # AC: the expanded workflow is the SAME IR as the hand-authored classify -> gated
    # branches -> join — same snapshot, same checksum, so graph / resume / execution
    # all operate on it unchanged.
    pattern_workflow = normalize_workflow(_classify_and_act_raw(), source="pattern.yaml")
    handwritten = normalize_workflow(
        {
            "name": "triage",
            "version": 1,
            "nodes": [
                {
                    "id": "classify",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Classify it", "fixture": "c.json"},
                },
                {
                    "id": "handle-bug",
                    "kind": "shell",
                    "needs": ["classify"],
                    "when": {
                        "ref": {
                            "node": "classify",
                            "field": "structured_output",
                            "path": ["category"],
                        },
                        "op": "equals",
                        "value": "bug",
                    },
                    "inputs": {"command": "echo bug"},
                },
                {
                    "id": "handle-feature",
                    "kind": "shell",
                    "needs": ["classify"],
                    "when": {
                        "ref": {
                            "node": "classify",
                            "field": "structured_output",
                            "path": ["category"],
                        },
                        "op": "equals",
                        "value": "feature",
                    },
                    "inputs": {"command": "echo feature"},
                },
                {
                    "id": "report",
                    "kind": "shell",
                    "needs": ["handle-bug", "handle-feature"],
                    "join": "any",
                    "inputs": {"command": "echo report"},
                },
            ],
        },
        source="handwritten.yaml",
    )

    assert workflow_snapshot(pattern_workflow) == workflow_snapshot(handwritten)
    assert definition_checksum(pattern_workflow) == definition_checksum(handwritten)


def test_classify_and_act_without_a_join_expands_classifier_and_gated_branches() -> None:
    # The join is optional: a classify-and-act with no join is a classifier plus its
    # gated branches with nothing fanning them in downstream.
    raw = _classify_and_act_raw()
    del raw["pattern"]["join"]

    workflow = normalize_workflow(raw, source="triage.yaml")

    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {
        "classify": (),
        "handle-bug": ("classify",),
        "handle-feature": ("classify",),
    }


def test_a_classify_and_act_branch_declaring_its_own_needs_is_a_config_error() -> None:
    # The expander owns the `needs: [classifier]` injection; a branch declaring its
    # own `needs` is rejected so the authored shape cannot fight the expansion.
    raw = _classify_and_act_raw()
    raw["pattern"]["branches"][0]["needs"] = ["classify"]

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="needs.yaml")

    assert "must not declare `needs`" in str(excinfo.value)


def test_a_classify_and_act_classifier_declaring_needs_is_a_config_error() -> None:
    # The classifier is the entry node; it must not declare `needs`.
    raw = _classify_and_act_raw()
    raw["pattern"]["classifier"]["needs"] = ["nope"]

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="needs.yaml")

    assert "must not declare `needs`" in str(excinfo.value)


# --- generate-and-filter (#13) -----------------------------------------------


def _generate_and_filter_raw() -> dict[str, Any]:
    # N candidate generators run in parallel (independent, no `needs`); a filter node
    # `needs` every generator and emits the accepted candidates.
    return {
        "name": "brainstorm",
        "version": 1,
        "pattern": {
            "type": "generate-and-filter",
            "generators": [
                {
                    "id": "candidate-1",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Idea 1", "fixture": "g1.json"},
                },
                {
                    "id": "candidate-2",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Idea 2", "fixture": "g2.json"},
                },
            ],
            "filter": {
                "id": "accept",
                "kind": "agent",
                "inputs": {
                    "adapter": "mock",
                    "prompt": "Keep the strong ideas",
                    "fixture": "f.json",
                },
            },
        },
    }


def test_generate_and_filter_expands_into_parallel_generators_and_a_filter() -> None:
    # AC: generate-and-filter expands into parallel generators (independent, no
    # `needs`) plus a filter node that `needs` every generator.
    workflow = normalize_workflow(_generate_and_filter_raw(), source="brainstorm.yaml")

    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {
        "candidate-1": (),
        "candidate-2": (),
        "accept": ("candidate-1", "candidate-2"),
    }


def test_expanded_generate_and_filter_is_identical_to_the_handwritten_equivalent() -> None:
    pattern_workflow = normalize_workflow(_generate_and_filter_raw(), source="pattern.yaml")
    handwritten = normalize_workflow(
        {
            "name": "brainstorm",
            "version": 1,
            "nodes": [
                {
                    "id": "candidate-1",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Idea 1", "fixture": "g1.json"},
                },
                {
                    "id": "candidate-2",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Idea 2", "fixture": "g2.json"},
                },
                {
                    "id": "accept",
                    "kind": "agent",
                    "needs": ["candidate-1", "candidate-2"],
                    "inputs": {
                        "adapter": "mock",
                        "prompt": "Keep the strong ideas",
                        "fixture": "f.json",
                    },
                },
            ],
        },
        source="handwritten.yaml",
    )

    assert workflow_snapshot(pattern_workflow) == workflow_snapshot(handwritten)
    assert definition_checksum(pattern_workflow) == definition_checksum(handwritten)


def test_a_generate_and_filter_generator_declaring_needs_is_a_config_error() -> None:
    raw = _generate_and_filter_raw()
    raw["pattern"]["generators"][0]["needs"] = ["candidate-2"]

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="needs.yaml")

    assert "must not declare `needs`" in str(excinfo.value)


def test_a_generate_and_filter_filter_declaring_needs_is_a_config_error() -> None:
    raw = _generate_and_filter_raw()
    raw["pattern"]["filter"]["needs"] = ["candidate-1"]

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="needs.yaml")

    assert "must not declare `needs`" in str(excinfo.value)


# --- fan-out-synthesis (#13) -------------------------------------------------


def _fan_out_synthesis_raw() -> dict[str, Any]:
    # Parallel agent workers run independently; a synthesize node `needs` every
    # worker and fans their results into one synthesized output.
    return {
        "name": "research",
        "version": 1,
        "pattern": {
            "type": "fan-out-synthesis",
            "workers": [
                {
                    "id": "angle-a",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Angle A", "fixture": "a.json"},
                },
                {
                    "id": "angle-b",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Angle B", "fixture": "b.json"},
                },
            ],
            "synthesize": {
                "id": "synthesize",
                "kind": "agent",
                "inputs": {
                    "adapter": "mock",
                    "prompt": "Synthesize the angles",
                    "fixture": "s.json",
                },
            },
        },
    }


def test_fan_out_synthesis_expands_into_parallel_workers_and_a_synthesize_join() -> None:
    # AC: fan-out-synthesis expands into parallel agent nodes (independent, no
    # `needs`) and a synthesize join that `needs` every worker.
    workflow = normalize_workflow(_fan_out_synthesis_raw(), source="research.yaml")

    needs = {node.id: node.needs for node in workflow.nodes}
    assert needs == {
        "angle-a": (),
        "angle-b": (),
        "synthesize": ("angle-a", "angle-b"),
    }


def test_expanded_fan_out_synthesis_is_identical_to_the_handwritten_equivalent() -> None:
    pattern_workflow = normalize_workflow(_fan_out_synthesis_raw(), source="pattern.yaml")
    handwritten = normalize_workflow(
        {
            "name": "research",
            "version": 1,
            "nodes": [
                {
                    "id": "angle-a",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Angle A", "fixture": "a.json"},
                },
                {
                    "id": "angle-b",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "Angle B", "fixture": "b.json"},
                },
                {
                    "id": "synthesize",
                    "kind": "agent",
                    "needs": ["angle-a", "angle-b"],
                    "inputs": {
                        "adapter": "mock",
                        "prompt": "Synthesize the angles",
                        "fixture": "s.json",
                    },
                },
            ],
        },
        source="handwritten.yaml",
    )

    assert workflow_snapshot(pattern_workflow) == workflow_snapshot(handwritten)
    assert definition_checksum(pattern_workflow) == definition_checksum(handwritten)


def test_fan_out_synthesis_synthesize_may_carry_a_join_policy() -> None:
    # The synthesize node carries the same node fields a hand-authored node has, so a
    # `join: any` policy (tolerate a skipped worker, ADR 0007) reaches it unchanged.
    raw = _fan_out_synthesis_raw()
    raw["pattern"]["synthesize"]["join"] = "any"

    workflow = normalize_workflow(raw, source="research.yaml")

    synthesize = next(node for node in workflow.nodes if node.id == "synthesize")
    assert synthesize.join == "any"
    assert synthesize.needs == ("angle-a", "angle-b")


def test_a_fan_out_synthesis_worker_declaring_needs_is_a_config_error() -> None:
    raw = _fan_out_synthesis_raw()
    raw["pattern"]["workers"][0]["needs"] = ["angle-b"]

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="needs.yaml")

    assert "must not declare `needs`" in str(excinfo.value)


def test_a_fan_out_synthesis_synthesize_declaring_needs_is_a_config_error() -> None:
    raw = _fan_out_synthesis_raw()
    raw["pattern"]["synthesize"]["needs"] = ["angle-a"]

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="needs.yaml")

    assert "must not declare `needs`" in str(excinfo.value)
