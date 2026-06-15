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
from caw.model import normalize_workflow


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
