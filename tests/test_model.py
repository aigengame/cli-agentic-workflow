"""Model-seam tests: Workflow IR validation details exercised through normalize_workflow."""

from typing import Any

import pytest

from caw.config import WorkflowConfigError
from caw.model import normalize_workflow


def test_cycle_error_names_only_the_cycle_members_not_downstream_nodes() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "tail", "kind": "shell", "needs": ["a"], "inputs": {"command": "echo tail"}},
            {"id": "a", "kind": "shell", "needs": ["b"], "inputs": {"command": "echo a"}},
            {"id": "b", "kind": "shell", "needs": ["a"], "inputs": {"command": "echo b"}},
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "dependency cycle: a -> b -> a" in message
    assert "tail" not in message, "a node downstream of the cycle is not a cycle member"
