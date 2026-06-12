"""Model-seam tests: Workflow IR validation details exercised through normalize_workflow."""

from typing import Any

import pytest

from caw.config import WorkflowConfigError
from caw.model import Node, ShellNodeInputs, Workflow, execution_order, normalize_workflow


def shell_node(node_id: str, *needs: str) -> Node:
    return Node(
        id=node_id, kind="shell", inputs=ShellNodeInputs(command="echo hi"), needs=tuple(needs)
    )


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


def test_execution_order_raises_on_an_unpeelable_remainder_instead_of_a_partial_order() -> None:
    # A validation-bypassing constructor (model_construct, model_copy(update=...))
    # can hold a cyclic graph; ordering it must fail loudly, never return a
    # partial order that an executor would record as a vacuously succeeded Run.
    workflow = Workflow.model_construct(
        name="sample",
        version=1,
        nodes=(shell_node("before"), shell_node("a", "b"), shell_node("b", "a")),
    )

    with pytest.raises(ValueError) as excinfo:
        execution_order(workflow)

    message = str(excinfo.value)
    assert "'a'" in message and "'b'" in message, "the unorderable nodes are named"
    assert "'before'" not in message, "orderable nodes are not blamed"


def test_cycle_extraction_reports_an_invariant_breach_as_value_error_not_stop_iteration() -> None:
    # If a remainder node references only nodes outside the remainder (an
    # unknown reference that escaped earlier validation), cycle extraction must
    # raise ValueError — which pydantic converts into a normal validation
    # error — never StopIteration, which would escape pydantic unwrapped.
    from caw.model import _find_cycle

    dangling = [shell_node("a", "missing")]

    with pytest.raises(ValueError, match="'a'"):
        _find_cycle(dangling)
